# Batch 3 — Error Visibility and Recovery Clarity
## VirusTotal Bulk Check Tool

**Branch:** `aipf/batch-3-error-visibility`
**Plan date:** 2026-05-24
**Implementation date:** 2026-05-24
**Status:** IMPLEMENTED — pending review and deployment approval

---

## Goal

Improve analyst trust by making rejected inputs, job-level fatal errors, temporary storage
limitations, and post-completion export guidance clearly visible in the UI.
No core workflow changes. No schema changes. No database. No authentication.

---

## Tickets Implemented

### DATA-02 — Expose job-level fatal error in API; show safe message in UI

**Files changed:**
- `dns-tool/backend/routers/vt_bulk_check.py`
- `vt-bulk-check/frontend/src/App.jsx`

**Backend change:**
Added `"error_message": job.error_message` to the `get_job` response dict.
`job.error_message` is `null` for normal jobs; contains the exception string for `status: "error"` jobs.
Exposed for maintainer debugging via the API. Not rendered in the UI.

**Frontend change:**
When `job.status === "error"`, renders a fixed safe banner:
> "The job encountered an unexpected error. Please retry or contact the tool maintainer if the issue continues."
Raw `error_message` value is never displayed to analysts.

**Verification:**
- `error_message: null` present in `GET /api/vt-bulk-check/jobs/{id}` response for done jobs.
- Safe fixed message renders on `status: "error"`. Normal jobs unaffected.

---

### DATA-01 — Display rejected items after submission

**Files changed:**
- `vt-bulk-check/frontend/src/App.jsx`

**Change:**
Added `rejectedItems` state. Populated from `data.rejected` in `onSubmit` response.
Reset to `[]` at start of each new submission. Renders a persistent notice listing
rejected inputs when `rejectedItems.length > 0`. No dismiss button.

**Backend:** No change. `rejected` was already returned by `/submit`.

**Reliable test input from the textarea:**
- `"..."` (three dots) — passes frontend `splitInputs` (truthy) but `_normalize_item`
  strips dots to `""` → `ValueError("Invalid domain")`.
- Any dots-only string (`.`, `..`, `...`) triggers rejection.

**Verification:**
- Submit `google.com`, `...`, `example.com` → results show 2 rows, notice lists `"..."`.
- New submission clears the notice immediately.
- Clean submission (all valid) → no notice.

---

### PERSIST-04 — Show contextual message when job returns 404 after restart

**Files changed:**
- `vt-bulk-check/frontend/src/App.jsx`

**Change:**
`fetchJob` now checks `resp.status === 404` explicitly before the generic non-ok check.
On 404, throws a typed error: `Object.assign(new Error('Job not found'), { isJobGone: true })`.
Polling loop catch block checks `e.isJobGone`: if true, stops polling and sets `jobGone = true`.
All non-404 errors continue through the existing generic `setError` path unchanged.
`jobGone` is reset to `false` at start of each new submission.

Renders when `jobGone === true`:
> "This job is no longer available. Results are stored in memory only and are lost when
> the service restarts. If you exported the CSV before this happened, your data is safe."

**Verification:**
- `curl http://127.0.0.1:8000/api/vt-bulk-check/jobs/00000000-0000-0000-0000-000000000000`
  returns HTTP 404 (confirms backend path is correct).
- Code review confirms `resp.status === 404` branch in `fetchJob` and `e.isJobGone` check
  in polling catch.
- After deployment: restart service with active job in browser → `jobGone` message appears.
- Network errors and 500s do not trigger `jobGone`.

---

### PERSIST-01 — Add temporary-results notice when job completes

**Files changed:**
- `vt-bulk-check/frontend/src/App.jsx`
- `vt-bulk-check/frontend/src/styles.css`

**Change:**
Added `.infoNote` CSS class (blue `--accent2` palette — neutral, not amber or red).
Renders a notice when `job.status === "done"`:
> "Results are temporary — export before closing this tab or restarting the service."

Not shown while running, not shown on error jobs, not shown before submission.
Clears when a new submission begins and `job` resets to `null`.

**Verification:**
- Submit 2–3 domains → job completes → notice visible.
- While job is running → notice absent.
- Before any submission → notice absent.
- `status: "error"` → notice absent (DATA-02 banner shown instead).

---

### PERSIST-02 — Add prominent secondary Export CSV button when job completes

**Files changed:**
- `vt-bulk-check/frontend/src/App.jsx`
- `vt-bulk-check/frontend/src/styles.css`

**Change:**
Added `.btnSecondary` CSS class — outlined blue, transparent background.
Visually distinct from the filled teal `.btnPrimary` ("Run check") — secondary weight,
not competing with the primary action.

Renders a `btn btnSecondary` Export CSV button directly above the results table
when `job.status === "done"`. Calls the same `downloadCsv` handler as the existing
small export button in the header row. The original small button is untouched.

Not shown while running, not shown before submission.

**Verification:**
- After job completes → prominent outlined Export CSV button visible above results.
- Click → same CSV download as original button.
- Original small export button still works independently.
- While running → prominent button absent.

---

## Commit Log

```
df34c96  feat: expose job error_message in API and show safe message in UI (DATA-02)
660a01e  feat: display rejected items after submission (DATA-01)
5b57dd9  feat: show contextual message when job returns 404 after restart (PERSIST-04)
137ce8e  feat: add temporary-results notice when job completes (PERSIST-01)
f43a4e9  feat: add prominent secondary Export CSV button when job completes (PERSIST-02)
```

---

## Files Changed in This Batch

| File | Tickets |
|---|---|
| `dns-tool/backend/routers/vt_bulk_check.py` | DATA-02 (one line) |
| `vt-bulk-check/frontend/src/App.jsx` | DATA-02, DATA-01, PERSIST-04, PERSIST-01, PERSIST-02 |
| `vt-bulk-check/frontend/src/styles.css` | PERSIST-01 (`.infoNote`), PERSIST-02 (`.btnSecondary`) |

---

## What Was Not Changed

- CSV export format — unchanged
- VirusTotal API behavior — unchanged
- Batch 2 quota logic — unchanged
- Batch 1 systemd configuration — unchanged
- Core submit / results / refresh / force-scan workflow — unchanged
- No authentication, database, or redesign

---

## Production Deployment Notes

See the deployment plan in the corresponding plan document.

**Backend router to copy:**
```
/root/vt-bulk-check-repo/dns-tool/backend/routers/vt_bulk_check.py
→ /var/www/dns-tool/backend/routers/vt_bulk_check.py
```

**Frontend:** Build from `vt-bulk-check/frontend/` and copy `dist/*` to
`/var/www/html/tools/vt-bulk-check/` (confirmed nginx alias).

**Service restart required** because backend router changed.

---

## Rollback

Restore timestamped backups created at deployment time:
```bash
sudo cp /var/www/dns-tool/backend/routers/vt_bulk_check.py.bak-<STAMP> \
    /var/www/dns-tool/backend/routers/vt_bulk_check.py
sudo systemctl restart dns-tool-backend.service

sudo rm -rf /var/www/html/tools/vt-bulk-check/assets
sudo rm -f /var/www/html/tools/vt-bulk-check/index.html
sudo cp -r /var/www/html/tools/vt-bulk-check.bak-<STAMP>/. \
    /var/www/html/tools/vt-bulk-check/
```
