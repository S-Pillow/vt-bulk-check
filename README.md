# vt-bulk-check

Production-ready VirusTotal Bulk Check tool designed for security operations workflows: submit a list of domains and URLs, fetch detection summaries in a throttled batch, automatically re-check stale findings, and export results to CSV.

## What this tool does

- **Bulk check**: Paste domains and URLs, submit a job, and watch results stream in as the backend queries VirusTotal (VT) within your plan's rate limits.
- **Quota awareness**: Tracks daily API usage, shows remaining quota, estimates lookups before submission, and warns when a job may exhaust the daily allowance.
- **Operational safety**: Server-side throttling respects VT rate limits (4 requests/minute, 15-second minimum interval). Submissions estimated to exceed the configured daily quota limit are rejected before any VT call is made.
- **URL cost transparency**: URLs may use up to 3 VT API requests each (lookup + scan submission + report fetch). The UI shows an estimated request cost and flags URL-heavy submissions.
- **Estimated run time**: Before submitting, the UI shows an estimated completion time based on item count, URL vs domain mix, and the current rate limit.
- **Stale awareness**: Detects results older than a configured threshold and can automatically request a fresh scan and refresh results.
- **Bulk actions**: Rescan selected rows or rescan all stale rows in one click.
- **Error visibility**: Failed lookups are surfaced in the results table and in the CSV export. Rejected inputs (items that could not be parsed) are identified after submission. A clear message appears if a job disappears after a service restart.
- **Export**: Download a CSV with full result details including detection counts, ratio, last-scanned timestamp, status, and any error.

## Repository layout

```
dns-tool/backend/
    FastAPI backend. Hosts all VT endpoints under /api/vt-bulk-check/*.
    Handles job lifecycle, VT calls, throttling, quota tracking, stale
    detection, re-scan and refresh workflows, and CSV export.

vt-bulk-check/frontend/
    Standalone frontend (Vite + React).
    Dark-mode-first security operations console UI with quota warnings,
    estimated run time, CSV export, progress/status states, and bulk
    rescan UX.

docs/deployment/
    Batch implementation records and sanitized systemd/config templates.
    batch-1-infra-guardrails.md  — QUOTA-05, SCOPE-05, SEC-02
    batch-2-quota-guardrails.md  — SEC-04, QUOTA-01/02/04, ERR-01
    batch-3-error-visibility.md  — DATA-01/02, PERSIST-01/02/04
    systemd/                     — sanitized service and override templates
```

## Key features

### Job-based processing
- Submit a job with many inputs; the backend processes them sequentially under a rate limiter.
- The UI polls job status and progressively renders results.

### Quota tracking and pre-submission warnings
- The UI displays current daily usage and remaining quota.
- Before submitting, the estimated VT request cost is calculated: domains cost 1 request, URLs up to 3.
- An amber warning appears if the estimated cost exceeds remaining daily quota.
- A red warning appears if the estimated cost exceeds the total daily quota limit.
- The backend rejects submissions whose estimated cost exceeds the daily quota limit (HTTP 422) before any VT call is made.
- Warnings do not block submission; only the backend hard cap does.

### Error and recovery visibility
- Rejected inputs (items that failed normalization) are listed by value after submission.
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
Export job results as a CSV. Columns:
- `domain` — original input string
- `type` — `domain` or `url`
- `flagging` — malicious + suspicious engine count
- `total_engines` — total engines that responded
- `detection_ratio` — e.g. `2/91`
- `last_scanned` — formatted timestamp
- `status` — `OK`, `STALE`, or `ERROR`
- `error` — error detail if the lookup failed

## API

Base path: `/api/vt-bulk-check`

### Submit a job

`POST /submit`

```json
{ "items": ["example.com", "https://example.com/path"] }
```

Response:
```json
{ "job_id": "uuid", "total": 2, "accepted": 2, "rejected": [] }
```

- `rejected`: inputs that could not be parsed (e.g. dots-only strings, whitespace-only strings).
- Returns HTTP 422 if the estimated VT request cost exceeds `DAILY_LOOKUPS_LIMIT`.

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

### Export CSV

`GET /jobs/{job_id}/export`

Downloads a CSV with columns: `domain, type, flagging, total_engines, detection_ratio, last_scanned, status, error`.

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
- Domain lookups cost 1 VT request each. URL lookups may cost up to 3 VT requests each.

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

Key points:
- The backend runs as a systemd service behind nginx (see `docs/deployment/systemd/`).
- Bind uvicorn to `127.0.0.1` only — nginx handles external TLS termination and proxying.
- Store `VT_API_KEY` in a restricted `EnvironmentFile` (e.g. `/etc/dns-tool/secrets.env`, mode `0600`), not inline in the systemd unit or override file.
- Set `VT_USAGE_FILE` to a stable path outside `/tmp` so the daily usage counter survives reboots.
- Build the frontend with `npm run build` and copy `dist/` to the nginx static root.
- Job results are stored in memory only and are lost on service restart. Analysts should export results to CSV before restarting.

### Files that must never be committed

- Real `VT_API_KEY` values
- `/etc/dns-tool/secrets.env` (real file)
- `node_modules/`
- `dist/` (frontend build output)
- Python `venv/`
- `*.bak`, `*.bak-*` backup files
- Log files or `/tmp` files

## Security notes

- **Secrets**: Keep `VT_API_KEY` in an `EnvironmentFile` with `chmod 0600`, owned by the service user. Never commit real key values. Use `docs/deployment/systemd/secrets.env.example` as a template.
- **Network exposure**: The backend should only listen on `127.0.0.1`. External access goes through nginx with TLS.
- **Job data**: Results are held in memory for the lifetime of the process. No persistent database is used. Restarting the service clears all active jobs.
- **Audit logging**: The backend logs VT API calls and job lifecycle events. Do not log the `VT_API_KEY` value.

## License

Add a license file if/when you plan to distribute this repository.
