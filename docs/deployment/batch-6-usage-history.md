# Batch 6 — Usage History and Downloadable Usage Reporting

**Branch:** `aipf/batch-6-usage-history`
**Theme:** Usage History and Downloadable Usage Reporting
**Status:** Implemented, pending merge and deployment

---

## Goal

Add lightweight operational usage history so administrators can evaluate
VirusTotal API quota consumption over time — without a dashboard, database,
or sensitive item data in any history file.

Key questions this answers:
- Are we regularly approaching the daily quota?
- How many jobs are being run, and how large are they?
- Do actual lookups track close to estimates, or is the 3× URL multiplier generating overhead?
- Is URL-mode (Batch 4A default) increasing API consumption?
- Do we need a higher VirusTotal API tier?

---

## Tickets

### USAGE-07 — .gitignore entries

Added to `.gitignore`:
```
vt_daily_history.json
vt_daily_history.tmp
vt_usage_history.jsonl
```

These are runtime operational files and must never be committed.

---

### USAGE-03 — JobState additions

New fields on `JobState`:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `estimated_requests` | `int` | `0` | Conservative request estimate computed at submit time |
| `rejected_count` | `int` | `0` | Count of items rejected at submit (never the values) |
| `usage_history_written` | `bool` | `False` | Dedup guard — prevents double-append to JSONL |

`estimated_requests` and `rejected_count` are also added to the Batch 5 snapshot schema so recovered jobs carry them. `usage_history_written=True` is set on recovered jobs to prevent re-writing history for already-logged completions.

---

### USAGE-01 — Rolling daily usage history

**File:** `/var/lib/dns-tool/vt_daily_history.json`

**Format:**
```json
{
  "2026-06-01": 312,
  "2026-06-02": 187,
  "2026-06-03": 136
}
```

**Behavior:**
- Updated as a side effect of every `_save_usage_state()` call
- `vt_usage.json` is always written first; daily history failure never affects quota tracking
- Atomic write: temp file + `os.replace`
- Corrupt files are silently replaced on next write
- Pruned to last 90 days on every write
- All failures logged as warnings, never raised

**New constant:** `_DAILY_HISTORY_MAX_DAYS = 90`

---

### USAGE-02 — Per-job JSONL helper

**Function:** `_append_usage_history(summary: Dict[str, Any])`

**VTFIX-02B contract (future appends):** Records are built by
`_build_vt_bulk_usage_history_record()` and include `tool_name: "vt_bulk_check"`,
ISO-8601 `timestamp`, and `quota_units_consumed` (mapped from `actual_lookups`)
in addition to legacy fields such as `ts` and `actual_lookups`. See
[vtfix-02b-vtbulk-history-contract.md](vtfix-02b-vtbulk-history-contract.md).

- Appends one newline-terminated JSON line per call
- Called outside `_JOBS_LOCK` — no I/O while holding the lock
- Creates file and parent directory if needed
- All failures logged as warnings, never raised
- Summary dict contains only counts and metadata — never domain names, URLs, item_order, or results_by_normalized

---

### USAGE-04 — Append completed and error job summaries

**Trigger:** End of `_process_job` at each final state.

**Dedup guard:** `usage_history_written` flag on `JobState` prevents duplicate appends if `_process_job` is ever retried or the error path is reached after the done path.

**JSONL record schema:**

| Field | Type | Description |
|---|---|---|
| `ts` | float | Unix timestamp of record creation |
| `job_id` | string | UUID |
| `status` | string | `"done"` or `"error"` |
| `submitted_at` | float or null | Job creation timestamp |
| `completed_at` | float or null | Job completion timestamp |
| `accepted_count` | int | Items accepted after normalization/deduplication |
| `rejected_count` | int | Items rejected at submit (count only) |
| `processed` | int | Items processed when job ended |
| `total` | int | Total items in job |
| `url_count` | int | Count of URL-type items (derived from result types) |
| `domain_count` | int | Count of domain-type items |
| `actual_lookups` | int | Real VT API calls made by this job |
| `estimated_requests` | int | Conservative estimate at submit time |
| `use_domain_reports` | bool | Whether domain-report mode was enabled |
| `error_summary` | string or null | Exception class name only (e.g., `"httpx.TimeoutException"`) |

**Does NOT include:** domain names, URLs, item_order, results_by_normalized, full tracebacks, user identity.

---

### USAGE-05 — Usage history CSV export endpoint

**Endpoint:** `GET /api/vt-bulk-check/usage-history/export`

**CSV filename:** `vt-usage-history.csv`

**CSV columns (exact order):**
```
timestamp_utc,job_id,status,submitted_at_utc,completed_at_utc,accepted_count,
rejected_count,processed,total,url_count,domain_count,estimated_requests,
actual_lookups,use_domain_reports,error_summary
```

**Timestamps:** Formatted as `YYYY-MM-DD HH:MM:SS UTC`.

**Behavior:**
- Returns header-only CSV if `vt_usage_history.jsonl` does not exist
- Skips corrupt JSONL lines with a warning log
- Makes zero VirusTotal API calls
- Does not expose domain names, URLs, item_order, or results_by_normalized
- Internal use only — no authentication (consistent with rest of tool)

---

### USAGE-06 — Frontend Download Usage Report button

**Placement:** Inside the API Limits / Usage card, below the daily usage progress bar.

**Helper text:** "Exports usage history for API quota planning."

**Button text:** "Download Usage Report"

**Behavior:**
- Calls `GET /api/vt-bulk-check/usage-history/export`
- Downloads as `vt-usage-history.csv`
- Reuses existing `setError` for failures
- No dashboard, chart, filter, date picker, or preview table

---

## File Storage

| File | Purpose | Format | Path |
|---|---|---|---|
| `vt_usage.json` | Current-day quota (unchanged) | JSON | `/var/lib/dns-tool/` |
| `vt_daily_history.json` | Rolling 90-day daily totals | JSON dict | `/var/lib/dns-tool/` |
| `vt_usage_history.jsonl` | Per-job summary records | Append-only JSONL | `/var/lib/dns-tool/` |

**Note:** These are operational visibility files only — not security-grade audit logs. No user attribution is possible because the tool has no authentication.

---

## Affected Files

| File | Change |
|---|---|
| `dns-tool/backend/routers/vt_bulk_check.py` | Constants, `JobState` fields, `_update_daily_history`, `_append_usage_history`, snapshot updates, `_process_job` append calls, `export_usage_history` endpoint |
| `vt-bulk-check/frontend/src/App.jsx` | `downloadUsageReport` function, Download Usage Report button JSX |
| `.gitignore` | 3 new runtime file entries |

---

## Deployment Checklist

```bash
STAMP=$(date +%Y%m%d-%H%M%S)

# Backup
cp /var/www/dns-tool/backend/routers/vt_bulk_check.py \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-${STAMP}
cp -r /var/www/html/tools/vt-bulk-check \
      /var/www/html/tools/vt-bulk-check.bak-${STAMP}

# Build
cd /root/vt-bulk-check-repo/vt-bulk-check/frontend
npm run build
grep -r "Download Usage Report" dist/ && echo "PASS"

# Deploy backend
cp /root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
python3 -m py_compile /var/www/dns-tool/backend/routers/vt_bulk_check.py && echo "PASS"
sudo systemctl restart dns-tool-backend.service
sleep 3
systemctl is-active dns-tool-backend.service

# Deploy frontend
cp -r /root/vt-bulk-check-repo/vt-bulk-check/frontend/dist/. \
   /var/www/html/tools/vt-bulk-check/

# Verify
curl -sf http://127.0.0.1:8000/api/vt-bulk-check/usage-history/export | head -2
# Expect: CSV header row
ls -la /var/lib/dns-tool/
# After a job: confirm vt_daily_history.json and vt_usage_history.jsonl exist
```

---

## Rollback Plan

```bash
STAMP=<timestamp>

cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-${STAMP} \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
sudo systemctl restart dns-tool-backend.service

rm -rf /var/www/html/tools/vt-bulk-check
cp -r /var/www/html/tools/vt-bulk-check.bak-${STAMP} \
      /var/www/html/tools/vt-bulk-check

# Optionally remove history files (not used by old code):
# rm -f /var/lib/dns-tool/vt_daily_history.json
# rm -f /var/lib/dns-tool/vt_usage_history.jsonl

# Verify
curl -sf http://127.0.0.1:8000/api/vt-bulk-check/usage-history/export
# Expect: 404 after rollback
```

---

## Out of Scope

- Dashboard or charts
- Date filters or report previews
- SQLite or any database
- Per-user attribution
- Export event tracking
- Alerting or email
- Authentication
- Full job result history
- Storing submitted domains or URLs

---

## Completion Notes

*(Update after production deployment is verified.)*
