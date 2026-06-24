"""
CSV export spreadsheet safety tests (detection_ratio date auto-parse fix).

No live VT calls. No runtime file access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from main import app
from routers import vt_bulk_check as vtb


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(vtb, "_USAGE_FILE", tmp_path / "vt_usage.json")
    monkeypatch.setattr(vtb, "_DAILY_HISTORY_FILE", tmp_path / "vt_daily_history.json")
    monkeypatch.setattr(vtb, "_USAGE_HISTORY_FILE", tmp_path / "vt_usage_history.jsonl")
    monkeypatch.setattr(vtb, "_LATEST_JOB_FILE", tmp_path / "vt_latest_job.json")
    monkeypatch.setenv("VT_QUOTA_LOCK_FILE", str(tmp_path / "vt_usage.lock"))
    (tmp_path / "vt_usage.lock").touch()
    vtb._JOBS.clear()
    yield
    vtb._JOBS.clear()


def _seed_job(job_id: str, rows: list[dict]) -> None:
    job = vtb.JobState(
        job_id=job_id,
        status="done",
        processed=len(rows),
        total=len(rows),
        item_order=[],
        results_by_normalized={},
    )
    for row in rows:
        normalized = row["normalized"]
        job.item_order.append(normalized)
        job.results_by_normalized[normalized] = row
    vtb._JOBS[job_id] = job


@pytest.mark.parametrize(
    "flagging,total,expected_ratio",
    [
        (0, 91, "0/91"),
        (1, 91, "1/91"),
        (2, 91, "2/91"),
        (6, 91, "6/91"),
        (9, 91, "9/91"),
        (10, 91, "10/91"),
        (14, 91, "14/91"),
    ],
)
def test_export_detection_ratio_prefixed_for_spreadsheet(flagging, total, expected_ratio):
    job_id = "csv-ratio-test"
    _seed_job(
        job_id,
        [
            {
                "input": "example.com",
                "normalized": "example.com",
                "type": "domain",
                "flagging_engines": flagging,
                "total_engines": total,
                "detection_ratio": expected_ratio,
                "last_scanned_display": "2026-06-02 21:13:04 UTC",
            }
        ],
    )

    client = TestClient(app)
    resp = client.get(f"/api/vt-bulk-check/jobs/{job_id}/export")
    assert resp.status_code == 200

    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("input,checked_as,type,flagging,total_engines,detection_ratio")
    data_line = lines[1]
    assert f"\t{expected_ratio}" in data_line


def test_csv_detection_ratio_cell_helper():
    assert vtb._csv_detection_ratio_cell("") == ""
    assert vtb._csv_detection_ratio_cell("1/91") == "\t1/91"
    assert vtb._csv_detection_ratio_cell("14/91") == "\t14/91"
