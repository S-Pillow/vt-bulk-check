"""
VTFIX-02F / MDI-21A: VT Bulk shared live counter coordination tests.

Uses tmp_path only. No live /var/lib/dns-tool/ access. No live VT calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quota_lock import QuotaLockTimeout, quota_lock
from routers import vt_bulk_check as vtb


@pytest.fixture(autouse=True)
def isolate_quota_paths(tmp_path, monkeypatch):
    """Redirect shared quota paths to tmp_path for every test."""
    usage = tmp_path / "vt_usage.json"
    lock_path = tmp_path / "vt_usage.lock"
    daily = tmp_path / "vt_daily_history.json"
    history = tmp_path / "vt_usage_history.jsonl"
    lock_path.touch()
    monkeypatch.setattr(vtb, "_USAGE_FILE", usage)
    monkeypatch.setattr(vtb, "_DAILY_HISTORY_FILE", daily)
    monkeypatch.setattr(vtb, "_USAGE_HISTORY_FILE", history)
    monkeypatch.setenv("VT_QUOTA_LOCK_FILE", str(lock_path))
    yield {"usage": usage, "lock": lock_path, "daily": daily, "history": history}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _mdi_style_write(usage: Path, count: int) -> None:
    """Simulate MDI quota_writer setting absolute count under exclusive flock."""
    today = _today()
    with quota_lock("exclusive", lock_path=usage.parent / "vt_usage.lock"):
        data = {"date_utc": today, "daily_lookups_used": count}
        tmp = usage.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, usage)


def _mdi_style_increment(usage: Path, amount: int = 1) -> None:
    """Simulate MDI quota_writer read-modify-write increment under exclusive flock."""
    today = _today()
    with quota_lock("exclusive", lock_path=usage.parent / "vt_usage.lock"):
        data = json.loads(usage.read_text()) if usage.exists() else {"date_utc": today, "daily_lookups_used": 0}
        if data.get("date_utc") != today:
            data = {"date_utc": today, "daily_lookups_used": 0}
        data["daily_lookups_used"] = int(data.get("daily_lookups_used", 0) or 0) + amount
        tmp = usage.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, usage)


def test_get_current_usage_reloads_after_external_write(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    _mdi_style_write(usage, 1)

    count, date = vtb._get_current_usage()
    assert count == 1
    assert date == _today()


def test_get_usage_endpoint_reloads_after_external_write(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    _mdi_style_write(usage, 1)

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    resp = client.get("/api/vt-bulk-check/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dailyLookupsUsed"] == 1
    assert "dailyLookupsLimit" in body
    assert "rateLimitPerMin" in body


def test_increment_usage_reloads_disk_before_increment(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    _mdi_style_write(usage, 1)

    asyncio.run(vtb._increment_usage())

    data = json.loads(usage.read_text())
    assert data["daily_lookups_used"] == 2


def test_mdi_write_then_vt_increment_final_count_is_two(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    _mdi_style_write(usage, 1)

    asyncio.run(vtb._increment_usage())

    assert json.loads(usage.read_text())["daily_lookups_used"] == 2


def test_vt_write_then_mdi_style_increment_final_count_is_two(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    asyncio.run(vtb._increment_usage())
    _mdi_style_increment(usage, 1)

    assert json.loads(usage.read_text())["daily_lookups_used"] == 2


def test_stale_cache_cannot_overwrite_mdi_increment(isolate_quota_paths):
    """Prime would-be stale state at 0, then MDI writes 1; VT increment must reach 2."""
    usage = isolate_quota_paths["usage"]
    usage.write_text(json.dumps({"date_utc": _today(), "daily_lookups_used": 0}))
    _mdi_style_write(usage, 1)

    asyncio.run(vtb._increment_usage())

    assert json.loads(usage.read_text())["daily_lookups_used"] == 2


def test_two_sequential_vt_increments_exact_count(isolate_quota_paths):
    async def _run():
        await vtb._increment_usage()
        await vtb._increment_usage()

    asyncio.run(_run())
    assert json.loads(isolate_quota_paths["usage"].read_text())["daily_lookups_used"] == 2


def test_vt_usage_json_mode_660_after_save(isolate_quota_paths):
    state = vtb.UsageState(date_utc=_today(), daily_lookups_used=3)
    vtb._save_usage_state(state)
    mode = oct(isolate_quota_paths["usage"].stat().st_mode & 0o777)
    assert mode == oct(0o660)


def test_increment_updates_daily_history_v2_without_overwriting_mdi(isolate_quota_paths):
    daily = isolate_quota_paths["daily"]
    today = _today()
    daily.write_text(
        json.dumps(
            {
                today: {
                    "schema_version": 2,
                    "total_quota_units": 1,
                    "tools": {
                        "mdi": {"quota_units": 1, "jobs": 1, "items_processed": 1},
                    },
                }
            }
        )
    )

    asyncio.run(vtb._increment_usage())

    data = json.loads(daily.read_text())
    day = data[today]
    assert day["schema_version"] == 2
    assert day["tools"]["mdi"]["quota_units"] == 1
    assert day["tools"]["vt_bulk_check"]["quota_units"] == 1
    assert day["total_quota_units"] == 2


def test_lock_timeout_does_not_mutate_usage(isolate_quota_paths):
    usage = isolate_quota_paths["usage"]
    usage.write_text(json.dumps({"date_utc": _today(), "daily_lookups_used": 5}))
    lock_path = isolate_quota_paths["lock"]

    with quota_lock("exclusive", lock_path=lock_path):
        with pytest.raises(QuotaLockTimeout):
            with quota_lock("exclusive", lock_path=lock_path, timeout=0.2):
                vtb._save_usage_state(vtb.UsageState(date_utc=_today(), daily_lookups_used=99))

    assert json.loads(usage.read_text())["daily_lookups_used"] == 5


def test_import_vt_bulk_check_module():
    import routers.vt_bulk_check  # noqa: F401
