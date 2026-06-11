"""VTFIX-02B — VT Bulk usage history JSONL contract tests.

Uses temp/local files only. Must not touch production /var/lib/dns-tool paths.
Run: python3 -m unittest tests.test_vt_usage_history_contract
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import vt_bulk_check as vbc


class TestVtUsageHistoryContract(unittest.TestCase):
    def test_build_record_includes_attribution_fields(self):
        record = vbc._build_vt_bulk_usage_history_record(
            ts=1_700_000_000.0,
            job_id="job-test-1",
            status="done",
            submitted_at=1_699_999_990.0,
            completed_at=1_700_000_000.0,
            accepted_count=3,
            rejected_count=1,
            processed=3,
            total=3,
            url_count=2,
            domain_count=1,
            actual_lookups=7,
            estimated_requests=9,
            use_domain_reports=False,
            error_summary=None,
        )

        self.assertEqual(record["tool_name"], "vt_bulk_check")
        self.assertEqual(record["quota_units_consumed"], 7)
        self.assertEqual(record["actual_lookups"], 7)
        self.assertTrue(record["timestamp"].endswith("+00:00"))
        self.assertEqual(record["ts"], 1_700_000_000.0)
        self.assertEqual(record["job_id"], "job-test-1")
        self.assertEqual(record["url_count"], 2)
        self.assertEqual(record["domain_count"], 1)
        self.assertFalse(record["use_domain_reports"])
        self.assertIsNone(record["error_summary"])

    def test_append_writes_contract_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = Path(tmp) / "vt_usage_history.jsonl"
            original = vbc._USAGE_HISTORY_FILE
            vbc._USAGE_HISTORY_FILE = history_file
            try:
                summary = vbc._build_vt_bulk_usage_history_record(
                    ts=1_700_000_100.0,
                    job_id="job-test-2",
                    status="error",
                    submitted_at=1_700_000_050.0,
                    completed_at=1_700_000_100.0,
                    accepted_count=2,
                    rejected_count=0,
                    processed=1,
                    total=2,
                    url_count=1,
                    domain_count=1,
                    actual_lookups=4,
                    estimated_requests=6,
                    use_domain_reports=True,
                    error_summary="RuntimeError",
                )
                vbc._append_usage_history(summary)

                lines = history_file.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                record = json.loads(lines[0])
                self.assertEqual(record["tool_name"], "vt_bulk_check")
                self.assertTrue(record["timestamp"])
                self.assertEqual(record["quota_units_consumed"], 4)
                self.assertEqual(record["status"], "error")
                self.assertEqual(record["error_summary"], "RuntimeError")
                self.assertNotIn("example.com", json.dumps(record))
            finally:
                vbc._USAGE_HISTORY_FILE = original

    def test_live_runtime_files_not_modified(self):
        live_history = Path("/var/lib/dns-tool/vt_usage_history.jsonl")
        if not live_history.exists():
            self.skipTest("live history file absent")

        before_size = live_history.stat().st_size
        before_mtime = live_history.stat().st_mtime

        with tempfile.TemporaryDirectory() as tmp:
            history_file = Path(tmp) / "isolated_history.jsonl"
            original = vbc._USAGE_HISTORY_FILE
            vbc._USAGE_HISTORY_FILE = history_file
            try:
                vbc._append_usage_history(
                    vbc._build_vt_bulk_usage_history_record(
                        ts=1_700_000_200.0,
                        job_id="isolated-job",
                        status="done",
                        submitted_at=1_700_000_150.0,
                        completed_at=1_700_000_200.0,
                        accepted_count=1,
                        rejected_count=0,
                        processed=1,
                        total=1,
                        url_count=0,
                        domain_count=1,
                        actual_lookups=1,
                        estimated_requests=1,
                        use_domain_reports=True,
                        error_summary=None,
                    )
                )
            finally:
                vbc._USAGE_HISTORY_FILE = original

        self.assertEqual(live_history.stat().st_size, before_size)
        self.assertEqual(live_history.stat().st_mtime, before_mtime)


if __name__ == "__main__":
    unittest.main()
