import os
import asyncio
import base64
import csv
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("vt_bulk_check")

router = APIRouter(prefix="/vt-bulk-check")

# ----- Usage Tracking -----
# Persistent storage for daily usage counters
_USAGE_FILE = Path(os.environ.get("VT_USAGE_FILE", "/tmp/vt_bulk_check_usage.json"))
_USAGE_LOCK = asyncio.Lock()

# Constants
DAILY_LOOKUPS_LIMIT = 500
RATE_LIMIT_PER_MIN = 4

# Auto-rescan guardrails
MAX_AUTO_RESCANS_PER_RUN = 25  # Maximum stale items to auto-rescan per run
AUTO_RESCAN_IF_OLDER_THAN_DAYS = 7  # Only auto-rescan items older than this threshold

# ----- Daily Usage History -----
_DAILY_HISTORY_FILE = Path("/var/lib/dns-tool/vt_daily_history.json")
_DAILY_HISTORY_MAX_DAYS = 90

# ----- Per-Job JSONL Usage History -----
_USAGE_HISTORY_FILE = Path("/var/lib/dns-tool/vt_usage_history.jsonl")

# ----- Latest Job Snapshot -----
_LATEST_JOB_FILE = Path("/var/lib/dns-tool/vt_latest_job.json")
_LATEST_JOB_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days
_LATEST_JOB_ID: Optional[str] = None  # tracks job_id of the most recently saved/loaded snapshot


@dataclass
class UsageState:
    """Tracks daily API usage across all jobs."""
    date_utc: str = ""  # YYYY-MM-DD in UTC
    daily_lookups_used: int = 0


_USAGE_STATE: Optional[UsageState] = None


def _get_utc_date_str() -> str:
    """Get current UTC date as YYYY-MM-DD string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _load_usage_state() -> UsageState:
    """Load usage state from persistent storage."""
    global _USAGE_STATE
    
    today = _get_utc_date_str()
    
    if _USAGE_FILE.exists():
        try:
            data = json.loads(_USAGE_FILE.read_text())
            stored_date = data.get("date_utc", "")
            if stored_date == today:
                _USAGE_STATE = UsageState(
                    date_utc=today,
                    daily_lookups_used=data.get("daily_lookups_used", 0)
                )
                return _USAGE_STATE
        except Exception as e:
            logger.warning(f"Failed to load usage state: {e}")
    
    # Reset for new day or on error
    _USAGE_STATE = UsageState(date_utc=today, daily_lookups_used=0)
    return _USAGE_STATE


def _save_usage_state(state: UsageState) -> None:
    """Save usage state to persistent storage.

    vt_usage.json is always written first.  The daily history update is a
    separate side-effect; its failure must never affect vt_usage.json.
    """
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_FILE.write_text(json.dumps({
            "date_utc": state.date_utc,
            "daily_lookups_used": state.daily_lookups_used
        }))
    except Exception as e:
        logger.warning(f"Failed to save usage state: {e}")
    # Additive side-effect — isolated try/except so a failure here never affects vt_usage.json
    _update_daily_history(state.date_utc, state.daily_lookups_used)


def _update_daily_history(date_str: str, count: int) -> None:
    """Atomically update the rolling 90-day daily usage history file.

    Failure is logged and silently swallowed — must never block VT API calls.
    """
    try:
        history: Dict[str, Any] = {}
        if _DAILY_HISTORY_FILE.exists():
            try:
                history = json.loads(_DAILY_HISTORY_FILE.read_text())
                if not isinstance(history, dict):
                    history = {}
            except Exception:
                history = {}  # corrupt — start fresh
        history[date_str] = count
        # Prune to last 90 days
        if len(history) > _DAILY_HISTORY_MAX_DAYS:
            for old_key in sorted(history.keys())[:-_DAILY_HISTORY_MAX_DAYS]:
                del history[old_key]
        tmp = _DAILY_HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, sort_keys=True))
        os.replace(tmp, _DAILY_HISTORY_FILE)
    except Exception as e:
        logger.warning(f"Failed to update daily history: {e}")


async def _increment_usage(job_id: Optional[str] = None) -> None:
    """Increment usage counters for a VT API call."""
    global _USAGE_STATE
    
    async with _USAGE_LOCK:
        today = _get_utc_date_str()
        
        # Load or reset state if needed
        if _USAGE_STATE is None or _USAGE_STATE.date_utc != today:
            _USAGE_STATE = _load_usage_state()
        
        # Reset if date changed since load
        if _USAGE_STATE.date_utc != today:
            _USAGE_STATE = UsageState(date_utc=today, daily_lookups_used=0)
        
        # Increment daily counter
        _USAGE_STATE.daily_lookups_used += 1
        _save_usage_state(_USAGE_STATE)
        
        logger.debug(f"Usage incremented: daily={_USAGE_STATE.daily_lookups_used}")
    
    # Increment job-specific counter if job_id provided
    if job_id:
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.lookups_used += 1


def _get_current_usage() -> Tuple[int, str]:
    """Get current daily usage (count, date).
    
    The daily counter automatically resets at 00:00 UTC when the date changes.
    """
    global _USAGE_STATE
    today = _get_utc_date_str()
    
    # Load state if not initialized
    if _USAGE_STATE is None:
        _load_usage_state()
    
    # Check if we need to reset for a new UTC day
    if _USAGE_STATE and _USAGE_STATE.date_utc != today:
        # Date has changed - reset the counter for the new UTC day
        _USAGE_STATE = UsageState(date_utc=today, daily_lookups_used=0)
        _save_usage_state(_USAGE_STATE)
        logger.info(f"Daily usage counter reset for new UTC day: {today}")
    
    if _USAGE_STATE:
        return _USAGE_STATE.daily_lookups_used, today
    
    return 0, today


class SubmitRequest(BaseModel):
    items: List[str]
    use_domain_reports: bool = False


class RefreshRequest(BaseModel):
    job_id: str
    item: str


class ForceScanRequest(BaseModel):
    job_id: str
    item: str
    type: Literal["domain", "url"]


class JobOnlyRequest(BaseModel):
    job_id: str


class ForceScanBulkRequest(BaseModel):
    job_id: str
    normalized_items: List[str]


class AutoUpdateStaleRequest(BaseModel):
    job_id: str


class VtResult(BaseModel):
    input: str
    normalized: str
    type: Literal["domain", "url"]
    malicious: int
    suspicious: int
    harmless: int
    total_engines: int
    flagging_engines: int
    detection_ratio: str
    last_analysis_date: Optional[int]
    last_scanned_display: Optional[str]
    is_stale: bool
    scan_requested: bool
    error: Optional[str]


@dataclass
class JobState:
    job_id: str
    status: Literal["running", "done", "error"]
    processed: int
    total: int
    item_order: List[str] = field(default_factory=list)
    results_by_normalized: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    error_message: Optional[str] = None
    # Lookup mode: False = URL reports for bare domains (default), True = domain reports
    use_domain_reports: bool = False
    # Timestamps for snapshot/recovery
    submitted_at: Optional[float] = None
    completed_at: Optional[float] = None
    # Usage tracking for this job
    lookups_used: int = 0
    # Batch 6: quota/history tracking fields
    estimated_requests: int = 0   # conservative estimate computed at submit time
    rejected_count: int = 0       # count of items rejected at submit (never the values)
    usage_history_written: bool = False  # dedup guard — prevents double-append to JSONL
    # Auto-update (stale re-scan + refresh) progress tracking
    update_active: bool = False
    update_phase: Optional[Literal["scanning", "refreshing", "complete", "error"]] = None
    update_total: int = 0
    update_done: int = 0
    update_message: Optional[str] = None
    update_started_at: Optional[float] = None
    update_error: Optional[str] = None
    update_baseline_by_normalized: Dict[str, Optional[int]] = field(default_factory=dict)


class AsyncMinIntervalLimiter:
    def __init__(self, min_interval_seconds: float):
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = time.monotonic() + self._min_interval


_VT_BASE_URL = "https://www.virustotal.com/api/v3"
_VT_LIMITER = AsyncMinIntervalLimiter(min_interval_seconds=15.0)
_JOBS: Dict[str, JobState] = {}
_JOBS_LOCK = asyncio.Lock()

# Context variable to track current job_id for usage counting
from contextvars import ContextVar
_CURRENT_JOB_ID: ContextVar[Optional[str]] = ContextVar("current_job_id", default=None)


def _format_last_scanned(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def _is_stale(ts: Optional[int], threshold_days: int = 5) -> bool:
    if not ts:
        return True
    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - ts) > (threshold_days * 86400)


def _is_eligible_for_auto_rescan(ts: Optional[int]) -> bool:
    """
    Check if an item is eligible for auto-rescan based on the age threshold.
    Only items older than AUTO_RESCAN_IF_OLDER_THAN_DAYS are eligible.
    """
    if not ts:
        return True  # Never scanned = eligible
    now = datetime.now(tz=timezone.utc).timestamp()
    age_days = (now - ts) / 86400
    return age_days > AUTO_RESCAN_IF_OLDER_THAN_DAYS


def _get_report_age_days(ts: Optional[int]) -> Optional[float]:
    """Get the age of a report in days."""
    if not ts:
        return None
    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - ts) / 86400


def _normalize_item(raw: str, use_domain_reports: bool = False) -> Tuple[str, Literal["domain", "url"], str]:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Empty")

    if "://" in s:
        return s.lower(), "url", s

    if "/" in s:
        url = s
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        return url.lower(), "url", url

    domain = s.strip(".").lower()
    if not domain or " " in domain:
        raise ValueError("Invalid domain")

    # Bare domain: use domain report (opt-in) or URL report (default, matches VT website behavior)
    if use_domain_reports:
        return domain, "domain", domain
    url_target = f"http://{domain}/"
    return url_target, "url", url_target


def _vt_url_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().strip("=")


async def _vt_request(method: str, path: str, *, data: Optional[Dict[str, Any]] = None) -> httpx.Response:
    api_key = os.environ.get("VT_API_KEY")
    if not api_key:
        logger.error("VT_API_KEY is not configured")
        raise HTTPException(status_code=500, detail="VT_API_KEY is not configured")

    await _VT_LIMITER.acquire()

    headers = {
        "x-apikey": api_key,
        "accept": "application/json",
    }

    url = f"{_VT_BASE_URL}{path}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.debug(f"VT API request: {method} {url}")
            resp = await client.request(method, url, headers=headers, data=data)
            logger.debug(f"VT API response: {resp.status_code}")
            
            # Increment usage counters for every VT API call (regardless of status)
            job_id = _CURRENT_JOB_ID.get()
            await _increment_usage(job_id)
            
            return resp
    except Exception as e:
        logger.error(f"VT API request failed: {method} {url} - {type(e).__name__}: {str(e)}")
        raise


def _parse_analysis_from_report(input_value: str, normalized: str, item_type: Literal["domain", "url"], report: Dict[str, Any]) -> Dict[str, Any]:
    attrs = (((report or {}).get("data") or {}).get("attributes") or {})
    stats = attrs.get("last_analysis_stats") or {}

    malicious = int(stats.get("malicious") or 0)
    suspicious = int(stats.get("suspicious") or 0)
    harmless = int(stats.get("harmless") or 0)
    undetected = int(stats.get("undetected") or 0)
    timeout = int(stats.get("timeout") or 0)

    total_engines = malicious + suspicious + harmless + undetected + timeout
    flagging = malicious + suspicious

    last_analysis_date = attrs.get("last_analysis_date")
    if last_analysis_date is not None:
        try:
            last_analysis_date = int(last_analysis_date)
        except Exception:
            last_analysis_date = None

    return {
        "input": input_value,
        "normalized": normalized,
        "type": item_type,
        "malicious": malicious,
        "suspicious": suspicious,
        "harmless": harmless,
        "total_engines": total_engines,
        "flagging_engines": flagging,
        "detection_ratio": f"{flagging}/{total_engines}" if total_engines else f"{flagging}/0",
        "last_analysis_date": last_analysis_date,
        "last_scanned_display": _format_last_scanned(last_analysis_date),
        "is_stale": _is_stale(last_analysis_date, threshold_days=5),
        "scan_requested": False,
        "error": None,
    }


async def _fetch_domain_report(domain: str) -> Dict[str, Any]:
    logger.info(f"Fetching domain report for: {domain}")
    try:
        resp = await _vt_request("GET", f"/domains/{domain}")
        if resp.status_code == 404:
            logger.warning(f"Domain not found in VT: {domain}")
            raise HTTPException(status_code=404, detail="Not found")
        if resp.status_code == 429:
            logger.warning(f"VT rate limit reached for domain: {domain}")
            raise HTTPException(status_code=429, detail="Rate limit reached")
        if resp.status_code >= 400:
            logger.error(f"VT error for domain {domain}: {resp.status_code} - {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"VirusTotal error: {resp.text}")
        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching domain report for {domain}: {type(e).__name__}: {str(e)}")
        raise


async def _fetch_url_report(url: str) -> Dict[str, Any]:
    logger.info(f"Fetching URL report for: {url}")
    url_id = _vt_url_id(url)
    try:
        resp = await _vt_request("GET", f"/urls/{url_id}")
        if resp.status_code == 404:
            logger.info(f"URL not found in VT, submitting: {url}")
            submit = await _vt_request("POST", "/urls", data={"url": url})
            if submit.status_code == 429:
                logger.warning(f"VT rate limit reached for URL submit: {url}")
                raise HTTPException(status_code=429, detail="Rate limit reached")
            if submit.status_code >= 400:
                logger.error(f"VT submit error for URL {url}: {submit.status_code} - {submit.text}")
                raise HTTPException(status_code=submit.status_code, detail=f"VirusTotal submit error: {submit.text}")
            resp2 = await _vt_request("GET", f"/urls/{url_id}")
            if resp2.status_code >= 400:
                logger.error(f"VT error for URL {url} after submit: {resp2.status_code} - {resp2.text}")
                raise HTTPException(status_code=resp2.status_code, detail=f"VirusTotal error: {resp2.text}")
            return resp2.json()

        if resp.status_code == 429:
            logger.warning(f"VT rate limit reached for URL: {url}")
            raise HTTPException(status_code=429, detail="Rate limit reached")
        if resp.status_code >= 400:
            logger.error(f"VT error for URL {url}: {resp.status_code} - {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"VirusTotal error: {resp.text}")
        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching URL report for {url}: {type(e).__name__}: {str(e)}")
        raise


async def _reanalyze_domain(domain: str) -> None:
    resp = await _vt_request("POST", f"/domains/{domain}/analyse")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Rate limit reached")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"VirusTotal error: {resp.text}")


async def _reanalyze_url(url: str) -> None:
    url_id = _vt_url_id(url)
    resp = await _vt_request("POST", f"/urls/{url_id}/analyse")
    if resp.status_code == 404:
        submit = await _vt_request("POST", "/urls", data={"url": url})
        if submit.status_code == 429:
            raise HTTPException(status_code=429, detail="Rate limit reached")
        if submit.status_code >= 400:
            raise HTTPException(status_code=submit.status_code, detail=f"VirusTotal submit error: {submit.text}")
        resp = await _vt_request("POST", f"/urls/{url_id}/analyse")

    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Rate limit reached")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"VirusTotal error: {resp.text}")


def _append_usage_history(summary: Dict[str, Any]) -> None:
    """Append one metadata-only summary record to the per-job usage history JSONL file.

    Must be called OUTSIDE _JOBS_LOCK.  Failure is logged and swallowed —
    it must never affect job completion.  The record must contain only counts
    and metadata — no domain names, no URLs, no input items.
    """
    try:
        _USAGE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_USAGE_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
        logger.debug(f"Usage history appended for job {summary.get('job_id')}")
    except Exception as e:
        logger.warning(f"Failed to append usage history for job {summary.get('job_id')}: {e}")


def _save_latest_job_snapshot(snapshot: Dict[str, Any]) -> None:
    """Write a pre-built snapshot dict to disk atomically with 0600 permissions.

    The caller must build ``snapshot`` *after* releasing _JOBS_LOCK so no disk
    I/O ever occurs while the lock is held.  Any failure is logged and silently
    swallowed — it must never affect job completion.
    """
    global _LATEST_JOB_ID
    try:
        _LATEST_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LATEST_JOB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot))
        tmp.chmod(0o600)
        os.replace(tmp, _LATEST_JOB_FILE)
        _LATEST_JOB_FILE.chmod(0o600)
        _LATEST_JOB_ID = snapshot["job_id"]
        logger.info(f"Saved latest job snapshot: {snapshot['job_id']}")
    except Exception as e:
        logger.warning(f"Failed to save latest job snapshot: {e}")


def _delete_latest_job_snapshot() -> None:
    """Delete the snapshot file, logging a warning on failure (never raises)."""
    try:
        if _LATEST_JOB_FILE.exists():
            _LATEST_JOB_FILE.unlink()
            logger.info("Deleted latest job snapshot file")
    except Exception as e:
        logger.warning(f"Failed to delete latest job snapshot: {e}")


def _load_latest_job_snapshot() -> Optional[Dict[str, Any]]:
    """Read and validate the snapshot file.

    Returns the dict if valid and unexpired, None otherwise.
    Corrupt, expired, or unknown-schema files are deleted to prevent
    repeated warnings on every startup.  Never raises.
    """
    try:
        if not _LATEST_JOB_FILE.exists():
            return None
        raw = _LATEST_JOB_FILE.read_text()
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Latest job snapshot corrupt (JSON): {e} — deleting")
        _delete_latest_job_snapshot()
        return None
    except Exception as e:
        logger.warning(f"Failed to read latest job snapshot: {e}")
        return None

    try:
        if data.get("schema_version") != 1:
            logger.warning(f"Latest job snapshot unknown schema_version={data.get('schema_version')} — deleting")
            _delete_latest_job_snapshot()
            return None

        completed_at = data.get("completed_at")
        if not completed_at:
            logger.warning("Latest job snapshot missing completed_at — deleting")
            _delete_latest_job_snapshot()
            return None

        age = time.time() - float(completed_at)
        if age > _LATEST_JOB_MAX_AGE_SECONDS:
            logger.info(f"Latest job snapshot expired ({age / 86400:.1f} days old) — deleting")
            _delete_latest_job_snapshot()
            return None

        return data
    except Exception as e:
        logger.warning(f"Latest job snapshot validation error: {e} — deleting")
        _delete_latest_job_snapshot()
        return None


def _reconstruct_job_from_snapshot(data: Dict[str, Any]) -> "JobState":
    """Rebuild a JobState from a validated snapshot dict."""
    job = JobState(
        job_id=data["job_id"],
        status="done",
        processed=int(data.get("processed", 0)),
        total=int(data.get("total", 0)),
        error_message=data.get("error_message"),
        use_domain_reports=bool(data.get("use_domain_reports", False)),
        submitted_at=data.get("submitted_at"),
        completed_at=data.get("completed_at"),
        estimated_requests=int(data.get("estimated_requests", 0)),
        rejected_count=int(data.get("rejected_count", 0)),
        # Recovered jobs: treat history as already written (it was written at original completion)
        usage_history_written=True,
    )
    job.item_order = list(data.get("item_order") or [])
    job.results_by_normalized = dict(data.get("results_by_normalized") or {})
    # Ensure no auto-update state is active on a recovered job
    job.update_active = False
    job.update_phase = None
    job.update_total = 0
    job.update_done = 0
    job.update_message = None
    job.update_error = None
    return job


def _startup_load_latest_job_sync() -> None:
    """Load the latest completed job snapshot into _JOBS at module import time.

    Runs synchronously before any request is served.  Any failure is caught and
    logged — this function must never raise.
    """
    global _LATEST_JOB_ID
    try:
        data = _load_latest_job_snapshot()
        if not data:
            return
        job = _reconstruct_job_from_snapshot(data)
        _JOBS[job.job_id] = job
        _LATEST_JOB_ID = job.job_id
        logger.info(f"Startup: recovered latest job {job.job_id} into _JOBS")
    except Exception as e:
        logger.warning(f"Startup: failed to recover latest job snapshot: {e}")


# Load snapshot once at import time — runs before any request handler is called.
_startup_load_latest_job_sync()


async def _process_job(job_id: str) -> None:
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        logger.warning(f"Job {job_id} not found in _process_job")
        return

    logger.info(f"Starting job processing: {job_id} with {job.total} items")
    
    # Set context for usage tracking
    _CURRENT_JOB_ID.set(job_id)
    
    try:
        for normalized in job.item_order:
            async with _JOBS_LOCK:
                current = _JOBS.get(job_id)
                if not current or current.status != "running":
                    return
                result = current.results_by_normalized.get(normalized)

            if not result:
                continue

            item_type = result["type"]
            try:
                if item_type == "domain":
                    report = await _fetch_domain_report(result["normalized"])
                else:
                    report = await _fetch_url_report(result["normalized_full_url"])  # type: ignore[typeddict-item]

                updated = _parse_analysis_from_report(result["input"], result["normalized"], item_type, report)

                async with _JOBS_LOCK:
                    job2 = _JOBS.get(job_id)
                    if not job2:
                        return
                    updated["normalized_full_url"] = result.get("normalized_full_url")
                    job2.results_by_normalized[normalized] = updated
                    job2.processed += 1
                    logger.info(f"Successfully processed {item_type}: {result['input']}")

            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.error(f"Error processing {item_type} '{result.get('input', normalized)}': {error_msg}")
                async with _JOBS_LOCK:
                    job2 = _JOBS.get(job_id)
                    if not job2:
                        return
                    job2.processed += 1
                    job2.results_by_normalized[normalized]["error"] = error_msg

        # Build snapshot dict under lock, then write to disk after releasing lock.
        snapshot_to_save: Optional[Dict[str, Any]] = None
        history_to_append: Optional[Dict[str, Any]] = None
        async with _JOBS_LOCK:
            job3 = _JOBS.get(job_id)
            if job3:
                job3.status = "done"
                job3.completed_at = time.time()
                logger.info(f"Job {job_id} completed successfully: {job3.processed}/{job3.total} items processed")
                snapshot_to_save = {
                    "schema_version": 1,
                    "job_id": job3.job_id,
                    "status": "done",
                    "submitted_at": job3.submitted_at,
                    "completed_at": job3.completed_at,
                    "processed": job3.processed,
                    "total": job3.total,
                    "error_message": job3.error_message,
                    "use_domain_reports": job3.use_domain_reports,
                    "estimated_requests": job3.estimated_requests,
                    "rejected_count": job3.rejected_count,
                    "item_order": list(job3.item_order),
                    "results_by_normalized": {k: dict(v) for k, v in job3.results_by_normalized.items()},
                    "rejected": [],
                }
                if not job3.usage_history_written:
                    job3.usage_history_written = True
                    # Pre-compute counts from job state (inside lock is fine — read-only)
                    _url_cnt = sum(1 for r in job3.results_by_normalized.values() if r.get("type") == "url")
                    _dom_cnt = sum(1 for r in job3.results_by_normalized.values() if r.get("type") == "domain")
                    history_to_append = {
                        "ts": job3.completed_at,
                        "job_id": job3.job_id,
                        "status": "done",
                        "submitted_at": job3.submitted_at,
                        "completed_at": job3.completed_at,
                        "accepted_count": job3.total,
                        "rejected_count": job3.rejected_count,
                        "processed": job3.processed,
                        "total": job3.total,
                        "url_count": _url_cnt,
                        "domain_count": _dom_cnt,
                        "actual_lookups": job3.lookups_used,
                        "estimated_requests": job3.estimated_requests,
                        "use_domain_reports": job3.use_domain_reports,
                        "error_summary": None,
                    }
        # All disk writes happen OUTSIDE the lock — failures are logged, never propagated
        if snapshot_to_save is not None:
            _save_latest_job_snapshot(snapshot_to_save)
        if history_to_append is not None:
            _append_usage_history(history_to_append)

    except Exception as e:
        logger.error(f"Fatal error in job {job_id}: {type(e).__name__}: {str(e)}", exc_info=True)
        error_history: Optional[Dict[str, Any]] = None
        async with _JOBS_LOCK:
            job4 = _JOBS.get(job_id)
            if job4:
                job4.status = "error"
                job4.error_message = str(e)
                if not job4.usage_history_written:
                    job4.usage_history_written = True
                    _err_ts = time.time()
                    _e_url = sum(1 for r in job4.results_by_normalized.values() if r.get("type") == "url")
                    _e_dom = sum(1 for r in job4.results_by_normalized.values() if r.get("type") == "domain")
                    error_history = {
                        "ts": _err_ts,
                        "job_id": job4.job_id,
                        "status": "error",
                        "submitted_at": job4.submitted_at,
                        "completed_at": _err_ts,
                        "accepted_count": job4.total,
                        "rejected_count": job4.rejected_count,
                        "processed": job4.processed,
                        "total": job4.total,
                        "url_count": _e_url,
                        "domain_count": _e_dom,
                        "actual_lookups": job4.lookups_used,
                        "estimated_requests": job4.estimated_requests,
                        "use_domain_reports": job4.use_domain_reports,
                        "error_summary": type(e).__name__,
                    }
        if error_history is not None:
            _append_usage_history(error_history)


def _job_results_list(job: JobState) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in job.item_order:
        r = job.results_by_normalized.get(key)
        if not r:
            continue
        r2 = {k: v for k, v in r.items() if k != "normalized_full_url"}
        out.append(r2)
    return out


async def _auto_update_stale_job(job_id: str, max_rescans: int = MAX_AUTO_RESCANS_PER_RUN) -> None:
    """
    FIRE-AND-FORGET auto-rescan for stale items.
    
    This function:
    1) Requests a new scan (reanalyze) for eligible stale items only
    2) Does NOT auto-refresh reports afterward
    3) Does NOT poll VirusTotal for updated results
    
    Users must manually click "Refresh report" to see updated results.
    
    Guardrails:
    - Only rescans items older than AUTO_RESCAN_IF_OLDER_THAN_DAYS
    - Caps rescans at max_rescans (default MAX_AUTO_RESCANS_PER_RUN)
    """
    # Set context for usage tracking
    _CURRENT_JOB_ID.set(job_id)
    
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if job.update_active:
            return

        # Find stale items that are ELIGIBLE for auto-rescan (older than threshold)
        all_stale = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]
        eligible = []
        for k in all_stale:
            r = job.results_by_normalized.get(k, {})
            last_ts = r.get("last_analysis_date")
            if _is_eligible_for_auto_rescan(last_ts):
                eligible.append(k)
        
        # Apply the cap
        targets = eligible[:max_rescans]
        skipped_count = len(eligible) - len(targets)
        total_stale = len(all_stale)
        ineligible_count = total_stale - len(eligible)
        
        logger.info(
            f"Auto-rescan job {job_id}: {total_stale} stale, {len(eligible)} eligible "
            f"(older than {AUTO_RESCAN_IF_OLDER_THAN_DAYS} days), {len(targets)} will be rescanned "
            f"(cap={max_rescans}), {skipped_count} skipped due to cap, {ineligible_count} too recent"
        )
        
        job.update_active = True
        job.update_phase = "scanning"
        job.update_total = len(targets)
        job.update_done = 0
        job.update_started_at = time.monotonic()
        job.update_error = None
        job.update_baseline_by_normalized = {}
        
        if targets:
            job.update_message = f"Requesting rescans… 0/{len(targets)}"
        else:
            job.update_message = "No eligible items to rescan"

    if not targets:
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.update_active = False
                job.update_phase = "complete"
                job.update_total = 0
                job.update_done = 0
                if ineligible_count > 0:
                    job.update_message = f"No items old enough to auto-rescan ({ineligible_count} stale but < {AUTO_RESCAN_IF_OLDER_THAN_DAYS} days old)"
                else:
                    job.update_message = "No stale items"
                job.update_error = None
                if job.status != "error":
                    job.status = "done"
        logger.info(f"Auto-rescan job {job_id}: No eligible items, completed immediately")
        return

    # FIRE-AND-FORGET: Request re-analyze for each eligible item (NO refresh polling afterward)
    scan_done = 0
    scan_success = 0
    scan_failed = 0
    
    for normalized in targets:
        async with _JOBS_LOCK:
            job2 = _JOBS.get(job_id)
            if not job2:
                return
            r = job2.results_by_normalized.get(normalized) or {}
            item_type = r.get("type")
            full_url = r.get("normalized_full_url")

        try:
            if item_type == "domain":
                await _reanalyze_domain(normalized)
            else:
                await _reanalyze_url(full_url or normalized)
            
            scan_success += 1
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                if normalized in job3.results_by_normalized:
                    job3.results_by_normalized[normalized]["scan_requested"] = True
            
            logger.debug(f"Auto-rescan: Requested rescan for {item_type} '{normalized}'")
            
        except Exception as e:
            scan_failed += 1
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                if normalized in job3.results_by_normalized:
                    job3.results_by_normalized[normalized]["error"] = str(e)
            
            logger.warning(f"Auto-rescan: Failed to request rescan for '{normalized}': {e}")
            
        finally:
            scan_done += 1
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                job3.update_done = scan_done
                job3.update_message = f"Requesting rescans… {scan_done}/{job3.update_total}"

    # COMPLETE - No refresh polling, fire-and-forget only
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        
        job.update_phase = "complete"
        job.update_active = False
        
        # Build completion message
        msg_parts = [f"Requested {scan_success} rescan(s)"]
        if scan_failed > 0:
            msg_parts.append(f"{scan_failed} failed")
        if skipped_count > 0:
            msg_parts.append(f"{skipped_count} skipped (cap reached)")
        msg_parts.append("Refresh reports manually to see results")
        
        job.update_message = ". ".join(msg_parts) + "."
        
        if job.status != "error":
            job.status = "done"
    
    logger.info(
        f"Auto-rescan job {job_id} COMPLETE (fire-and-forget): "
        f"{scan_success} rescans requested, {scan_failed} failed, {skipped_count} skipped. "
        f"NO follow-up VT calls scheduled."
    )

@router.post("/submit")
async def submit(req: SubmitRequest):
    rejected: List[str] = []
    accepted_items: List[Tuple[str, Literal["domain", "url"], str, str]] = []
    seen: set[str] = set()

    logger.info(f"Received submit request with {len(req.items or [])} items")

    for raw in req.items or []:
        try:
            normalized, item_type, normalized_full = _normalize_item(raw, req.use_domain_reports)
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            accepted_items.append((raw, item_type, normalized, normalized_full))
        except Exception as e:
            logger.warning(f"Rejected item '{raw}': {type(e).__name__}: {str(e)}")
            rejected.append(raw)

    # SEC-04: reject if estimated VirusTotal API request cost exceeds the daily quota.
    # Domains cost 1 request each; URLs may cost up to 3 requests each.
    estimated_requests = sum(3 if item_type == "url" else 1 for _, item_type, _, _ in accepted_items)
    if estimated_requests > DAILY_LOOKUPS_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Submission is estimated to use {estimated_requests} VirusTotal API requests. "
                f"The daily quota limit is {DAILY_LOOKUPS_LIMIT} requests. "
                f"Domains count as 1 request and URLs may count as up to 3 requests. "
                f"Please reduce or split your list."
            )
        )

    job_id = str(uuid.uuid4())
    logger.info(f"Created job {job_id}: {len(accepted_items)} accepted, {len(rejected)} rejected")

    job = JobState(job_id=job_id, status="running", processed=0, total=len(accepted_items),
                   use_domain_reports=req.use_domain_reports,
                   submitted_at=time.time(),
                   estimated_requests=estimated_requests,
                   rejected_count=len(rejected))
    for original, item_type, normalized, normalized_full in accepted_items:
        key = normalized
        job.item_order.append(key)
        base = {
            "input": original,
            "normalized": normalized,
            "type": item_type,
            "malicious": 0,
            "suspicious": 0,
            "harmless": 0,
            "total_engines": 0,
            "flagging_engines": 0,
            "detection_ratio": "0/0",
            "last_analysis_date": None,
            "last_scanned_display": None,
            "is_stale": True,
            "scan_requested": False,
            "error": None,
        }
        if item_type == "url":
            base["normalized_full_url"] = normalized_full
        job.results_by_normalized[key] = base

    async with _JOBS_LOCK:
        _JOBS[job_id] = job

    asyncio.create_task(_process_job(job_id))

    return {
        "job_id": job_id,
        "total": job.total,
        "accepted": job.total,
        "rejected": rejected,
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "processed": job.processed,
            "total": job.total,
            "error_message": job.error_message,
            "update": {
                "active": job.update_active,
                "phase": job.update_phase,
                "done": job.update_done,
                "total": job.update_total,
                "message": job.update_message,
                "error": job.update_error,
            },
            "results": _job_results_list(job),
        }


@router.get("/jobs/{job_id}/export")
async def export_job_csv(job_id: str):
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        results = _job_results_list(job)
        # Option B: read normalized_full_url from the raw result dicts while the
        # lock is held so we can populate checked_as without exposing
        # normalized_full_url in the public GET /jobs/{job_id} response.
        raw_by_key = {
            key: job.results_by_normalized.get(key, {})
            for key in job.item_order
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["input", "checked_as", "type", "flagging", "total_engines", "detection_ratio", "last_scanned", "status", "error"])
    # zip is safe: _job_results_list preserves item_order order
    for r, key in zip(results, job.item_order):
        raw_r = raw_by_key.get(key, {})
        item_type = r.get("type", "")
        if item_type == "url":
            checked_as = raw_r.get("normalized_full_url") or r.get("normalized", "")
        else:
            checked_as = r.get("normalized", "")
        error = r.get("error") or ""
        if error:
            status = "ERROR"
        elif r.get("is_stale"):
            status = "STALE"
        else:
            status = "OK"
        writer.writerow([
            r.get("input", ""),
            checked_as,
            item_type,
            "" if error else r.get("flagging_engines", 0),
            "" if error else r.get("total_engines", 0),
            "" if error else r.get("detection_ratio", ""),
            r.get("last_scanned_display") or "",
            status,
            error,
        ])

    csv_content = buf.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="vt-bulk-check-{job_id}.csv"'
        },
    )


@router.get("/usage")
async def get_usage(job_id: Optional[str] = Query(None, alias="jobId")):
    """
    Get API usage statistics without making any VirusTotal API calls.
    
    Returns:
        - jobLookupsUsed: Number of lookups used by the specified job (if job_id provided)
        - dailyLookupsUsed: Total lookups used today (UTC)
        - dailyLookupsLimit: Daily limit (500)
        - rateLimitPerMin: Rate limit per minute (4)
    """
    job_lookups = 0
    
    if job_id:
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job_lookups = job.lookups_used
    
    daily_used, _ = _get_current_usage()
    
    return {
        "jobLookupsUsed": job_lookups,
        "dailyLookupsUsed": daily_used,
        "dailyLookupsLimit": DAILY_LOOKUPS_LIMIT,
        "rateLimitPerMin": RATE_LIMIT_PER_MIN,
    }


def _format_epoch_utc(epoch: Any) -> str:
    """Convert a Unix timestamp float to 'YYYY-MM-DD HH:MM:SS UTC', or empty string."""
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ""


_USAGE_HISTORY_CSV_COLUMNS = [
    "timestamp_utc", "job_id", "status",
    "submitted_at_utc", "completed_at_utc",
    "accepted_count", "rejected_count", "processed", "total",
    "url_count", "domain_count", "estimated_requests",
    "actual_lookups", "use_domain_reports", "error_summary",
]


@router.get("/usage-history/export")
async def export_usage_history():
    """
    Download per-job usage history as CSV for API quota planning.

    Reads vt_usage_history.jsonl and returns a CSV with one row per job.
    No VirusTotal API calls are made and zero quota is consumed.
    Corrupt JSONL lines are skipped.
    Internal use only — no authentication required (consistent with the rest of the tool).
    Does not expose domain names, URLs, item values, or any sensitive item-level data.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_USAGE_HISTORY_CSV_COLUMNS)

    if _USAGE_HISTORY_FILE.exists():
        try:
            raw_text = _USAGE_HISTORY_FILE.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read usage history file: {e}")
            raw_text = ""

        for raw_line in raw_text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                rec = json.loads(raw_line)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt usage history line")
                continue
            writer.writerow([
                _format_epoch_utc(rec.get("ts")),
                rec.get("job_id", ""),
                rec.get("status", ""),
                _format_epoch_utc(rec.get("submitted_at")),
                _format_epoch_utc(rec.get("completed_at")),
                rec.get("accepted_count", ""),
                rec.get("rejected_count", ""),
                rec.get("processed", ""),
                rec.get("total", ""),
                rec.get("url_count", ""),
                rec.get("domain_count", ""),
                rec.get("estimated_requests", ""),
                rec.get("actual_lookups", ""),
                rec.get("use_domain_reports", ""),
                rec.get("error_summary", ""),
            ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vt-usage-history.csv"'},
    )


@router.get("/latest-job")
async def get_latest_job():
    """
    Return metadata for the most recently completed job, or {"job_id": null}.

    Never returns full results (results_by_normalized, item_order).
    Never returns use_domain_reports.
    Makes no VirusTotal API calls and consumes 0 quota.
    """
    global _LATEST_JOB_ID

    # Check in-memory _JOBS first (fastest path — no file I/O)
    if _LATEST_JOB_ID:
        async with _JOBS_LOCK:
            job = _JOBS.get(_LATEST_JOB_ID)
        if job and job.status == "done":
            # Belt-and-suspenders expiration check at request time (PERSIST-05F)
            if job.completed_at and (time.time() - job.completed_at) > _LATEST_JOB_MAX_AGE_SECONDS:
                async with _JOBS_LOCK:
                    _JOBS.pop(_LATEST_JOB_ID, None)
                _LATEST_JOB_ID = None
                _delete_latest_job_snapshot()
                return {"job_id": None}
            return {
                "job_id": job.job_id,
                "status": job.status,
                "total": job.total,
                "processed": job.processed,
                "completed_at": job.completed_at,
                "submitted_at": job.submitted_at,
                "rejected_count": 0,
            }
        # ID set but job not in memory (evicted) — fall through to file
        _LATEST_JOB_ID = None

    # Fall back to snapshot file
    data = _load_latest_job_snapshot()
    if not data:
        return {"job_id": None}

    # Reload into _JOBS so subsequent GET /jobs/{job_id} calls work
    try:
        job = _reconstruct_job_from_snapshot(data)
        async with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        _LATEST_JOB_ID = job.job_id
    except Exception as e:
        logger.warning(f"latest-job: failed to reload snapshot into _JOBS: {e}")
        return {"job_id": None}

    return {
        "job_id": data["job_id"],
        "status": data.get("status", "done"),
        "total": data.get("total", 0),
        "processed": data.get("processed", 0),
        "completed_at": data.get("completed_at"),
        "submitted_at": data.get("submitted_at"),
        "rejected_count": len(data.get("rejected") or []),
    }


@router.get("/jobs/{job_id}/rescan-eligibility")
async def get_rescan_eligibility(job_id: str):
    """
    Get information about how many items are eligible for auto-rescan.
    Used by the frontend to show confirmation dialogs before triggering rescans.
    
    Does NOT make any VirusTotal API calls.
    """
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        all_stale = []
        eligible = []
        
        for k in job.item_order:
            r = job.results_by_normalized.get(k, {})
            if r.get("is_stale"):
                all_stale.append(k)
                last_ts = r.get("last_analysis_date")
                age_days = _get_report_age_days(last_ts)
                if _is_eligible_for_auto_rescan(last_ts):
                    eligible.append({
                        "normalized": k,
                        "age_days": round(age_days, 1) if age_days else None
                    })
        
        will_rescan = min(len(eligible), MAX_AUTO_RESCANS_PER_RUN)
        skipped_due_to_cap = max(0, len(eligible) - MAX_AUTO_RESCANS_PER_RUN)
        too_recent = len(all_stale) - len(eligible)
        
        daily_used, _ = _get_current_usage()
        daily_remaining = max(0, DAILY_LOOKUPS_LIMIT - daily_used)
        
        return {
            "totalStale": len(all_stale),
            "eligibleCount": len(eligible),
            "willRescan": will_rescan,
            "skippedDueToCap": skipped_due_to_cap,
            "tooRecent": too_recent,
            "maxAutoRescansPerRun": MAX_AUTO_RESCANS_PER_RUN,
            "autoRescanThresholdDays": AUTO_RESCAN_IF_OLDER_THAN_DAYS,
            "dailyLookupsRemaining": daily_remaining,
            "dailyLookupsLimit": DAILY_LOOKUPS_LIMIT,
        }


@router.post("/jobs/{job_id}/auto-update-stale")
async def auto_update_stale(job_id: str, _req: AutoUpdateStaleRequest):
    """
    Trigger fire-and-forget auto-rescan for eligible stale items.
    
    This endpoint:
    - Only rescans items older than AUTO_RESCAN_IF_OLDER_THAN_DAYS
    - Caps rescans at MAX_AUTO_RESCANS_PER_RUN
    - Does NOT auto-refresh reports afterward (fire-and-forget)
    - Does NOT poll VirusTotal for updates
    """
    # Keep job_id both in path and body to make clients explicit
    if _req.job_id != job_id:
        raise HTTPException(status_code=400, detail="job_id mismatch")

    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.update_active:
            return {"scheduled": False, "message": "Auto-rescan already in progress"}
        
        # Check for stale items
        all_stale = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]
        
        if not all_stale:
            job.update_active = False
            job.update_phase = "complete"
            job.update_total = 0
            job.update_done = 0
            job.update_message = "No stale items"
            job.update_error = None
            job.update_baseline_by_normalized = {}
            if job.status != "error":
                job.status = "done"
            return {"scheduled": False, "message": "No stale items"}
        
        # Check for eligible items (older than threshold)
        eligible = []
        for k in all_stale:
            r = job.results_by_normalized.get(k, {})
            last_ts = r.get("last_analysis_date")
            if _is_eligible_for_auto_rescan(last_ts):
                eligible.append(k)
        
        if not eligible:
            job.update_active = False
            job.update_phase = "complete"
            job.update_total = 0
            job.update_done = 0
            job.update_message = f"No items old enough to auto-rescan ({len(all_stale)} stale but < {AUTO_RESCAN_IF_OLDER_THAN_DAYS} days old)"
            job.update_error = None
            job.update_baseline_by_normalized = {}
            if job.status != "error":
                job.status = "done"
            return {
                "scheduled": False, 
                "message": f"No items eligible for auto-rescan (all {len(all_stale)} stale items are less than {AUTO_RESCAN_IF_OLDER_THAN_DAYS} days old)"
            }
        
        will_rescan = min(len(eligible), MAX_AUTO_RESCANS_PER_RUN)
        skipped = len(eligible) - will_rescan

    asyncio.create_task(_auto_update_stale_job(job_id))
    
    return {
        "scheduled": True,
        "willRescan": will_rescan,
        "skippedDueToCap": skipped,
        "maxAutoRescansPerRun": MAX_AUTO_RESCANS_PER_RUN,
        "message": f"Requesting rescans for {will_rescan} eligible item(s)" + (f" ({skipped} skipped due to cap)" if skipped > 0 else "")
    }


@router.post("/refresh")
async def refresh(req: RefreshRequest):
    # Fetch job first so normalization uses the job's original lookup mode
    async with _JOBS_LOCK:
        job_check = _JOBS.get(req.job_id)
        if not job_check:
            raise HTTPException(status_code=404, detail="Job not found")
        use_domain_reports = job_check.use_domain_reports

    try:
        normalized, item_type, normalized_full = _normalize_item(req.item, use_domain_reports)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid item")

    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        existing = job.results_by_normalized.get(normalized)
        if not existing:
            raise HTTPException(status_code=404, detail="Item not found in job")
        input_value = existing.get("input", req.item)

    # Set context for usage tracking
    _CURRENT_JOB_ID.set(req.job_id)

    if item_type == "domain":
        report = await _fetch_domain_report(normalized)
    else:
        report = await _fetch_url_report(normalized_full)

    updated = _parse_analysis_from_report(input_value, normalized, item_type, report)
    if item_type == "url":
        updated["normalized_full_url"] = normalized_full

    async with _JOBS_LOCK:
        job2 = _JOBS.get(req.job_id)
        if not job2:
            raise HTTPException(status_code=404, detail="Job not found")
        job2.results_by_normalized[normalized] = updated

    return updated


@router.post("/force-scan")
async def force_scan(req: ForceScanRequest):
    # Fetch job first so normalization uses the job's original lookup mode
    async with _JOBS_LOCK:
        job_check = _JOBS.get(req.job_id)
        if not job_check:
            raise HTTPException(status_code=404, detail="Job not found")
        use_domain_reports = job_check.use_domain_reports

    try:
        normalized, _, normalized_full = _normalize_item(req.item, use_domain_reports)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid item")

    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        existing = job.results_by_normalized.get(normalized)
        if not existing:
            raise HTTPException(status_code=404, detail="Item not found in job")

    # Set context for usage tracking
    _CURRENT_JOB_ID.set(req.job_id)

    if req.type == "domain":
        await _reanalyze_domain(normalized)
    else:
        await _reanalyze_url(normalized_full)

    async with _JOBS_LOCK:
        job2 = _JOBS.get(req.job_id)
        if not job2:
            raise HTTPException(status_code=404, detail="Job not found")
        r = job2.results_by_normalized.get(normalized)
        if r:
            r["scan_requested"] = True

    return {"scan_requested": True}


@router.post("/refresh-stale")
async def refresh_stale(req: JobOnlyRequest):
    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        targets = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]

    async def _run() -> None:
        # Set context for usage tracking
        _CURRENT_JOB_ID.set(req.job_id)
        for normalized in targets:
            async with _JOBS_LOCK:
                job2 = _JOBS.get(req.job_id)
                if not job2:
                    return
                r = job2.results_by_normalized.get(normalized)
                if not r:
                    continue
                item_type = r.get("type")
                input_value = r.get("input")
                full_url = r.get("normalized_full_url")

            try:
                if item_type == "domain":
                    report = await _fetch_domain_report(normalized)
                else:
                    report = await _fetch_url_report(full_url)

                updated = _parse_analysis_from_report(input_value, normalized, item_type, report)
                if item_type == "url":
                    updated["normalized_full_url"] = full_url

                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    job3.results_by_normalized[normalized] = updated
            except Exception as e:
                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["error"] = str(e)

    asyncio.create_task(_run())

    return {"refreshed": 0, "scheduled": len(targets)}


@router.post("/force-scan-stale")
async def force_scan_stale(req: JobOnlyRequest):
    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        targets = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]

    async def _run() -> None:
        # Set context for usage tracking
        _CURRENT_JOB_ID.set(req.job_id)
        
        for normalized in targets:
            async with _JOBS_LOCK:
                job2 = _JOBS.get(req.job_id)
                if not job2:
                    return
                r = job2.results_by_normalized.get(normalized)
                if not r:
                    continue
                item_type = r.get("type")
                full_url = r.get("normalized_full_url")

            try:
                if item_type == "domain":
                    await _reanalyze_domain(normalized)
                else:
                    await _reanalyze_url(full_url)

                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["scan_requested"] = True
            except Exception as e:
                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["error"] = str(e)

    asyncio.create_task(_run())

    return {"scan_requested": True, "scheduled": len(targets)}


@router.post("/force-scan-bulk")
async def force_scan_bulk(req: ForceScanBulkRequest):
    normalized_items = [str(s or "").strip().lower() for s in (req.normalized_items or [])]
    normalized_items = [s for s in normalized_items if s]
    # De-dupe while preserving order
    seen: set[str] = set()
    normalized_items = [s for s in normalized_items if not (s in seen or seen.add(s))]

    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        targets: List[str] = []
        skipped: List[str] = []
        for n in normalized_items:
            if n in job.results_by_normalized:
                targets.append(n)
            else:
                skipped.append(n)

    async def _run() -> None:
        # Set context for usage tracking
        _CURRENT_JOB_ID.set(req.job_id)
        
        for normalized in targets:
            async with _JOBS_LOCK:
                job2 = _JOBS.get(req.job_id)
                if not job2:
                    return
                r = job2.results_by_normalized.get(normalized)
                if not r:
                    continue
                item_type = r.get("type")
                full_url = r.get("normalized_full_url")

            try:
                if item_type == "domain":
                    await _reanalyze_domain(normalized)
                else:
                    await _reanalyze_url(full_url or normalized)

                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["scan_requested"] = True
            except Exception as e:
                async with _JOBS_LOCK:
                    job3 = _JOBS.get(req.job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["error"] = str(e)

    asyncio.create_task(_run())

    return {"scan_requested": True, "scheduled": len(targets), "skipped": len(skipped)}
