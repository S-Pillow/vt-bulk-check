"""
Shared VT daily history schema (VTFIX-02D).

Unified per-day shape:
  {
    "YYYY-MM-DD": {
      "schema_version": 2,
      "total_quota_units": 0,
      "tools": {
        "mdi": {"quota_units": 0, "jobs": 0, "items_processed": 0},
        "vt_bulk_check": {"quota_units": 0, "jobs": 0, "items_processed": 0},
        "legacy": {"quota_units": 0, "jobs": 0, "items_processed": 0}
      }
    }
  }

Legacy shapes normalized at read/write time:
  - integer day value → legacy tool bucket (not MDI)
  - {"quota_units": N, "tool": "mdi"} → mdi bucket
  - partial unified dict → merged safely
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 2
DAILY_HISTORY_MAX_DAYS = 90

TOOL_MDI = "mdi"
TOOL_VT_BULK = "vt_bulk_check"
TOOL_LEGACY = "legacy"
KNOWN_TOOLS = frozenset({TOOL_MDI, TOOL_VT_BULK, TOOL_LEGACY})


def _empty_tool_stats() -> Dict[str, int]:
    return {"quota_units": 0, "jobs": 0, "items_processed": 0}


def _empty_day_entry() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "total_quota_units": 0,
        "tools": {},
    }


def _coerce_tool_name(tool: Optional[str]) -> str:
    if tool in (TOOL_MDI, TOOL_VT_BULK):
        return tool
    return TOOL_LEGACY


def _sum_tool_quota(tools: Dict[str, Any]) -> int:
    total = 0
    for stats in tools.values():
        if isinstance(stats, dict):
            total += int(stats.get("quota_units", 0) or 0)
    return total


def normalize_day_entry(raw: Any) -> Dict[str, Any]:
    """Normalize one day entry from any legacy or unified shape."""
    if raw is None:
        return _empty_day_entry()

    if isinstance(raw, (int, float)):
        quota = int(raw)
        return {
            "schema_version": SCHEMA_VERSION,
            "total_quota_units": quota,
            "tools": {TOOL_LEGACY: {**_empty_tool_stats(), "quota_units": quota}},
        }

    if not isinstance(raw, dict):
        return _empty_day_entry()

    if "tools" in raw and isinstance(raw.get("tools"), dict):
        tools: Dict[str, Any] = {}
        for name, stats in raw["tools"].items():
            tool_key = _coerce_tool_name(str(name))
            if not isinstance(stats, dict):
                continue
            merged = _empty_tool_stats()
            merged["quota_units"] = int(stats.get("quota_units", 0) or 0)
            merged["jobs"] = int(stats.get("jobs", 0) or 0)
            merged["items_processed"] = int(stats.get("items_processed", 0) or 0)
            if tool_key in tools:
                for field in merged:
                    tools[tool_key][field] += merged[field]
            else:
                tools[tool_key] = merged
        total = int(raw.get("total_quota_units", 0) or 0)
        if total <= 0:
            total = _sum_tool_quota(tools)
        return {
            "schema_version": SCHEMA_VERSION,
            "total_quota_units": total,
            "tools": tools,
        }

    if "quota_units" in raw:
        tool_key = _coerce_tool_name(raw.get("tool") or raw.get("tool_name"))
        quota = int(raw.get("quota_units", 0) or 0)
        tools = {
            tool_key: {
                **_empty_tool_stats(),
                "quota_units": quota,
                "jobs": int(raw.get("jobs", 0) or 0),
                "items_processed": int(raw.get("items_processed", 0) or 0),
            }
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "total_quota_units": quota,
            "tools": tools,
        }

    return _empty_day_entry()


def extract_day_quota_units(entry: Any) -> int:
    """Return shared daily quota units for dashboard trend reads."""
    normalized = normalize_day_entry(entry)
    total = int(normalized.get("total_quota_units", 0) or 0)
    if total > 0:
        return total
    return _sum_tool_quota(normalized.get("tools", {}))


def record_tool_usage(
    data: Dict[str, Any],
    date_str: str,
    tool: str,
    *,
    quota_delta: int = 0,
    jobs_delta: int = 0,
    items_delta: int = 0,
) -> Dict[str, Any]:
    """Additively update one tool bucket for a date without overwriting other tools."""
    tool_key = _coerce_tool_name(tool)
    day = normalize_day_entry(data.get(date_str))
    tools = day.setdefault("tools", {})
    stats = tools.setdefault(tool_key, _empty_tool_stats())
    stats["quota_units"] = int(stats.get("quota_units", 0) or 0) + int(quota_delta)
    stats["jobs"] = int(stats.get("jobs", 0) or 0) + int(jobs_delta)
    stats["items_processed"] = int(stats.get("items_processed", 0) or 0) + int(items_delta)
    day["tools"] = tools
    day["total_quota_units"] = _sum_tool_quota(tools)
    day["schema_version"] = SCHEMA_VERSION
    data[date_str] = day
    return data


def prune_history(data: Dict[str, Any], max_days: int = DAILY_HISTORY_MAX_DAYS) -> Dict[str, Any]:
    if len(data) > max_days:
        for old_key in sorted(data.keys())[:-max_days]:
            del data[old_key]
    return data


def load_history_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def save_history_file(
    path: Path,
    data: Dict[str, Any],
    *,
    max_days: int = DAILY_HISTORY_MAX_DAYS,
    indent: Optional[int] = 2,
) -> None:
    prune_history(data, max_days)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(data, indent=indent, sort_keys=indent is None)
    tmp.write_text(payload)
    os.replace(tmp, path)
