# nginx Proxy Timeout — VT Bulk Check API

| Field | Value |
|---|---|
| Date | 2026-06-24 |
| Ticket | OPS-NGINX-01 (informal) |
| Status | **DEPLOYED** on production |
| Production host | `forgeforward.app` |
| nginx site file | `/etc/nginx/sites-available/forgeforward.app` |

---

## Problem

Operators saw **504 Gateway Time-out** from nginx when using **Refresh** or **Force Scan**
in VT Bulk Check. The HTML error page showed `nginx/1.24.0 (Ubuntu)`.

Nginx error log:

```
upstream timed out (110: Connection timed out) while reading response header from upstream
```

Affected routes:

- `POST /api/vt-bulk-check/refresh`
- `POST /api/vt-bulk-check/force-scan`

The FastAPI backend (`dns-tool-backend.service` on `127.0.0.1:8000`) continued
processing after nginx returned 504 — VT API calls and exports could still succeed
server-side while the browser showed failure.

## Root cause

The `location ^~ /api/vt-bulk-check/` block had **no** `proxy_read_timeout`.
nginx defaults to **60 seconds**.

VT Bulk Check operations can exceed 60s because:

- Internal rate limiter: **15 seconds minimum** between VT API calls (4/min)
- **Force Scan** may require multiple VT calls (submit + analyse)
- Quota lock wait or concurrent job activity can add delay

The MDI API block on the same site already uses `proxy_read_timeout 300s`; VT Bulk
Check did not.

## Fix applied (production)

Added timeout directives to the VT Bulk Check API location block:

```nginx
location ^~ /api/vt-bulk-check/ {
    proxy_pass         http://127.0.0.1:8000/api/vt-bulk-check/;
    proxy_http_version 1.1;

    proxy_set_header   Host             $host;
    proxy_set_header   X-Real-IP        $remote_addr;
    proxy_set_header   X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;

    proxy_connect_timeout 60s;
    proxy_send_timeout    60s;
    proxy_read_timeout    300s;
}
```

Sanitized snippet for copy/paste: [`nginx/forgeforward-vt-bulk-check-api.conf.example`](nginx/forgeforward-vt-bulk-check-api.conf.example)

## Deploy steps

Use when applying on a new host or after restoring nginx from an old backup.

```bash
# 1. Back up the live site file
STAMP=$(date +%Y%m%d-%H%M%S)
sudo cp /etc/nginx/sites-available/forgeforward.app \
    /etc/nginx/sites-available/forgeforward.app.bak-${STAMP}

# 2. Edit the VT Bulk Check API location block (see snippet above)
sudo nano /etc/nginx/sites-available/forgeforward.app

# 3. Validate and reload (no backend restart required)
sudo nginx -t && sudo systemctl reload nginx
```

## Verification

```bash
# Quick read-only smoke — should return 200
curl -sS -o /dev/null -w "usage HTTP %{http_code}\n" \
  https://forgeforward.app/api/vt-bulk-check/usage

curl -sS -o /dev/null -w "frontend HTTP %{http_code}\n" \
  https://forgeforward.app/tools/vt-bulk-check/

# Confirm timeout is present in live config
sudo grep -A12 'location \^~ /api/vt-bulk-check/' \
  /etc/nginx/sites-available/forgeforward.app | grep proxy_read_timeout
# Expected: proxy_read_timeout    300s;
```

Functional check (consumes VT quota — operator only):

- Run **Refresh** or **Force Scan** on a single row that previously returned 504
- Expect JSON response or application error — **not** nginx 504 HTML within ~60s

## Rollback

```bash
sudo cp /etc/nginx/sites-available/forgeforward.app.bak-YYYYMMDD-HHMMSS \
    /etc/nginx/sites-available/forgeforward.app
sudo nginx -t && sudo systemctl reload nginx
```

Rollback restores the 60s default and may reintroduce 504 on slow VT operations.

## Files in this repository

| Path | Purpose |
|---|---|
| `docs/deployment/nginx-vt-bulk-check-proxy-timeout.md` | This record |
| `docs/deployment/nginx/forgeforward-vt-bulk-check-api.conf.example` | Copy-paste nginx snippet |

## Files on the production host (not in git)

| Path | Notes |
|---|---|
| `/etc/nginx/sites-available/forgeforward.app` | Live nginx site config |
| `/etc/nginx/sites-enabled/forgeforward.app` | Symlink to sites-available |

The live nginx site file is **not** committed whole — only the VT Bulk Check API
snippet is versioned here. MDI and other location blocks belong to the same site
file but are documented separately in the MDI repository.

## Completion notes

| Field | Value |
|---|---|
| Date deployed | 2026-06-24 |
| nginx reload | `nginx -t` passed; `systemctl reload nginx` succeeded |
| Backend restart | Not required |
| Post-deploy smoke | `/api/vt-bulk-check/usage` and `/tools/vt-bulk-check/` returned 200 |
