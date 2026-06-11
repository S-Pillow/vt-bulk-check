# VTFIX-02B — VT Bulk JSONL History Contract

**Branch:** `feature/vtfix-02b-vtbulk-history-contract`  
**Scope:** Future VT Bulk Check JSONL records only — no migration or backfill.

## Goal

Make newly appended VT Bulk Check usage history records attributable and
compatible with shared VT usage reporting (MDI dashboard preparation).

## JSONL record contract (new appends only)

Each VT Bulk job completion appends one metadata-only line to
`/var/lib/dns-tool/vt_usage_history.jsonl` via `_build_vt_bulk_usage_history_record`.

### Required attribution fields (VTFIX-02B)

| Field | Type | Value |
|---|---|---|
| `tool_name` | string | `"vt_bulk_check"` |
| `timestamp` | string | ISO-8601 UTC from `completed_at` |
| `quota_units_consumed` | int | Same as `actual_lookups` |

### Preserved legacy VT Bulk fields

| Field | Notes |
|---|---|
| `ts` | Unix float; retained for CSV export and backward compatibility |
| `job_id`, `status`, `submitted_at`, `completed_at` | Unchanged |
| `accepted_count`, `rejected_count`, `processed`, `total` | Unchanged |
| `url_count`, `domain_count` | Unchanged |
| `actual_lookups` | Unchanged |
| `estimated_requests`, `use_domain_reports`, `error_summary` | Unchanged |

Records must not contain raw domains, URLs, or per-item results.

## Readers

| Consumer | Behavior |
|---|---|
| `GET /api/vt-bulk-check/usage-history/export` | CSV includes new columns: `tool_name`, `timestamp`, `quota_units_consumed` |
| MDI dashboard (`filter_mdi_records`) | Still MDI-only until VTFIX-02C; skips non-`mdi` records |
| MDI `quota_writer` / shared flock | Unchanged |

## Out of scope

- Dashboard source filters (VTFIX-02C)
- Daily history schema unification (VTFIX-02D)
- Jun 10 backfill (VTFIX-02H)
- Migrating or rewriting existing JSONL lines

## Verification (non-quota)

```bash
cd /root/vt-bulk-check-repo/dns-tool/backend
python3 -m pytest tests/test_vt_usage_history_contract.py -q
python3 -m py_compile routers/vt_bulk_check.py
```

After a real job (quota-consuming — separate approval), confirm new JSONL line
has `tool_name`, `timestamp`, and `quota_units_consumed` via `stat` + field
names only (do not paste line contents into tickets).
