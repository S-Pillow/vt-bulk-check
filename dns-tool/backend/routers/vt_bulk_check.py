import os
import asyncio
import base64
import csv
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("vt_bulk_check")

router = APIRouter(prefix="/vt-bulk-check")


class SubmitRequest(BaseModel):
    items: List[str]


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


def _format_last_scanned(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return f"{dt.month}/{dt.day}/{dt.year}, {dt.strftime('%-I:%M:%S %p')}"


def _is_stale(ts: Optional[int], threshold_days: int = 5) -> bool:
    if not ts:
        return True
    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - ts) > (threshold_days * 86400)


def _normalize_item(raw: str) -> Tuple[str, Literal["domain", "url"], str]:
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
    return domain, "domain", domain


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


async def _process_job(job_id: str) -> None:
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        logger.warning(f"Job {job_id} not found in _process_job")
        return

    logger.info(f"Starting job processing: {job_id} with {job.total} items")
    
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

        async with _JOBS_LOCK:
            job3 = _JOBS.get(job_id)
            if job3:
                job3.status = "done"
                logger.info(f"Job {job_id} completed successfully: {job3.processed}/{job3.total} items processed")

    except Exception as e:
        logger.error(f"Fatal error in job {job_id}: {type(e).__name__}: {str(e)}", exc_info=True)
        async with _JOBS_LOCK:
            job4 = _JOBS.get(job_id)
            if job4:
                job4.status = "error"
                job4.error_message = str(e)


def _job_results_list(job: JobState) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in job.item_order:
        r = job.results_by_normalized.get(key)
        if not r:
            continue
        r2 = {k: v for k, v in r.items() if k != "normalized_full_url"}
        out.append(r2)
    return out


async def _auto_update_stale_job(job_id: str) -> None:
    """
    Background task that ensures stale items are fully updated:
    1) Request a new scan (reanalyze) for each stale item
    2) Repeatedly refresh reports until VT returns a newer last_analysis_date than baseline
    """
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if job.update_active:
            return

        targets = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]
        # If the job is currently "done", temporarily flip it back to "running" while we
        # re-scan and refresh stale items. This prevents clients that stop polling on
        # status=="done" from missing the update phases.
        if targets and job.status == "done":
            job.status = "running"
        job.update_active = True
        job.update_phase = "scanning"
        job.update_total = len(targets)
        job.update_done = 0
        job.update_message = f"Rescanning stale items… 0/{len(targets)}" if targets else "Complete"
        job.update_started_at = time.monotonic()
        job.update_error = None
        job.update_baseline_by_normalized = {
            k: (job.results_by_normalized.get(k, {}) or {}).get("last_analysis_date") for k in targets
        }

    if not targets:
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.update_active = False
                job.update_phase = "complete"
                job.update_total = 0
                job.update_done = 0
                job.update_message = "Complete"
                job.update_error = None
                job.update_baseline_by_normalized = {}
                if job.status != "error":
                    job.status = "done"
        return

    # Phase 1: request re-analyze
    scan_done = 0
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
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                if normalized in job3.results_by_normalized:
                    job3.results_by_normalized[normalized]["scan_requested"] = True
        except Exception as e:
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                if normalized in job3.results_by_normalized:
                    job3.results_by_normalized[normalized]["error"] = str(e)
        finally:
            scan_done += 1
            async with _JOBS_LOCK:
                job3 = _JOBS.get(job_id)
                if not job3:
                    return
                job3.update_done = scan_done
                job3.update_message = f"Rescanning stale items… {scan_done}/{job3.update_total}"

    # Phase 2: refresh until updated
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update_phase = "refreshing"
        job.update_done = 0
        job.update_message = f"Refreshing reports… 0/{job.update_total} updated"

    # We'll wait up to ~30 minutes for fresh reports to land.
    deadline = time.monotonic() + (30 * 60)
    updated: set[str] = set()

    while time.monotonic() < deadline:
        # Early exit if job was deleted
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return
            baselines = dict(job.update_baseline_by_normalized)
            current_total = job.update_total

        # Refresh items not yet updated
        for normalized in targets:
            if normalized in updated:
                continue

            async with _JOBS_LOCK:
                job2 = _JOBS.get(job_id)
                if not job2:
                    return
                r = job2.results_by_normalized.get(normalized) or {}
                item_type = r.get("type")
                input_value = r.get("input", normalized)
                full_url = r.get("normalized_full_url")

            try:
                if item_type == "domain":
                    report = await _fetch_domain_report(normalized)
                else:
                    report = await _fetch_url_report(full_url or normalized)

                refreshed = _parse_analysis_from_report(input_value, normalized, item_type, report)
                if item_type == "url":
                    refreshed["normalized_full_url"] = full_url

                # Determine if this is a "new" analysis vs baseline (or at least no longer stale)
                baseline_ts = baselines.get(normalized)
                new_ts = refreshed.get("last_analysis_date")
                is_newer = False
                if baseline_ts is None and new_ts is not None:
                    is_newer = True
                elif baseline_ts is not None and new_ts is not None and int(new_ts) > int(baseline_ts):
                    is_newer = True
                is_fresh = not bool(refreshed.get("is_stale"))

                async with _JOBS_LOCK:
                    job3 = _JOBS.get(job_id)
                    if not job3:
                        return
                    job3.results_by_normalized[normalized] = refreshed
                    if is_newer or is_fresh:
                        updated.add(normalized)
                        job3.update_done = len(updated)
                        job3.update_message = f"Refreshing reports… {job3.update_done}/{current_total} updated"
            except Exception as e:
                async with _JOBS_LOCK:
                    job3 = _JOBS.get(job_id)
                    if not job3:
                        return
                    if normalized in job3.results_by_normalized:
                        job3.results_by_normalized[normalized]["error"] = str(e)

        if len(updated) >= len(targets):
            break

        # Avoid tight loops; VT updates can take a bit.
        await asyncio.sleep(5.0)

    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if len(updated) >= len(targets):
            job.update_phase = "complete"
            job.update_message = "Complete"
            # Restore job to done once all refreshed results are applied
            if job.status != "error":
                job.status = "done"
        else:
            job.update_phase = "error"
            job.update_error = "Timed out waiting for refreshed reports"
            job.update_message = f"Refreshing reports… {len(updated)}/{len(targets)} updated (timed out)"
            job.status = "error"
            job.error_message = job.update_error
        job.update_active = False

@router.post("/submit")
async def submit(req: SubmitRequest):
    rejected: List[str] = []
    accepted_items: List[Tuple[str, Literal["domain", "url"], str, str]] = []
    seen: set[str] = set()

    logger.info(f"Received submit request with {len(req.items or [])} items")

    for raw in req.items or []:
        try:
            normalized, item_type, normalized_full = _normalize_item(raw)
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            accepted_items.append((raw, item_type, normalized, normalized_full))
        except Exception as e:
            logger.warning(f"Rejected item '{raw}': {type(e).__name__}: {str(e)}")
            rejected.append(raw)

    job_id = str(uuid.uuid4())
    logger.info(f"Created job {job_id}: {len(accepted_items)} accepted, {len(rejected)} rejected")

    job = JobState(job_id=job_id, status="running", processed=0, total=len(accepted_items))
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

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["domain", "flagging"])
    for r in results:
        writer.writerow([r.get("input", ""), r.get("flagging_engines", 0)])

    csv_content = buf.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="vt-bulk-check-{job_id}.csv"'
        },
    )


@router.post("/jobs/{job_id}/auto-update-stale")
async def auto_update_stale(job_id: str, _req: AutoUpdateStaleRequest):
    # Keep job_id both in path and body to make clients explicit
    if _req.job_id != job_id:
        raise HTTPException(status_code=400, detail="job_id mismatch")

    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.update_active:
            return {"scheduled": False, "message": "Auto-update already in progress"}
        stale_targets = [k for k in job.item_order if job.results_by_normalized.get(k, {}).get("is_stale")]
        if not stale_targets:
            # Nothing to do; keep job stable and mark update as complete/no-op.
            job.update_active = False
            job.update_phase = "complete"
            job.update_total = 0
            job.update_done = 0
            job.update_message = "Complete"
            job.update_error = None
            job.update_baseline_by_normalized = {}
            if job.status != "error":
                job.status = "done"
            return {"scheduled": False, "message": "No stale items"}

    asyncio.create_task(_auto_update_stale_job(job_id))
    return {"scheduled": True}


@router.post("/refresh")
async def refresh(req: RefreshRequest):
    try:
        normalized, item_type, normalized_full = _normalize_item(req.item)
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
    try:
        normalized, _, normalized_full = _normalize_item(req.item)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid item")

    async with _JOBS_LOCK:
        job = _JOBS.get(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        existing = job.results_by_normalized.get(normalized)
        if not existing:
            raise HTTPException(status_code=404, detail="Item not found in job")

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
