# VTFIX-02F — Shared VT Live Counter Coordination

**Status:** Implemented on branch `feature/vtfix-02f-shared-live-counter` — not deployed.

## Problem

After VTFIX-02D deploy, coordinated smoke showed:

- `vt_daily_history.json` — schema v2 correct (`tools.mdi` + `tools.vt_bulk_check`, `total_quota_units: 2`)
- `vt_usage.json` — `daily_lookups_used: 1` (undercount)
- Both usage endpoints reported `1`

## Root cause

VTFIX-02D replaced `vt_bulk_check.py` without the MDI-21A flock coordination:

- Process-global `_USAGE_STATE` cache
- Direct `write_text` without `fcntl.flock` on `vt_usage.lock`
- Stale in-memory state overwrote MDI's prior increment

MDI `quota_writer.increment_quota()` already uses exclusive flock + read-modify-write.

## Fix

Restore MDI-21A semantics in VT Bulk:

1. `quota_lock.py` — shared flock helper (mirrors MDI)
2. `_load_usage_state_unlocked()` / `_save_usage_state_unlocked()` — disk I/O only under caller-held flock
3. `_increment_usage()` — exclusive flock, re-read disk, increment, atomic `tmp → os.replace`
4. `_get_current_usage()` — shared flock read from disk (no stale cache)
5. Preserve VTFIX-02D `_record_daily_tool_usage()` additive daily-history v2 side-effects

## Tests

| File | Coverage |
|------|----------|
| `tests/test_quota_cache.py` | Stale cache, MDI-then-VT, endpoint keys, daily-history v2 coexistence |
| `tests/test_shared_live_counter.py` | Sequential MDI↔VT increments to count 2 |
| `tests/test_vt_daily_history_schema.py` | Unchanged — daily-history v2 |

All tests use `tmp_path` only.

## Deploy notes (when approved)

Deploy **only** VT Bulk backend files:

- `dns-tool/backend/quota_lock.py`
- `dns-tool/backend/routers/vt_bulk_check.py`

Restart `dns-tool-backend.service` in the same maintenance window as any MDI quota changes (MDI code unchanged in this ticket).

Do **not** hand-edit `vt_usage.json` to reconcile live counter vs daily-history totals.

## Smoke anomaly reference

VTFIX-02D minimal smoke (2026-06-11): one MDI job + one VT Bulk job consumed 2 VT API calls; daily-history recorded 2; live counter recorded 1.
