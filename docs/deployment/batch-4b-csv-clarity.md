# Batch 4B — CSV Clarity and Spreadsheet-Friendly Date Format

## Summary

Batch 4B makes the VirusTotal Bulk Check Tool CSV export clearer and more
compatible with Excel and Google Sheets by:

1. Renaming the first column from `domain` to `input` (CSV-01)
2. Adding a `checked_as` column showing the exact VirusTotal object queried (CSV-02)
3. Changing the `last_scanned` date format to `YYYY-MM-DD HH:MM:SS UTC` (CSV-03)

**Branch:** `aipf/batch-4b-csv-clarity`
**Merge commit:** (fill in after merge)
**Deployed:** (fill in after deployment)

---

## Breaking Changes

> **Warning for analysts and downstream tooling.**
>
> This batch changes the CSV export format. Any spreadsheet template, script,
> or automation that references the exported CSV by column name or column
> position will be affected.
>
> Changes:
> - Column 1 renamed: `domain` → `input` (same data, new header)
> - Column 2 added: `checked_as` (shifts all subsequent columns right by one)
> - `last_scanned` date value format changed: `6/2/2026, 9:13:04 PM` → `2026-06-02 21:13:04 UTC`
>
> Teams using CSV exports in downstream tools should audit column references
> before deploying this batch.

---

## Tickets

### CSV-01 — Rename CSV column `domain` to `input`

**File:** `dns-tool/backend/routers/vt_bulk_check.py`
**Function:** `export_job_csv`
**Change:** Header string `"domain"` changed to `"input"`.

The column has always contained the original submitted input string
(result field `input`). The old header name `domain` was misleading
because URL inputs are also valid submissions.

No data change — only the column header is renamed.

---

### CSV-02 — Add `checked_as` column

**File:** `dns-tool/backend/routers/vt_bulk_check.py`
**Function:** `export_job_csv`

**What it shows:**
- For URL-type rows: the full normalized URL actually submitted to VirusTotal
  (e.g. `http://example.com/`, `https://example.com/path`)
- For domain-type rows: the normalized domain queried at the VT domain endpoint
  (e.g. `example.com`)

**Why it matters:** The analyst's original input (column `input`) may differ
from what was actually queried. For example:
- Analyst submits: `example.com/path`
- Tool queries VT with: `https://example.com/path`
- `input` = `example.com/path`, `checked_as` = `https://example.com/path`

The `checked_as` column lets analysts verify the exact VT object used,
and reproduce queries directly on the VirusTotal website.

**Implementation note (Option B):** `normalized_full_url` is read directly
from `job.results_by_normalized` inside the export lock block. It is **not**
added to the `GET /jobs/{job_id}` API response. `_job_results_list` is
unchanged.

---

### CSV-03 — Spreadsheet-friendly UTC date format

**File:** `dns-tool/backend/routers/vt_bulk_check.py`
**Function:** `_format_last_scanned`

**Old format:** `6/2/2026, 9:13:04 PM`
**New format:** `2026-06-02 21:13:04 UTC`

**Why the old format caused issues:**
- Single-digit month/day is locale-ambiguous (`6/2` = June 2 in the US,
  February 6 in many European locales)
- 12-hour AM/PM is not universally auto-parsed by spreadsheet software
- No timezone label — looked like local time to analysts in non-UTC timezones
- Used `.astimezone()` which depends on server system timezone

**Why the new format is better:**
- ISO 8601 year-month-day ordering is unambiguous in all locales
- Leading zeros (`06`, `02`) make the value sortable as a string
- 24-hour time eliminates AM/PM ambiguity
- Explicit `UTC` suffix makes timezone clear
- Recognized or trivially parseable by Excel, Google Sheets, pandas, and
  standard date libraries

**UI side effect (approved):** `_format_last_scanned` also populates the
`last_scanned_display` field returned by `GET /jobs/{job_id}`. The results
table in the browser UI will show dates in the new format after this batch
is deployed. No frontend code change is required.

---

## Final CSV Column Order

| # | Column | Contents |
|---|---|---|
| 1 | `input` | Original submitted string (as typed by the analyst) |
| 2 | `checked_as` | Exact VirusTotal object queried (normalized URL or domain) |
| 3 | `type` | `url` or `domain` — which VT API endpoint was used |
| 4 | `flagging` | Malicious + suspicious detections (empty on error) |
| 5 | `total_engines` | Total AV engines in the analysis (empty on error) |
| 6 | `detection_ratio` | `flagging/total` string, e.g. `2/91` (empty on error) |
| 7 | `last_scanned` | `YYYY-MM-DD HH:MM:SS UTC` — last VT analysis timestamp |
| 8 | `status` | `OK`, `STALE`, or `ERROR` |
| 9 | `error` | Error message if lookup failed; empty otherwise |

---

## Example Row — Domain Item

```
input,checked_as,type,flagging,total_engines,detection_ratio,last_scanned,status,error
example.com,example.com,domain,0,91,0/91,2026-06-02 21:13:04 UTC,OK,
```

## Example Row — URL Item

```
input,checked_as,type,flagging,total_engines,detection_ratio,last_scanned,status,error
example.com/path,https://example.com/path,url,0,91,0/91,2026-06-02 21:13:04 UTC,OK,
```

---

## Files Changed

| File | Change |
|---|---|
| `dns-tool/backend/routers/vt_bulk_check.py` | `_format_last_scanned`, `export_job_csv` |

**Files not changed:** `App.jsx`, `styles.css`, all Batch 1–3 docs,
`_normalize_item`, `SubmitRequest`, `SEC-04`, `_job_results_list`.

---

## Deployment Steps

1. Backup production backend router:
   ```
   cp /var/www/dns-tool/backend/routers/vt_bulk_check.py \
      /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-YYYYMMDD-HHMMSS
   ```

2. Copy updated backend router:
   ```
   cp /root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py \
      /var/www/dns-tool/backend/routers/vt_bulk_check.py
   ```

3. Restart backend service:
   ```
   sudo systemctl restart dns-tool-backend
   ```

4. Verify service active and bound to 127.0.0.1:8000:
   ```
   systemctl is-active dns-tool-backend
   ss -tlnp | grep 8000
   ```

5. Verify CSV export:
   ```
   curl -s "http://127.0.0.1:8000/api/vt-bulk-check/jobs/{job_id}/export" \
     > /tmp/test_export.csv
   head -2 /tmp/test_export.csv
   ```
   Expected header: `input,checked_as,type,flagging,total_engines,detection_ratio,last_scanned,status,error`

6. Verify `normalized_full_url` absent from job API response:
   ```
   curl -s "http://127.0.0.1:8000/api/vt-bulk-check/jobs/{job_id}" | \
     python3 -c "import sys,json; d=json.load(sys.stdin); \
       print('normalized_full_url present:', \
       any(r.get('normalized_full_url') for r in d.get('results',[])))"
   ```
   Expected: `normalized_full_url present: False`

**No frontend build is needed.** This is a backend-only change.

---

## Rollback Steps

1. Restore backup:
   ```
   cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-YYYYMMDD-HHMMSS \
      /var/www/dns-tool/backend/routers/vt_bulk_check.py
   ```

2. Restart service:
   ```
   sudo systemctl restart dns-tool-backend
   ```

3. Verify: export CSV and confirm first header is `domain`, date format is
   `M/D/YYYY, H:MM:SS AM/PM`.

---

## Completion Notes

*(Fill in after deployment)*

- Deployed by:
- Deployed at:
- Verification result:
- Issues found:
- Rollback needed: no / yes — reason:
