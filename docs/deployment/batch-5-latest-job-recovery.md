# Batch 5 — Latest Completed Job Recovery

**Branch:** `aipf/batch-5-latest-job-recovery`
**Theme:** Latest Completed Job Recovery
**Status:** Implemented, pending merge and deployment

---

## Goal

Save the most recently completed job to disk so analysts can recover results
if the browser closes or the backend restarts after a job finishes.

---

## Tickets

### PERSIST-05A — Snapshot helpers, constants, JobState fields

**Backend file:** `dns-tool/backend/routers/vt_bulk_check.py`

**Added constants:**
```python
_LATEST_JOB_FILE = Path("/var/lib/dns-tool/vt_latest_job.json")
_LATEST_JOB_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days
_LATEST_JOB_ID: Optional[str] = None
```

**Added JobState fields:**
```python
submitted_at: Optional[float] = None
completed_at: Optional[float] = None
```

**Added helpers:**
- `_save_latest_job_snapshot(snapshot: Dict)` — atomic write (temp + os.replace), chmod 0600. Accepts a pre-built dict, never performs I/O under `_JOBS_LOCK`.
- `_delete_latest_job_snapshot()` — deletes the file, logs warning on failure, never raises.
- `_load_latest_job_snapshot()` — reads, validates schema_version, checks expiration. Deletes corrupt/expired/unknown-schema files to prevent repeat warnings. Never raises.
- `_reconstruct_job_from_snapshot(data)` — builds a `JobState` with `status="done"` and all auto-update fields reset to inactive defaults.

---

### PERSIST-05B — Save snapshot on completion

**Critical lock rule enforced:**

Under `_JOBS_LOCK`: set `job.status="done"`, `job.completed_at`, and copy a plain dict.
After releasing `_JOBS_LOCK`: call `_save_latest_job_snapshot(snapshot_dict)`.

Disk I/O never occurs while the lock is held. Write failures are logged but never propagate.

Also: `submitted_at = time.time()` is set when the job is created in the `submit` endpoint.

**Snapshot schema (schema_version=1):**
```json
{
  "schema_version": 1,
  "job_id": "...",
  "status": "done",
  "submitted_at": 1234567890.123,
  "completed_at": 1234567890.456,
  "processed": 10,
  "total": 10,
  "error_message": null,
  "use_domain_reports": false,
  "item_order": ["example.com", "http://example.org/"],
  "results_by_normalized": { "...": { "..." } },
  "rejected": []
}
```

---

### PERSIST-05C — Load snapshot on startup

Function `_startup_load_latest_job_sync()` is called at module import time (before any request is served).

Behavior:
1. Calls `_load_latest_job_snapshot()` — handles expiration and corruption.
2. Calls `_reconstruct_job_from_snapshot()` to rebuild a `JobState`.
3. Inserts the job into `_JOBS` and sets `_LATEST_JOB_ID`.
4. Any failure is caught and logged — never blocks backend startup.

---

### PERSIST-05D — Latest-job metadata endpoint

**Endpoint:** `GET /api/vt-bulk-check/latest-job`

**No-snapshot response:**
```json
{ "job_id": null }
```

**Valid-snapshot response:**
```json
{
  "job_id": "...",
  "status": "done",
  "total": 10,
  "processed": 10,
  "completed_at": 1234567890.456,
  "submitted_at": 1234567890.123,
  "rejected_count": 0
}
```

Does **not** return: `results_by_normalized`, `item_order`, `use_domain_reports`, full item list.

Endpoint logic:
1. If `_LATEST_JOB_ID` is set and the job is still in `_JOBS`, check expiration in memory. If expired: evict from `_JOBS`, delete file, return null.
2. If not in `_JOBS` (evicted), fall back to `_load_latest_job_snapshot()` from disk.
3. Reload the recovered job into `_JOBS` on fallback (so `GET /jobs/{id}` and export work).
4. Returns null if no valid snapshot.

---

### PERSIST-05E — Frontend recovery banner

**Files:** `vt-bulk-check/frontend/src/App.jsx`, `vt-bulk-check/frontend/src/styles.css`

**New state:**
- `latestJobMeta` — metadata from `/latest-job` endpoint
- `isRecoveredJob` — flag to show "Results recovered from a previous session." note
- `pendingExport` — triggers CSV download after recovered job loads

**On page load:** `fetchLatestJobMeta()` calls `/api/vt-bulk-check/latest-job?t=<cache-buster>` with `cache: 'no-store'`.

**Recovery banner** (shown below Run Check row, inside input card):
- Appears only when `latestJobMeta.job_id` exists AND no current job is loaded.
- Text: **"A previous completed job is available."** + item count + completed timestamp.
- **Load results** button: sets `jobId`, marks `isRecoveredJob=true`, dismisses banner.
- **Export CSV** button: same as Load results + sets `pendingExport=true`; CSV downloads automatically once the job finishes loading.

**Recovery note** (in results area): "Results recovered from a previous session." — shown when `isRecoveredJob && job?.status === 'done'`.

**On new submission:** Clears `latestJobMeta`, `isRecoveredJob`, `pendingExport`.

**CSS class `.recoveryBanner`:** subtle card-style border, flex row wrapping for small screens.

---

### PERSIST-05F — 7-day expiration enforcement

Expiration is enforced in three places:

| Location | Trigger | Action |
|---|---|---|
| `_load_latest_job_snapshot()` | Any read (startup, endpoint fallback) | Delete file if `age > 7 days`, return None |
| `GET /latest-job` in-memory path | Each request | Evict from `_JOBS`, delete file, return null |
| `_startup_load_latest_job_sync()` | Module import | Calls `_load_latest_job_snapshot()` → handled there |

Corrupt/unknown-schema files are deleted on first detection to prevent repeated warnings.

**`.gitignore` additions:**
```
vt_latest_job.json
vt_latest_job.tmp
```

---

## File Storage

| Property | Value |
|---|---|
| Path | `/var/lib/dns-tool/vt_latest_job.json` |
| Permissions | `0600` (owner only) |
| Write method | Atomic: temp file + `os.replace` |
| Max age | 7 days from `completed_at` |
| Owner | backend service user |

**Pre-deployment requirement:** `/var/lib/dns-tool/` must exist and be writable by the backend service user. Command to verify:
```bash
sudo -u <service-user> test -w /var/lib/dns-tool && echo "OK"
```

---

## Affected Files

| File | Change |
|---|---|
| `dns-tool/backend/routers/vt_bulk_check.py` | Constants, JobState fields, snapshot helpers, startup load, endpoint |
| `vt-bulk-check/frontend/src/App.jsx` | State, fetch on load, banner JSX, recovery note |
| `vt-bulk-check/frontend/src/styles.css` | `.recoveryBanner` and children |
| `.gitignore` | Add `vt_latest_job.json` and `vt_latest_job.tmp` |

---

## Deployment Checklist

### Pre-deployment
```bash
cd /root/vt-bulk-check-repo
git branch --show-current   # expect aipf/batch-5-latest-job-recovery or main after merge
git log --oneline -7
# Confirm service is active
systemctl is-active dns-tool-backend
# Confirm /var/lib/dns-tool/ exists and is writable
ls -la /var/lib/dns-tool/
```

### Backup
```bash
STAMP=$(date +%Y%m%d-%H%M%S)
cp /var/www/dns-tool/backend/routers/vt_bulk_check.py \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-${STAMP}
cp -r /var/www/html/tools/vt-bulk-check /var/www/html/tools/vt-bulk-check.bak-${STAMP}
echo "Backups: ${STAMP}"
```

### Build
```bash
cd /root/vt-bulk-check-repo/vt-bulk-check/frontend
npm run build
ls -la dist/
```

### Deploy backend
```bash
cp dns-tool/backend/routers/vt_bulk_check.py \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
python3 -m py_compile /var/www/dns-tool/backend/routers/vt_bulk_check.py && echo "OK"
systemctl restart dns-tool-backend
sleep 2
systemctl is-active dns-tool-backend
```

### Deploy frontend
```bash
cp -r /root/vt-bulk-check-repo/vt-bulk-check/frontend/dist/. \
   /var/www/html/tools/vt-bulk-check/
```

### Verify /var/lib/dns-tool/ directory
```bash
# If directory doesn't exist, create it with correct permissions:
# sudo mkdir -p /var/lib/dns-tool
# sudo chown <service-user>: /var/lib/dns-tool
# sudo chmod 750 /var/lib/dns-tool
ls -la /var/lib/dns-tool/
```

### Post-deployment verification
```bash
# Latest-job endpoint (no snapshot yet)
curl -s http://127.0.0.1:8000/api/vt-bulk-check/latest-job | python3 -m json.tool
# Expect: { "job_id": null }

# Run a small job (1-2 domains, requires quota)
# After it completes:
curl -s http://127.0.0.1:8000/api/vt-bulk-check/latest-job | python3 -m json.tool
# Expect: { "job_id": "...", "status": "done", ... }

# Check file exists with correct permissions
ls -la /var/lib/dns-tool/vt_latest_job.json
# Expect: -rw------- (0600)

# Restart backend, confirm recovery
systemctl restart dns-tool-backend
curl -s http://127.0.0.1:8000/api/vt-bulk-check/latest-job | python3 -m json.tool
# Expect: same job_id as before
```

---

## Rollback Plan

```bash
STAMP=<timestamp-from-backup>
# Restore backend router
cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-${STAMP} \
   /var/www/dns-tool/backend/routers/vt_bulk_check.py
systemctl restart dns-tool-backend
# Restore frontend
rm -rf /var/www/html/tools/vt-bulk-check
cp -r /var/www/html/tools/vt-bulk-check.bak-${STAMP} \
   /var/www/html/tools/vt-bulk-check
# Optionally delete snapshot file
# rm -f /var/lib/dns-tool/vt_latest_job.json
# Verify
curl -s http://127.0.0.1:8000/api/vt-bulk-check/usage
```

---

## Out of Scope

- Full job history
- SQLite
- Per-user recovery
- Authentication
- Running-job resume
- Partial error-job recovery
- Dashboard / reporting
- DELETE /latest-job endpoint
- Multiple snapshots
- Auto-loading recovered job on page load
- Banner auto-dismiss after timeout

---

## Completion Notes

*(Update this section after production deployment is verified.)*
