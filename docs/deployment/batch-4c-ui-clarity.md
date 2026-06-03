# Batch 4C — UI Clarity, Tooltip Improvements, and Duplicate Export CSV Cleanup

## Summary

Batch 4C improves analyst-facing clarity in the pre-submission area and removes
a redundant Export CSV button without changing any backend behavior, CSV format,
lookup logic, or quota logic.

**Branch:** `aipf/batch-4c-ui-clarity`
**Merge commit:** (fill in after merge)
**Deployed:** (fill in after deployment)

---

## Tickets

### UI-01 — Rename "Estimated lookups" to "Unique items"

**File:** `vt-bulk-check/frontend/src/App.jsx`

The label "Estimated lookups" was renamed to "Unique items" to accurately
describe what is being counted (distinct parsed inputs, not API requests).
The stale-rescan companion note was updated from "additional lookups" to
"additional API requests" for consistent terminology.

No variable renames. CSS class `estimatedLookups` is unchanged.

---

### UI-02 — Improve estimated API request help text

**File:** `vt-bulk-check/frontend/src/App.jsx`

Three changes:

1. **Tooltip on "Unique items" span** updated to:
   > "Unique parsed inputs. Domain reports use 1 API request. URL reports use
   > 1 request if VirusTotal already has the exact URL, or up to 3 if it has
   > not. Refresh and new scan actions use additional requests."

2. **Render condition** for the estimated API requests line changed from
   `{hasUrlItems && ...}` to `{estimatedRequests > 0 && ...}` so the line
   appears for all non-empty input, not only when URL-type items are detected.

3. **Estimated API requests line text** updated to:
   > "Estimated API requests: N — URL items may cost up to 3 requests each
   > (1 if VirusTotal already has the exact URL, up to 3 if it does not).
   > Domain items cost 1 request each."

`Est. run time` line is unaffected.

---

### UI-03 — Clarify current domain and URL record behavior

**File:** `vt-bulk-check/frontend/src/App.jsx`

Added a small informational note below the estimated API requests line:

> "Bare domains are currently checked as domain records in this tool.
> VirusTotal's website may show the URL record by default."

This note explains the known discrepancy (e.g. tool shows 91 vendors, VT
website shows 92) without changing any behavior. It will be updated or
removed when Batch 4A ships URL-mode bare-domain lookup.

The note is rendered when `estimatedLookups > 0` (same guard as the
surrounding block) so it only appears when items are entered.

---

### UI-04/UI-05 — Remove duplicate Export CSV action

**Files:** `vt-bulk-check/frontend/src/App.jsx`,
`vt-bulk-check/frontend/src/styles.css`

**Removed:** The header-row `smallBtn exportBtn` button (teal-accented,
appeared inline with the Job UUID in the Run Check row whenever a `jobId`
existed).

**Kept:** The results-area `btn btnSecondary` button (blue-outlined, appears
only when `job.status === 'done'`, directly below the PERSIST-01
temporary-results notice).

**Why the results-area button was kept:**
- Sits immediately below the "Results are temporary" PERSIST-01 notice,
  forming a natural notice → action unit from Batch 3
- Appears only on completion — when export is most relevant
- Greater visual weight than the small header-row button
- The PERSIST-01 + Export CSV unit from Batch 3 is preserved intact

**`disabled` prop on remaining button** updated from `disabled={exporting}` to
`disabled={exporting || !jobId}` as a defensive guard (jobId is always set
when status is done, but this makes behavior explicit).

**CSS cleanup:** The `.exportBtn`, `.exportBtn:hover:not(:disabled)`, and
`.exportBtn:focus-visible` rules removed from `styles.css` (22 lines).
`.btnSecondary` is unchanged.

---

## Files Changed

| File | Change |
|---|---|
| `vt-bulk-check/frontend/src/App.jsx` | Label rename, tooltip, API requests note, bare-domain note, export button removal |
| `vt-bulk-check/frontend/src/styles.css` | `.exportBtn` CSS block removed |

**Files not changed:** `vt_bulk_check.py`, `package.json`, `package-lock.json`,
all Batch 1–4B docs.

---

## UI State After Batch 4C

**Pre-submission area (when items are entered):**
1. Unique items: N  *(tooltip explains cost model)*
2. + up to N additional API requests (stale rescans)  *(only if auto-rescan on)*
3. Estimated API requests: N — ...explanation...
4. Bare domains are currently checked as domain records in this tool...
5. Est. run time: ~N min

**Post-completion results area:**
1. Results are temporary — export before closing this tab or restarting the service.
2. [Export CSV]  *(single button — btnSecondary)*
3. Error summary banner (if errors)
4. Results table

---

## Deployment Steps

1. Build the frontend:
   ```
   cd /root/vt-bulk-check-repo/vt-bulk-check/frontend
   npm run build
   ```

2. Backup current frontend static directory:
   ```
   TIMESTAMP=$(date +%Y%m%d-%H%M%S)
   cp -r /var/www/html/tools/vt-bulk-check/ \
         /var/www/html/tools/vt-bulk-check.bak-${TIMESTAMP}/
   ```

3. Copy dist contents to production:
   ```
   cp -r dist/. /var/www/html/tools/vt-bulk-check/
   ```

4. Verify page loads and UI changes are visible.

**No backend restart required.** Frontend-only change.

---

## Rollback Steps

1. Restore frontend static backup:
   ```
   rm -rf /var/www/html/tools/vt-bulk-check/
   cp -r /var/www/html/tools/vt-bulk-check.bak-YYYYMMDD-HHMMSS \
         /var/www/html/tools/vt-bulk-check/
   ```

2. Verify old UI returns in browser.

No backend rollback required.

---

## Completion Notes

*(Fill in after deployment)*

- Deployed by:
- Deployed at:
- Verification result:
- Issues found:
- Rollback needed: no / yes — reason:
