"""
VTFIX-02F cross-tool shared live counter regression tests.

Simulates MDI-style and VT Bulk-style writers against the same temp files.
No production runtime access. No VT API calls.
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

from quota_lock import quota_lock
from routers import vt_bulk_check as vtb


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    usage = tmp_path / "vt_usage.json"
    lock_path = tmp_path / "vt_usage.lock"
    lock_path.touch()
    monkeypatch.setattr(vtb, "_USAGE_FILE", usage)
    monkeypatch.setattr(vtb, "_DAILY_HISTORY_FILE", tmp_path / "vt_daily_history.json")
    monkeypatch.setattr(vtb, "_USAGE_HISTORY_FILE", tmp_path / "vt_usage_history.jsonl")
    monkeypatch.setenv("VT_QUOTA_LOCK_FILE", str(lock_path))
    yield usage


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _mdi_style_increment(usage: Path, amount: int = 1) -> None:
    today = _today()
    with quota_lock("exclusive", lock_path=usage.parent / "vt_usage.lock"):
        data = json.loads(usage.read_text()) if usage.exists() else {"date_utc": today, "daily_lookups_used": 0}
        if data.get("date_utc") != today:
            data = {"date_utc": today, "daily_lookups_used": 0}
        data["daily_lookups_used"] = int(data.get("daily_lookups_used", 0) or 0) + amount
        tmp = usage.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, usage)


def test_mdi_then_vt_same_day_count_is_two(isolate_paths):
    usage = isolate_paths
    usage.write_text(json.dumps({"date_utc": _today(), "daily_lookups_used": 0}))

    _mdi_style_increment(usage, 1)
    asyncio.run(vtb._increment_usage())

    assert json.loads(usage.read_text())["daily_lookups_used"] == 2


def test_vt_then_mdi_same_day_count_is_two(isolate_paths):
    usage = isolate_paths
    usage.write_text(json.dumps({"date_utc": _today(), "daily_lookups_used": 0}))

    asyncio.run(vtb._increment_usage())
    _mdi_style_increment(usage, 1)

    assert json.loads(usage.read_text())["daily_lookups_used"] == 2


def test_vt_single_increment_writes_one(isolate_paths):
    usage = isolate_paths
    usage.write_text(json.dumps({"date_utc": _today(), "daily_lookups_used": 0}))

    asyncio.run(vtb._increment_usage())

    assert json.loads(usage.read_text())["daily_lookups_used"] == 1
