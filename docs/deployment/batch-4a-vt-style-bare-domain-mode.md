# Batch 4A — VirusTotal-Style Bare-Domain Lookup Behavior

## Summary

Batch 4A changes the default lookup behavior for bare domain inputs (e.g. `example.com`)
to match how the VirusTotal website handles them. Previously, bare domains were always
checked as VT domain records. After Batch 4A, bare domains default to URL reports
(`http://domain/`), closing the discrepancy where the tool and the VT website would
show different vendor counts and detection results for the same input.

A batch-level checkbox allows analysts who specifically need domain records to opt back in.

**Branch:** `aipf/batch-4a-vt-style-bare-domain-mode`
**Merge commit:** (fill in after merge)
**Deployed:** (fill in after deployment)

---

## Approved Behavior

### Checkbox OFF (default — URL reports for bare domains)

| Input | Type | checked_as in CSV | Notes |
|---|---|---|---|
| `example.com` | `url` | `http://example.com/` | New default behavior |
| `www.example.com` | `url` | `http://www.example.com/` | New default behavior |
| `http://example.com/` | `url` | `http://example.com/` | Unchanged |
| `https://example.com/` | `url` | `https://example.com/` | Unchanged — never downgraded |
| `example.com/path` | `url` | `https://example.com/path` | Unchanged |
| `www.example.com/path` | `url` | `https://www.example.com/path` | Unchanged |
| `sub.example.com/path?a=1` | `url` | normalized URL | Unchanged |
| `example.com` + `http://example.com/` | `url` × 1 | `http://example.com/` | Deduplicated to 1 item |

### Checkbox ON (domain reports for bare domains)

| Input | Type | checked_as in CSV | Notes |
|---|---|---|---|
| `example.com` | `domain` | `example.com` | Pre-Batch-4A behavior |
| `www.example.com` | `domain` | `www.example.com` | Pre-Batch-4A behavior |
| `http://example.com/` | `url` | `http://example.com/` | Unchanged |
| `https://example.com/` | `url` | `https://example.com/` | Unchanged |
| `example.com/path` | `url` | `https://example.com/path` | Unchanged |
| `example.com` + `http://example.com/` | domain + url × 2 | respective targets | Two separate VT objects |

---

## Quota Impact

| Scenario | Estimated requests | Outcome |
|---|---|---|
| 1 bare domain, checkbox OFF | 3 | Allowed |
| 10 bare domains, checkbox OFF | 30 | Allowed |
| 166 bare domains, checkbox OFF | 498 | Allowed, strong quota warning |
| 167 bare domains, checkbox OFF | 501 | **Rejected (SEC-04, 422)** |
| 1 bare domain, checkbox ON | 1 | Allowed |
| 500 bare domains, checkbox ON | 500 | Allowed (at limit) |
| 501 bare domains, checkbox ON | 501 | **Rejected (SEC-04, 422)** |

Analysts with large bare-domain batches should use checkbox ON to preserve
domain-report behavior at lower quota cost.

---

## Files Changed

| File | Tickets | Description |
|---|---|---|
| `dns-tool/backend/routers/vt_bulk_check.py` | LOOKUP-01, 02, 03 | `SubmitRequest.use_domain_reports`, `_normalize_item` bare-domain branch, `JobState.use_domain_reports`, `submit`/`refresh`/`force-scan` call sites |
| `vt-bulk-check/frontend/src/App.jsx` | LOOKUP-04, 05, 06 | `useDomainReports` state, checkbox JSX, submit body, `estimatedRequests` useMemo, bare-domain note |

`vt-bulk-check/frontend/src/styles.css` — not changed (no new CSS classes required).

---

## Backend Changes Detail

### `_normalize_item` new parameter

```python
def _normalize_item(raw: str, use_domain_reports: bool = False):
    # ... existing http/https and path handling unchanged ...
    # New bare-domain branch:
    if use_domain_reports:
        return domain, "domain", domain          # unchanged pre-4A behavior
    url_target = f"http://{domain}/"
    return url_target, "url", url_target          # new default
```

The return value `normalized = url_target` (e.g. `"http://example.com/"`) ensures
deduplication key equality: both `"example.com"` and `"http://example.com/"` produce
key `"http://example.com/"` in checkbox-OFF mode, naturally deduplicating them.

### Call sites updated

| Call site | Change |
|---|---|
| `submit` normalization loop | `_normalize_item(raw, req.use_domain_reports)` |
| `submit` — `JobState` creation | `use_domain_reports=req.use_domain_reports` |
| `refresh` endpoint | Fetches job first; passes `job.use_domain_reports` |
| `force-scan` endpoint | Fetches job first; passes `job.use_domain_reports` |

### Call sites NOT changed (safe — use stored data, not re-normalization)

- `_process_job` — uses stored `item_type` and `normalized_full_url`
- `refresh-stale` — uses stored `item_type` and `normalized_full_url`
- `force-scan-stale` — uses stored `item_type` and `normalized_full_url`
- `force-scan-bulk` — uses stored `item_type` and `normalized_full_url`
- `_auto_update_stale_job` — uses stored `item_type` and `normalized_full_url`

### `use_domain_reports` not exposed in API

`GET /api/vt-bulk-check/jobs/{job_id}` does not include `use_domain_reports`
in its response. It is stored internally on `JobState` only.

---

## Frontend Changes Detail

### `estimatedRequests` useMemo

```js
for (const item of unique) {
  if (isUrlItem(item)) {
    total += 3; urlCount++;            // explicit URL — always 3
  } else if (useDomainReports) {
    total += 1;                        // checkbox ON: bare domain → domain report
  } else {
    total += 3; urlCount++;            // checkbox OFF: bare domain → URL report
  }
}
```

### Checkbox placement

Visible in the main input card, between the textarea and the estimate block.
Always visible — not hidden inside Advanced. Default: unchecked. Not persisted
in localStorage. Not reset after job completion.

---

## Deployment Steps

Because this batch changes both the backend and frontend, both must be deployed
together (or backend first, then frontend).

```
PRE-DEPLOYMENT
[ ] Confirm repo on main, working tree clean
[ ] Confirm merge commit is present
[ ] Confirm service active: systemctl is-active dns-tool-backend.service
[ ] Confirm API responds: curl http://127.0.0.1:8000/api/vt-bulk-check/usage

BACKUP
[ ] cp /var/www/dns-tool/backend/routers/vt_bulk_check.py
       /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-YYYYMMDD-HHMMSS
[ ] cp -r /var/www/html/tools/vt-bulk-check/
          /var/www/html/tools/vt-bulk-check.bak-YYYYMMDD-HHMMSS/

BUILD FRONTEND
[ ] cd /root/vt-bulk-check-repo/vt-bulk-check/frontend && npm run build
[ ] Verify dist/ exists and is fresh
[ ] Confirm "Use domain reports" in built JS

DEPLOY BACKEND
[ ] cp /root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py
       /var/www/dns-tool/backend/routers/vt_bulk_check.py
[ ] python3 -m py_compile /var/www/dns-tool/backend/routers/vt_bulk_check.py
[ ] sudo systemctl restart dns-tool-backend.service
[ ] Confirm service active and 127.0.0.1:8000 bound

DEPLOY FRONTEND
[ ] cp -r dist/. /var/www/html/tools/vt-bulk-check/
[ ] Confirm index.html references new asset hashes

POST-DEPLOYMENT VERIFICATION
[ ] Page loads (HTTP 200)
[ ] "Use domain reports for bare domains" checkbox visible, unchecked by default
[ ] 1 bare domain, checkbox OFF → Estimated API requests: 3
[ ] 1 bare domain, checkbox ON → Estimated API requests: 1
[ ] 167 bare domains, checkbox OFF → quota hard-warn + 422 rejection (0 quota consumed)
[ ] 501 bare domains, checkbox ON → 422 rejection (0 quota consumed)
[ ] https:// URL never converted to http://
[ ] Small live job (2–3 real domains), checkbox OFF → type=url, checked_as=http://domain/
[ ] Small live job (2–3 real domains), checkbox ON → type=domain, checked_as=domain
[ ] CSV headers: input,checked_as,type,flagging,total_engines,detection_ratio,last_scanned,status,error
[ ] Export CSV button (single, results-area) still works
[ ] Batch 2 quota warnings still appear
[ ] Temporary-results notice still appears
```

---

## Rollback Steps

```bash
# Restore backend
cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-YYYYMMDD-HHMMSS \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
sudo systemctl restart dns-tool-backend.service

# Restore frontend
rm -rf /var/www/html/tools/vt-bulk-check/
cp -r /var/www/html/tools/vt-bulk-check.bak-YYYYMMDD-HHMMSS/ \
      /var/www/html/tools/vt-bulk-check/

# Verify
# - bare domains return type=domain after new submission
# - checkbox is gone from UI
# - CSV export still works
```

---

## Completion Notes

*(Fill in after deployment)*

- Deployed by:
- Deployed at:
- Verification result:
- Issues found:
- Rollback needed: no / yes — reason:
