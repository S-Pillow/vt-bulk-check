"""VTFIX-02D — Unified daily history schema tests (temp files only)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daily_history


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class TestVtDailyHistorySchema(unittest.TestCase):
    def test_increment_does_not_overwrite_mdi_bucket(self):
        data = {_today(): {"quota_units": 4, "tool": "mdi"}}
        daily_history.record_tool_usage(
            data, _today(), daily_history.TOOL_VT_BULK, quota_delta=2
        )
        day = data[_today()]
        self.assertEqual(day["tools"]["mdi"]["quota_units"], 4)
        self.assertEqual(day["tools"]["vt_bulk_check"]["quota_units"], 2)
        self.assertEqual(day["total_quota_units"], 6)

    def test_legacy_integer_preserved_as_legacy(self):
        entry = daily_history.normalize_day_entry(50)
        self.assertEqual(entry["tools"]["legacy"]["quota_units"], 50)
        self.assertNotIn("mdi", entry["tools"])

    def test_vt_bulk_job_completion_adds_jobs_without_quota(self):
        data: dict = {}
        daily_history.record_tool_usage(
            data, _today(), daily_history.TOOL_VT_BULK, quota_delta=10, jobs_delta=1, items_delta=25
        )
        day = data[_today()]
        self.assertEqual(day["tools"]["vt_bulk_check"]["quota_units"], 10)
        self.assertEqual(day["tools"]["vt_bulk_check"]["jobs"], 1)
        self.assertEqual(day["tools"]["vt_bulk_check"]["items_processed"], 25)

    def test_atomic_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vt_daily_history.json"
            data: dict = {}
            daily_history.record_tool_usage(
                data, _today(), daily_history.TOOL_VT_BULK, quota_delta=1
            )
            daily_history.save_history_file(path, data, indent=None)
            loaded = daily_history.load_history_file(path)
            self.assertEqual(
                loaded[_today()]["tools"]["vt_bulk_check"]["quota_units"], 1
            )


if __name__ == "__main__":
    unittest.main()
