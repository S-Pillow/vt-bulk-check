# vt-bulk-check

Production-ready VirusTotal Bulk Check tool designed for security operations workflows: submit a list of domains/URLs, fetch detection summaries in a throttled batch, automatically re-check stale findings, and export results to CSV.

## What this tool does

- **Bulk check**: Paste domains and URLs, submit a job, and watch results stream in as the backend queries VirusTotal (VT).
- **Operational safety**: Uses server-side throttling to respect VT rate limits.
- **Stale awareness**: Detects “stale” results (older than a configured threshold) and can automatically request a fresh scan and refresh results.
- **Bulk actions**: Rescan selected rows or rescan all stale rows.
- **Export**: Download a CSV with `domain,flagging` (where *flagging* = malicious + suspicious engines).

## Repository layout

- `dns-tool/backend/`
  - FastAPI backend hosting VT endpoints under `/api/vt-bulk-check/*`.
  - Implements job lifecycle, VT calls, throttling, stale detection, re-scan and refresh workflows, and CSV export.
- `vt-bulk-check/frontend/`
  - Standalone frontend (Vite + React) used for `/tools/vt-bulk-check/`.
  - Dark-mode-first “Security Operations Console” UI with CSV export, progress/status states, and bulk rescan UX.

## Key features (Ops workflow)

- **Job-based processing**
  - Submit a job with many inputs; the backend processes them sequentially under a limiter.
  - The UI polls job status and progressively renders results.

- **Clear severity and status**
  - Results include malicious/suspicious counts, a flagging total, ratio, and “last scanned” timestamp.
  - UI emphasizes operational meaning (Clean / Flagged / Malicious) and highlights stale rows.

- **Auto re-check stale results**
  - When enabled, stale items trigger an automated pipeline:
    1) request a new scan on VT  
    2) refresh reports until VT returns updated results  
    3) update the UI table (no manual refresh required)

- **CSV export**
  - Export job results as a CSV for reporting or downstream enrichment.

## API (backend)

Base path: `/api/vt-bulk-check`

### Submit a job

`POST /submit`

Body:

```json
{ "items": ["example.com", "https://example.com/path"] }
```

Response:

```json
{ "job_id": "uuid", "total": 10, "accepted": 10, "rejected": [] }
```

### Read job status/results

`GET /jobs/{job_id}`

Response fields include:
- `status`: `running | done | error`
- `processed`, `total`
- `results`: array of results (one per input)
- `update`: progress for the auto stale re-check pipeline (when used)

### Export CSV

`GET /jobs/{job_id}/export`

Downloads a CSV with:
- `domain`: original input string
- `flagging`: `flagging_engines` (malicious + suspicious)

### Refresh a single item (fetch latest report)

`POST /refresh`

```json
{ "job_id": "uuid", "item": "example.com" }
```

### Request a new scan (reanalyze) for a single item

`POST /force-scan`

```json
{ "job_id": "uuid", "item": "example.com", "type": "domain" }
```

### Refresh all stale items (fetch latest reports)

`POST /refresh-stale`

```json
{ "job_id": "uuid" }
```

### Request new scans for all stale items

`POST /force-scan-stale`

```json
{ "job_id": "uuid" }
```

### Request new scans for selected items (bulk)

`POST /force-scan-bulk`

```json
{ "job_id": "uuid", "normalized_items": ["example.com", "https://example.com/path"] }
```

### Auto-update stale results end-to-end

`POST /jobs/{job_id}/auto-update-stale`

This runs the complete “stale re-check pipeline” (scan + refresh) server-side and exposes progress via `GET /jobs/{job_id}`.

```json
{ "job_id": "uuid" }
```

## Configuration

### Required environment variables

- `VT_API_KEY`: VirusTotal API key with permissions to query and (optionally) request reanalysis.

### Throttling / rate limiting

The backend uses a minimum-interval limiter to reduce the risk of hitting VT API rate limits. Depending on your VT plan, you may need to tune throttling and/or batch sizes.

## Running locally (development)

### Backend

From `dns-tool/backend/` (example; adjust for your environment):

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt  # if/when you add one
export VT_API_KEY="..."
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend

From `vt-bulk-check/frontend/`:

```bash
npm install
npm run dev
```

The frontend expects the backend VT endpoints at `/api/vt-bulk-check/*` (typically served behind nginx in production).

## Deployment notes (production)

- The backend is typically run as a systemd service (or similar) behind nginx.
- The standalone frontend is built with Vite and served as static assets.
- Ensure you do not deploy or commit:
  - `.env` files
  - `node_modules/`
  - `dist/`
  - Python `venv/`

## Security & compliance

- **Do not commit secrets**: keep VT keys in environment variables or a secrets manager.
- **Audit logging**: consider adding structured logging for operational traceability (without logging secrets).
- **Least privilege**: use VT credentials with minimum scopes required for your workflow.

## License

Add a license file if/when you plan to distribute this repository.

