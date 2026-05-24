# Batch 2 Quota Guardrails
## VirusTotal Bulk Check Tool

| Field | Value |
|---|---|
| Date | 2026-05-24 |
| Tickets | SEC-04, ERR-01, QUOTA-04, QUOTA-02, QUOTA-01 |
| Branch | aipf/batch-2-quota-guardrails |
| Status | PENDING DEPLOYMENT — awaiting approval |

---

## Summary of Changes

| Ticket | File(s) | Change |
|---|---|---|
| SEC-04 | `dns-tool/backend/routers/vt_bulk_check.py` | Reject submissions whose estimated API request cost exceeds `DAILY_LOOKUPS_LIMIT` (500) |
| ERR-01 | `vt-bulk-check/frontend/src/App.jsx` | Append quota reset time to the 429 error banner |
| QUOTA-04 | `vt-bulk-check/frontend/src/App.jsx` | Add `estimatedRequests` (URL-multiplier-aware) and URL note |
| QUOTA-02 | `vt-bulk-check/frontend/src/App.jsx` | Add estimated run time display |
| QUOTA-01 | `vt-bulk-check/frontend/src/App.jsx`, `styles.css` | Add pre-submission soft/hard quota warnings |

All changes are additive. No existing features were removed or altered.

---

## Cost Model

| Item type | Estimated API requests |
|---|---|
| Domain | 1 |
| URL | up to 3 (conservative estimate) |

Examples at `DAILY_LOOKUPS_LIMIT = 500`:

| Input | Est. requests | Outcome |
|---|---|---|
| 250 domains | 250 | Allowed |
| 500 domains | 500 | Allowed (strong warning) |
| 501 domains | 501 | Rejected — HTTP 422 |
| 166 URLs | 498 | Allowed (strong warning) |
| 167 URLs | 501 | Rejected — HTTP 422 |
| 100 domains + 100 URLs | 400 | Allowed (warning if > remaining) |
| 200 domains + 101 URLs | 503 | Rejected — HTTP 422 |

---

## SEC-04 Detail

**Guard location:** After normalization and deduplication in the `submit` endpoint, before job creation.

**Logic:**
```python
estimated_requests = sum(3 if item_type == "url" else 1 for _, item_type, _, _ in accepted_items)
if estimated_requests > DAILY_LOOKUPS_LIMIT:
    raise HTTPException(status_code=422, detail="...")
```

**Constant used:** Existing `DAILY_LOOKUPS_LIMIT = 500` — no new constant introduced.

**422 message format:**
> "Submission is estimated to use {N} VirusTotal API requests. The daily quota limit is 500 requests. Domains count as 1 request and URLs may count as up to 3 requests. Please reduce or split your list."

---

## Frontend Changes Detail

### ERR-01
`quotaResetLabel` useMemo computes time until midnight UTC client-side.
Appended to existing 429 banner: `"Quota resets at 00:00 UTC — in X hr Y min."`
Sub-one-minute: `"in less than 1 minute"`.

### QUOTA-04
`isUrlItem(s)` helper: detects `http://` or `https://` prefix.
`estimatedRequests` useMemo: domains × 1 + URLs × 3 from deduplicated input.
`hasUrlItems` boolean: true when any URL detected.
URL note displayed below estimated lookups when `hasUrlItems` is true.

### QUOTA-02
`formatRunTime(seconds)` helper: formats to `~X sec`, `~X min`, or `~X hr Y min`.
`estimatedRunSeconds`: `estimatedRequests × (60 / rateLimitPerMin)`, guarded for zero.
Displayed below the URL note when list is non-empty.

### QUOTA-01
`dailyRemaining`: `dailyLookupsLimit - dailyLookupsUsed`, null if limit unknown.
`quotaWarnSoft`: `estimatedRequests > dailyRemaining AND <= dailyLookupsLimit`.
`quotaWarnHard`: `estimatedRequests > dailyLookupsLimit`.
Warnings displayed above Submit button. Neither tier blocks submission.
`.warnBanner` CSS class added (amber) for soft tier; hard tier reuses `.errorBanner` (red).

---

## Deployment Checklist (Do Not Execute Until Approved)

> Confirm nginx static root before step 4:
> ```bash
> grep -r "root\|alias" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | grep -i "vt-bulk-check\|tools"
> ```
> Expected: `/var/www/html/tools/vt-bulk-check/`

```
[ ] 1. Confirm merge is on main
        git checkout main && git pull origin main && git log --oneline -3

[ ] 2. Deploy backend router (only changed file)
        sudo cp /root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py \
            /var/www/dns-tool/backend/routers/vt_bulk_check.py

[ ] 3. Build frontend
        cd /root/vt-bulk-check-repo/vt-bulk-check/frontend
        npm run build
        # Confirm: exits 0, no errors

[ ] 4. Confirm frontend path and copy build
        ls -ld /var/www/html/tools/vt-bulk-check/
        sudo cp -r /root/vt-bulk-check-repo/vt-bulk-check/frontend/dist/* \
            /var/www/html/tools/vt-bulk-check/

[ ] 5. Restart backend service (required — backend router changed)
        sudo systemctl restart dns-tool-backend.service
        sleep 4
        systemctl is-active dns-tool-backend.service
        ss -tlnp | grep 8000   # Expected: 127.0.0.1:8000

[ ] 6. Verify API responds
        curl -s http://127.0.0.1:8000/api/vt-bulk-check/usage

[ ] 7. Smoke-test in browser
        - Submit 501 domains: confirm 422 cap message
        - Enter list > remaining quota: confirm soft warning
        - Enter 501+ est. requests: confirm hard warning
        - Enter URL item: confirm URL note and estimatedRequests
        - Verify run-time estimate appears
        - If 429 errors present: confirm reset time in banner
        - Submit normal batch, view results, export CSV — all unchanged
```

---

## Rollback

### Frontend only
```bash
git checkout <prev-hash> -- vt-bulk-check/frontend/src/App.jsx
git checkout <prev-hash> -- vt-bulk-check/frontend/src/styles.css
cd vt-bulk-check/frontend && npm run build
sudo cp -r dist/* /var/www/html/tools/vt-bulk-check/
# No service restart needed
```

### Backend router only
```bash
git checkout <prev-hash> -- dns-tool/backend/routers/vt_bulk_check.py
sudo cp /root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py \
    /var/www/dns-tool/backend/routers/vt_bulk_check.py
sudo systemctl restart dns-tool-backend.service
sleep 4
systemctl is-active dns-tool-backend.service
```

---

## Completion Notes

_(Fill in after production deployment is approved and verified.)_

| Field | Value |
|---|---|
| Date completed | |
| Executed by | |
| All verification steps passed | yes / no |
| Issues encountered | none / _(describe)_ |
| Rollback required | yes / no |
