# vt-bulk-check

Production-ready VirusTotal Bulk Check tool designed for security operations workflows: submit a list of domains and URLs, fetch detection summaries in a throttled batch, automatically re-check stale findings, and export results to CSV.

## What this tool does

- **Bulk check**: Paste domains and URLs, submit a job, and watch results stream in as the backend queries VirusTotal (VT) within your plan's rate limits.
- **Bare domain → URL mode**: By default, bare domains (e.g. `example.com`) are submitted as URLs (`http://example.com/`) to the VirusTotal URL reports endpoint. This mirrors the VirusTotal website's default behavior. A checkbox ("Use domain reports for bare domains") switches to the domain reports endpoint instead.
- **Quota awareness**: Tracks daily API usage, shows remaining quota, estimates lookups before submission, and warns when a job may exhaust the daily allowance.
- **Operational safety**: Server-side throttling respects VT rate limits (4 requests/minute, 15-second minimum interval). Submissions estimated to exceed the configured daily quota limit are rejected before any VT call is made.
- **URL cost transparency**: URLs may use up to 3 VT API requests each (lookup + scan submission + report fetch). Domain reports cost 1 request each. The UI shows an estimated request cost and flags URL-heavy submissions.
- **Estimated run time**: Before submitting, the UI shows an estimated completion time based on item count, URL vs domain mix, and the current rate limit.
- **Stale awareness**: Detects results older than a configured threshold and can automatically request a fresh scan and refresh results.
- **Bulk actions**: Rescan selected rows or rescan all stale rows in one click.
- **Error visibility**: Failed lookups are surfaced in the results table and in the CSV export. Rejected inputs (items that could not be parsed) are identified after submission. A clear message appears if a job disappears after a service restart.
- **Export**: Download a CSV with full result details including detection counts, ratio, last-scanned timestamp, status, and any error.
- **Latest job recovery**: The most recently completed job is saved as a snapshot. If the page is closed or the service restarts, the previous job's results can be recovered from a banner without re-running lookups. Snapshots expire after 7 days.
- **Usage reporting**: A downloadable usage history CSV tracks per-job API consumption (metadata and counts only) for quota planning.

## Repository layout

```
dns-tool/backend/
    FastAPI backend. Hosts all VT endpoints under /api/vt-bulk-check/*.
    Handles job lifecycle, VT calls, throttling, quota tracking, stale
    detection, re-scan and refresh workflows, CSV export, latest-job
    snapshot, daily usage history, and per-job usage history JSONL.

vt-bulk-check/frontend/
    Standalone frontend (Vite + React).
    Dark-mode-first security operations console UI with quota warnings,
    estimated run time, CSV export, progress/status states, bulk
    rescan UX, latest-job recovery banner, and Download Usage Report.

docs/deployment/
    Batch implementation records and sanitized systemd/config templates.
    batch-1-infra-guardrails.md      — QUOTA-05, SCOPE-05, SEC-02
    batch-2-quota-guardrails.md      — SEC-04, QUOTA-01/02/04, ERR-01
    batch-3-error-visibility.md      — DATA-01/02, PERSIST-01/02/04
    batch-4a-bare-domain-url-mode.md — bare domain → URL mode, checkbox
    batch-4b-csv-clarity.md          — CSV column and header updates
    batch-4c-ui-clarity.md           — UI label and UX improvements
    batch-5-latest-job-recovery.md   — latest completed job snapshot
    batch-6-usage-history.md         — daily history + per-job JSONL
    nginx-vt-bulk-check-proxy-timeout.md — nginx 504 fix (Refresh/Force Scan)
    nginx/                           — sanitized nginx location snippets
    systemd/                         — sanitized service and override templates
```

## Key features

### Bare domain lookup behavior (default: URL mode)

By default, bare domains are submitted to the VirusTotal **URL reports** endpoint:

| Input | Checked as | VT object |
|---|---|---|
| `example.com` | `http://example.com/` | URL report |
| `example.com/path` | `http://example.com/path` | URL report |
| `http://example.com` | `http://example.com/` | URL report |
| `https://example.com` | `https://example.com/` | URL report |

The "Use domain reports for bare domains" checkbox switches bare domains to the VT **domain reports** endpoint (1 request, no scan submission step). Explicit `http://` and `https://` URLs always use URL reports regardless of this setting.

URL reports cost up to 3 VT API requests. Domain reports cost 1.

### Job-based processing
- Submit a job with many inputs; the backend processes them sequentially under a rate limiter.
- The UI polls job status and progressively renders results.

### Quota tracking and pre-submission warnings
- The UI displays current daily usage and remaining quota.
- Before submitting, the estimated VT request cost is calculated: domain reports cost 1 request, URL reports up to 3.
- An amber warning appears if the estimated cost exceeds remaining daily quota.
- A red warning appears if the estimated cost exceeds the total daily quota limit.
- The backend rejects submissions whose estimated cost exceeds the daily quota limit (HTTP 422) before any VT call is made. Rejection tests consume zero VT quota.
- Warnings do not block submission; only the backend hard cap does.

### Error and recovery visibility
- Rejected inputs (items that failed normalization) are listed by count after submission.
- If a job ends in a fatal error, a safe message is shown (raw exception details remain in server logs only).
- When a job disappears after a service restart, a friendly explanation is shown instead of a raw error string.
- Results are in-memory only. A notice reminds analysts to export before closing the tab or restarting the service.

### Clear severity and status
- Results include malicious/suspicious counts, a flagging total, ratio, and last-scanned timestamp.
- UI highlights stale rows and surfaces per-row errors with a retry option.
- A summary banner identifies how many lookups failed and whether quota exhaustion (429) was the cause.

### Auto re-check stale results
When enabled, stale items trigger an automated pipeline:
1. Request a new scan on VT
2. Refresh reports until VT returns updated results
3. Update the UI table (no manual refresh required)

### CSV export

Export job results as a CSV. Exact columns:

```
input,checked_as,type,flagging,total_engines,detection_ratio,last_scanned,status,error
```

Column descriptions:
- `input` — original submitted value exactly as entered
- `checked_as` — the actual VT object checked (e.g. `http://example.com/` for a bare domain in URL mode)
- `type` — `domain` or `url`
- `flagging` — malicious + suspicious engine count
- `total_engines` — total engines that responded
- `detection_ratio` — e.g. `2/91` (CSV export prefixes with a tab so Excel does not auto-parse ratios like `1/91` as dates)
- `last_scanned` — formatted as `YYYY-MM-DD HH:MM:SS UTC`
- `status` — `OK`, `STALE`, or `ERROR`
- `error` — error detail if the lookup failed, empty otherwise

### Latest completed job recovery

The most recently completed job is saved as a snapshot at:

```
/var/lib/dns-tool/vt_latest_job.json
```

Behavior:
- Only completed (`status: done`) jobs are saved. Running jobs are not persisted mid-job.
- Only one snapshot is retained at a time (the most recent completed job).
- Snapshots expire after **7 days**. Expired snapshots are ignored on startup.
- On page load, if a valid snapshot exists, a recovery banner appears:
  > *A previous completed job is available.*
- The banner offers **Load results** and **Export CSV** options without re-running any VT lookups.
- There is no authentication or per-user recovery — a single shared snapshot is stored per server.

### Usage history and reporting

Usage history tracks per-job API consumption for quota planning. No submitted domains, URLs, or result data are stored.

**Download Usage Report** button (in the API Limits / Usage card) downloads a CSV from:

```
GET /api/vt-bulk-check/usage-history/export
```

This endpoint consumes zero VirusTotal quota.

Usage report CSV columns (exact order):

```
timestamp_utc,job_id,status,submitted_at_utc,completed_at_utc,accepted_count,
rejected_count,processed,total,url_count,domain_count,estimated_requests,
actual_lookups,use_domain_reports,error_summary
```

- `estimated_requests` — conservative pre-job estimate (URL items × 3, domain items × 1)
- `actual_lookups` — real VT API calls made by the job
- `error_summary` — exception class name only for failed jobs; `null` for successful jobs
- Timestamps are formatted as `YYYY-MM-DD HH:MM:SS UTC`

Usage history is for API quota planning only. It is not audit-grade logging and does not attribute requests to individual users.

## Runtime files

These files are created and managed by the backend service at runtime. **They must not be committed to version control.**

| File | Purpose |
|---|---|
| `/var/lib/dns-tool/vt_usage.json` | Current-day VT API quota counter. Contains `date_utc` and `daily_lookups_used`. Reset automatically at midnight UTC. |
| `/var/lib/dns-tool/vt_latest_job.json` | Snapshot of the most recently completed job for recovery. Expires after 7 days. |
| `/var/lib/dns-tool/vt_daily_history.json` | Rolling 90-day history of daily lookup totals. Updated as a side effect of quota counter saves. |
| `/var/lib/dns-tool/vt_usage_history.jsonl` | Append-only per-job usage summary records. Contains metadata and counts only — no submitted domains or URLs. New VT Bulk records (VTFIX-02B) include `tool_name: "vt_bulk_check"`, ISO `timestamp`, and `quota_units_consumed` alongside legacy fields (`ts`, `actual_lookups`, etc.). |

All four files are excluded from version control via `.gitignore`.

## API

Base path: `/api/vt-bulk-check`

### Submit a job

`POST /submit`

```json
{
  "items": ["example.com", "https://example.com/path"],
  "use_domain_reports": false
}
```

- `use_domain_reports` (optional, default `false`): when `true`, bare domains use the VT domain reports endpoint (1 request each) instead of URL reports (up to 3 requests each).

Response:
```json
{ "job_id": "uuid", "total": 2, "accepted": 2, "rejected": [] }
```

- `rejected`: inputs that could not be parsed (e.g. dots-only strings, whitespace-only strings). Contains a count; values are not echoed back.
- Returns HTTP 422 if the estimated VT request cost exceeds `DAILY_LOOKUPS_LIMIT`. Zero VT quota is consumed on rejection.

### Read job status and results

`GET /jobs/{job_id}`

Response fields:
- `status`: `running | done | error`
- `processed`, `total`
- `error_message`: `null` for normal jobs; exception string for `status: error` jobs (for maintainer debugging; not rendered in the UI)
- `results`: array of per-input results
- `update`: progress for the auto stale re-check pipeline

Returns HTTP 404 if the job no longer exists (e.g. service was restarted).

### Daily quota and usage

`GET /usage`

```json
{
  "jobLookupsUsed": 1,
  "dailyLookupsUsed": 42,
  "dailyLookupsLimit": 500,
  "rateLimitPerMin": 4
}
```

### Export job results as CSV

`GET /jobs/{job_id}/export`

Downloads a CSV with columns: `input, checked_as, type, flagging, total_engines, detection_ratio, last_scanned, status, error`.

### Latest completed job metadata

`GET /latest-job`

Returns metadata for the most recently completed job snapshot (job_id, submitted_at, completed_at, total, processed, use_domain_reports), or `{"job_id": null}` if no valid snapshot exists.

### Download usage history CSV

`GET /usage-history/export`

Downloads `vt-usage-history.csv` with per-job API usage records. CSV includes attribution columns `tool_name`, `timestamp`, and `quota_units_consumed` for records appended after VTFIX-02B. Returns a header-only CSV if no history exists yet. Consumes zero VT quota. Skips corrupt lines silently.

### Refresh a single item

`POST /refresh`
```json
{ "job_id": "uuid", "item": "example.com" }
```

### Request a new scan for a single item

`POST /force-scan`
```json
{ "job_id": "uuid", "item": "example.com", "type": "domain" }
```

### Refresh all stale items

`POST /refresh-stale`
```json
{ "job_id": "uuid" }
```

### Request new scans for all stale items

`POST /force-scan-stale`
```json
{ "job_id": "uuid" }
```

### Request new scans for selected items

`POST /force-scan-bulk`
```json
{ "job_id": "uuid", "normalized_items": ["example.com", "https://example.com/path"] }
```

### Auto-update stale results end-to-end

`POST /jobs/{job_id}/auto-update-stale`

Runs the complete stale re-check pipeline (scan + refresh) server-side. Progress is exposed via `GET /jobs/{job_id}`.

### Rescan eligibility

`GET /jobs/{job_id}/rescan-eligibility`

Returns counts of stale items eligible for auto-rescan, items skipped due to the rescan cap, and remaining daily quota.

## Configuration

### Required environment variables

- `VT_API_KEY` — VirusTotal API key. Store in a restricted file; do not put directly in systemd `Environment=` lines or commit to version control. See `docs/deployment/systemd/secrets.env.example`.

### Optional environment variables

- `VT_USAGE_FILE` — Path to the persistent daily usage counter JSON file. Defaults to `/tmp/vt_bulk_check_usage.json`. Set to a stable path outside `/tmp` to survive reboots. Example: `/var/lib/dns-tool/vt_usage.json`.

### Quota and rate limits (configured in backend)

- `DAILY_LOOKUPS_LIMIT` — Daily VT API request cap (default: `500` for the public API plan).
- `RATE_LIMIT_PER_MIN` — Requests per minute (default: `4`, enforced via a 15-second minimum interval).
- Domain reports cost 1 VT request each. URL reports cost up to 3 VT requests each (the default for bare domains).

## Running locally (development)

### Backend

```bash
cd dns-tool/backend
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn httpx
export VT_API_KEY="your_key_here"
export VT_USAGE_FILE="/tmp/vt_bulk_check_usage.json"
uvicorn main:app --host 127.0.0.1 --port 8000
```

Use `--host 127.0.0.1` in all environments. Do not bind to `0.0.0.0` unless you have a firewall or reverse proxy in front.

### Frontend

```bash
cd vt-bulk-check/frontend
npm install
npm run dev
```

The frontend expects the backend at `/api/vt-bulk-check/*` (proxied by Vite in dev, by nginx in production).

## Deployment (production)

See `docs/deployment/` for batch-by-batch implementation records and sanitized systemd templates.

### Production file locations

| Component | Path |
|---|---|
| Backend router | `/var/www/dns-tool/backend/routers/vt_bulk_check.py` |
| Frontend static files | `/var/www/html/tools/vt-bulk-check/` |
| Backend service | `dns-tool-backend.service` (systemd) |
| Runtime data | `/var/lib/dns-tool/` |

### Deployment steps

1. Back up the production backend router and frontend static directory before every deploy.
2. Build the frontend: `npm run build` in `vt-bulk-check/frontend/`.
3. Copy only the backend router file to `/var/www/dns-tool/backend/routers/`.
4. Run `python3 -m py_compile` on the copied file before restarting.
5. Restart `dns-tool-backend.service` and confirm it is active.
6. Confirm the backend is still bound to `127.0.0.1:8000` only.
7. Copy frontend `dist/` contents to `/var/www/html/tools/vt-bulk-check/`.
8. When using `cp -r` for frontend deploys, old hashed asset files accumulate. Use `rsync --delete` or clear the `assets/` subdirectory before copying to keep the directory clean.

### nginx (VT Bulk Check API)

The production `location ^~ /api/vt-bulk-check/` block **must** set
`proxy_read_timeout 300s`. Without it, nginx defaults to 60s and returns **504
Gateway Time-out** on Refresh/Force Scan while the backend may still complete the
VT work. See [`docs/deployment/nginx-vt-bulk-check-proxy-timeout.md`](docs/deployment/nginx-vt-bulk-check-proxy-timeout.md)
and the snippet in [`docs/deployment/nginx/forgeforward-vt-bulk-check-api.conf.example`](docs/deployment/nginx/forgeforward-vt-bulk-check-api.conf.example).

After any nginx restore or new host setup:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Key deployment rules

- Bind uvicorn to `127.0.0.1` only — nginx handles external TLS termination and proxying.
- Store `VT_API_KEY` in a restricted `EnvironmentFile` (e.g. `/etc/dns-tool/secrets.env`, mode `0600`), not inline in the systemd unit or override file.
- Set `VT_USAGE_FILE` to a stable path outside `/tmp` so the daily usage counter survives reboots.
- Job results are in memory only and are lost on service restart. The latest completed job snapshot survives restarts (expires after 7 days).

### Rollback procedure

```bash
STAMP=<backup-timestamp>

# Restore backend router
cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-${STAMP} \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
sudo systemctl restart dns-tool-backend.service

# Restore frontend
cp -r /var/www/html/tools/vt-bulk-check.bak-${STAMP}/. \
      /var/www/html/tools/vt-bulk-check/

# Verify
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/vt-bulk-check/usage
```

Runtime history files (`vt_daily_history.json`, `vt_usage_history.jsonl`) are not used by pre-Batch-6 code and can be left in place after a rollback. Remove them only if their presence would cause confusion.

### Files that must never be committed

- Real `VT_API_KEY` values
- `/etc/dns-tool/secrets.env` (real file)
- `node_modules/`
- `dist/` (frontend build output)
- Python `venv/`
- `*.bak`, `*.bak-*` backup files
- Log files or `/tmp` files
- Runtime data files: `vt_usage.json`, `vt_latest_job.json`, `vt_daily_history.json`, `vt_usage_history.jsonl`

## Security and scope notes

- **Secrets**: Keep `VT_API_KEY` in an `EnvironmentFile` with `chmod 0600`, owned by the service user. Never commit real key values. Use `docs/deployment/systemd/secrets.env.example` as a template.
- **Network exposure**: The backend must only listen on `127.0.0.1`. External access goes through nginx with TLS.
- **Job data**: Results are held in memory for the lifetime of the process. No persistent database is used. Restarting the service clears all active jobs.
- **No authentication**: The tool assumes internal use. There is no login, session, or per-user access control.
- **Usage history scope**: The per-job JSONL and daily history files contain metadata and API request counts only. They do not store submitted domains, URLs, input values, result data, or user identity. Usage history is for API quota planning, not audit-grade logging.
- **Audit logging**: The backend logs VT API calls and job lifecycle events to the system journal. Exception class names (not full tracebacks) are written to usage history records.

## Out of scope / not implemented

The following are explicitly not part of this tool:

- No dashboard or charts
- No SQLite or any persistent database
- No per-user reporting or attribution
- No authentication or access control
- No automatic dual-lookup of both `http://` and `https://` variants
- No resume of in-progress (running) jobs after a service restart
- No full job result history (only the most recent completed job is retained)
- No alerting or scheduled reporting
- No date pickers, filters, or preview tables in the usage report UI

## License

Add a license file if/when you plan to distribute this repository.
