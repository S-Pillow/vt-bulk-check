import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Shield, RotateCcw, RefreshCcw, Zap, X, Sun, Moon, Info, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react'

function splitInputs(raw) {
  const parts = (raw || '')
    .split(/[\n\r,\s]+/g)
    .map((s) => s.trim())
    .filter(Boolean)
  return parts
}

function formatPercent(n) {
  if (!Number.isFinite(n)) return '0%'
  return `${Math.round(n * 100)}%`
}

function ratioParts(ratio) {
  const m = String(ratio || '').match(/^(\d+)\/(\d+)$/)
  if (!m) return { a: 0, b: 0 }
  return { a: Number(m[1]), b: Number(m[2]) }
}

function getRowSeverity(r) {
  const malicious = Number(r?.malicious ?? 0)
  const suspicious = Number(r?.suspicious ?? 0)
  const flagging = Number(r?.flagging_engines ?? 0)

  if (malicious > 0 || flagging >= 10) return 'bad'
  if (suspicious > 0 || flagging > 0) return 'warn'
  return 'clean'
}

// Mirrors backend _normalize_item logic: any input containing "://" or "/"
// is classified as a URL for quota-estimation purposes.
function isUrlItem(s) {
  const t = s.trim()
  return t.includes('://') || t.includes('/')
}

function formatRunTime(seconds) {
  if (seconds < 60) return `~${Math.ceil(seconds)} sec`
  if (seconds < 3600) return `~${Math.ceil(seconds / 60)} min`
  const h = Math.floor(seconds / 3600)
  const m = Math.ceil((seconds % 3600) / 60)
  return `~${h} hr ${m} min`
}

export default function App() {
  const [rawInput, setRawInput] = useState('')
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [rowBusy, setRowBusy] = useState({})
  const [exporting, setExporting] = useState(false)
  const [autoForceScanStaleEnabled, setAutoForceScanStaleEnabled] = useState(false) // OFF by default for quota safety
  const [autoScanError, setAutoScanError] = useState(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [confirmModal, setConfirmModal] = useState(null) // { willRescan, dailyRemaining, onConfirm }
  const [rescanEligibility, setRescanEligibility] = useState(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [selectedNormalized, setSelectedNormalized] = useState(() => new Set())
  const [theme, setTheme] = useState('dark')
  const [usage, setUsage] = useState({
    jobLookupsUsed: 0,
    dailyLookupsUsed: 0,
    dailyLookupsLimit: 500,
    rateLimitPerMin: 4
  })
  const pollRef = useRef(null)
  const usagePollRef = useRef(null)
  const autoTriggeredForJobIdRef = useRef(null)

  const total = job?.total ?? 0
  const processed = job?.processed ?? 0

  const progress = total > 0 ? processed / total : 0

  const results = job?.results ?? []
  const staleCount = useMemo(() => results.filter((r) => r.is_stale).length, [results])
  const errorCount = useMemo(() => results.filter((r) => r.error).length, [results])
  const quotaErrorCount = useMemo(() => results.filter((r) => r.error && String(r.error).includes('429')).length, [results])
  const quotaResetLabel = useMemo(() => {
    if (quotaErrorCount === 0) return null
    const now = new Date()
    const midnightUTC = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1))
    const ms = midnightUTC - now
    if (ms < 60000) return 'in less than 1 minute'
    const h = Math.floor(ms / 3600000)
    const m = Math.floor((ms % 3600000) / 60000)
    return `in ${h} hr ${m} min`
  }, [quotaErrorCount])
  const update = job?.update || null

  const updateActive = Boolean(update?.active)
  const updateTotal = Number(update?.total ?? 0)
  const updateDone = Number(update?.done ?? 0)
  const updatePhase = update?.phase || null

  const progressPhaseLabel =
    updateActive
      ? (updatePhase === 'scanning' ? 'Scanning…' : updatePhase === 'refreshing' ? 'Refreshing reports…' : 'Updating…')
      : (job?.status === 'running' ? 'Running…' : job?.status === 'done' ? 'Complete' : job?.status === 'error' ? 'Error' : '—')

  const effectiveDone = updateActive ? updateDone : processed
  const effectiveTotal = updateActive ? updateTotal : total
  const effectiveProgress = effectiveTotal > 0 ? Math.min(1, effectiveDone / effectiveTotal) : 0

  useEffect(() => {
    const saved = window.localStorage.getItem('vt-bulk-check-theme')
    const initial = saved === 'light' || saved === 'dark' ? saved : 'dark'
    setTheme(initial)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    window.localStorage.setItem('vt-bulk-check-theme', theme)
  }, [theme])

  // Poll usage endpoint when job is running
  useEffect(() => {
    // Initial fetch even without job
    fetchUsage(jobId).then(setUsage).catch(() => {})

    if (!jobId) return

    let cancelled = false

    async function tick() {
      if (cancelled) return
      try {
        const data = await fetchUsage(jobId)
        if (!cancelled) setUsage(data)
      } catch (e) {
        // Silently ignore usage fetch errors
      }
    }

    // Poll every 3 seconds while job is running
    const shouldPoll = job?.status === 'running' || job?.update?.active
    if (shouldPoll) {
      usagePollRef.current = setInterval(tick, 3000)
    } else {
      // One final fetch when job completes
      tick()
    }

    return () => {
      cancelled = true
      if (usagePollRef.current) {
        clearInterval(usagePollRef.current)
        usagePollRef.current = null
      }
    }
  }, [jobId, job?.status, job?.update?.active])

  async function fetchJob(id) {
    // Avoid any intermediary caching while we rely on polling for UI truth.
    const resp = await fetch(`/api/vt-bulk-check/jobs/${id}?t=${Date.now()}`, { cache: 'no-store' })
    if (!resp.ok) throw new Error(await resp.text())
    return resp.json()
  }

  async function fetchUsage(jobIdParam) {
    const url = jobIdParam
      ? `/api/vt-bulk-check/usage?jobId=${encodeURIComponent(jobIdParam)}&t=${Date.now()}`
      : `/api/vt-bulk-check/usage?t=${Date.now()}`
    const resp = await fetch(url, { cache: 'no-store' })
    if (!resp.ok) throw new Error(await resp.text())
    return resp.json()
  }

  // Calculate daily usage percentage and warnings
  const dailyUsagePercent = usage.dailyLookupsLimit > 0
    ? (usage.dailyLookupsUsed / usage.dailyLookupsLimit) * 100
    : 0
  const dailyUsageWarning = dailyUsagePercent >= 95
    ? 'Almost out of lookups for today.'
    : dailyUsagePercent >= 80
      ? 'Approaching today\'s limit.'
      : null

  // Calculate estimated lookups before running
  const estimatedLookups = useMemo(() => {
    const items = splitInputs(rawInput)
    const unique = new Set(items.map(s => s.trim().toLowerCase()))
    return unique.size
  }, [rawInput])

  // URL-multiplier-aware estimated API request cost (domains = 1, URLs = up to 3)
  const { estimatedRequests, hasUrlItems } = useMemo(() => {
    const items = splitInputs(rawInput)
    const unique = [...new Set(items.map(s => s.trim().toLowerCase()))]
    const urlCount = unique.filter(isUrlItem).length
    return {
      estimatedRequests: (unique.length - urlCount) + urlCount * 3,
      hasUrlItems: urlCount > 0
    }
  }, [rawInput])

  const estimatedRunSeconds = usage.rateLimitPerMin > 0
    ? estimatedRequests * (60 / usage.rateLimitPerMin)
    : null

  const dailyRemaining = usage.dailyLookupsLimit > 0
    ? usage.dailyLookupsLimit - usage.dailyLookupsUsed
    : null
  const quotaWarnSoft = dailyRemaining !== null
    && estimatedRequests > 0
    && estimatedRequests > dailyRemaining
    && estimatedRequests <= usage.dailyLookupsLimit
  const quotaWarnHard = usage.dailyLookupsLimit > 0
    && estimatedRequests > usage.dailyLookupsLimit

  async function downloadCsv() {
    if (!jobId || exporting) return
    setExporting(true)
    setError(null)
    try {
      const resp = await fetch(`/api/vt-bulk-check/jobs/${jobId}/export`)
      if (!resp.ok) throw new Error(await resp.text())
      const blob = await resp.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `vt-bulk-check-${jobId}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.URL.revokeObjectURL(url)
    } catch (e) {
      setError(String(e?.message || e))
    } finally {
      setExporting(false)
    }
  }

  async function fetchRescanEligibility() {
    if (!jobId) return null
    try {
      const resp = await fetch(`/api/vt-bulk-check/jobs/${jobId}/rescan-eligibility`)
      if (!resp.ok) return null
      return resp.json()
    } catch (e) {
      return null
    }
  }

  async function autoUpdateStaleNow(skipConfirm = false) {
    if (!jobId) return
    setAutoScanError(null)
    
    // Check eligibility first to show confirmation if needed
    if (!skipConfirm) {
      const eligibility = await fetchRescanEligibility()
      if (eligibility) {
        setRescanEligibility(eligibility)
        const { willRescan, dailyLookupsRemaining, maxAutoRescansPerRun } = eligibility
        
        // Show confirmation if willRescan > 25 OR if daily remaining is low
        if (willRescan > 0 && (willRescan >= maxAutoRescansPerRun || dailyLookupsRemaining < willRescan * 2)) {
          setConfirmModal({
            willRescan,
            dailyRemaining: dailyLookupsRemaining,
            eligibility,
            onConfirm: () => {
              setConfirmModal(null)
              autoUpdateStaleNow(true) // Skip confirm on retry
            }
          })
          return
        }
      }
    }
    
    try {
      const resp = await fetch(`/api/vt-bulk-check/jobs/${jobId}/auto-update-stale`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId })
      })
      if (!resp.ok) throw new Error(await resp.text())
      const data = await resp.json()
      if (!data.scheduled && data.message) {
        setAutoScanError(data.message)
      }
    } catch (e) {
      setAutoScanError(String(e?.message || e))
    }
  }

  async function forceScanSelectedNow() {
    if (!jobId || bulkBusy) return
    const items = Array.from(selectedNormalized)
    if (items.length === 0) return
    setBulkBusy(true)
    setError(null)
    try {
      const resp = await fetch('/api/vt-bulk-check/force-scan-bulk', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, normalized_items: items })
      })
      if (!resp.ok) throw new Error(await resp.text())
      await fetchJob(jobId).then(setJob)
    } catch (e) {
      setError(String(e?.message || e))
    } finally {
      setBulkBusy(false)
    }
  }

  useEffect(() => {
    if (!jobId) return

    let cancelled = false

    async function tick() {
      try {
        const data = await fetchJob(jobId)
        if (cancelled) return
        setJob(data)
        const uActive = Boolean(data?.update?.active)
        const uPhase = data?.update?.phase
        const uIsComplete = !uActive && (uPhase === 'complete' || uPhase === 'error')
        const staleNow = (data?.results || []).filter((r) => r?.is_stale).length

        // Auto-trigger: when job is done and stale exists, kick off auto-rescan (with confirmation)
        // This is fire-and-forget - no additional polling needed after rescans are requested
        if (
          data?.status === 'done' &&
          autoForceScanStaleEnabled &&
          staleNow > 0 &&
          !uActive &&
          (uPhase == null || uPhase === 'complete' || uPhase === 'error') &&
          autoTriggeredForJobIdRef.current !== jobId
        ) {
          autoTriggeredForJobIdRef.current = jobId
          autoUpdateStaleNow() // Will show confirmation modal if needed
        }

        // Keep polling while job is running or update is active
        // NO longer need to poll indefinitely for stale items (fire-and-forget)
        const shouldKeepPolling =
          data?.status === 'running' ||
          uActive ||
          (uPhase != null && !uIsComplete)

        if (data.status === 'error' || (data.status === 'done' && !shouldKeepPolling)) {
          if (pollRef.current) {
            clearInterval(pollRef.current)
            pollRef.current = null
          }
        }
      } catch (e) {
        if (!cancelled) setError(String(e?.message || e))
      }
    }

    tick()
    pollRef.current = setInterval(tick, 2000)

    return () => {
      cancelled = true
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [jobId, autoForceScanStaleEnabled])

  async function onSubmit() {
    setSubmitting(true)
    setError(null)
    setAutoScanError(null)
    setJob(null)
    setSelected(null)
    setSelectedNormalized(new Set())
    autoTriggeredForJobIdRef.current = null

    try {
      const items = splitInputs(rawInput)
      const resp = await fetch('/api/vt-bulk-check/submit', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ items })
      })
      if (!resp.ok) throw new Error(await resp.text())
      const data = await resp.json()
      setJobId(data.job_id)
    } catch (e) {
      setError(String(e?.message || e))
    } finally {
      setSubmitting(false)
    }
  }

  async function refreshOne(item) {
    if (!jobId) return
    setRowBusy((s) => ({ ...s, [`refresh:${item}`]: true }))
    setError(null)
    try {
      const resp = await fetch('/api/vt-bulk-check/refresh', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, item })
      })
      if (!resp.ok) throw new Error(await resp.text())
      await fetchJob(jobId).then(setJob)
    } catch (e) {
      setError(String(e?.message || e))
    } finally {
      setRowBusy((s) => {
        const n = { ...s }
        delete n[`refresh:${item}`]
        return n
      })
    }
  }

  async function forceScanOne(item, type) {
    if (!jobId) return
    setRowBusy((s) => ({ ...s, [`scan:${item}`]: true }))
    setError(null)
    try {
      const resp = await fetch('/api/vt-bulk-check/force-scan', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, item, type })
      })
      if (!resp.ok) throw new Error(await resp.text())
      await fetchJob(jobId).then(setJob)
    } catch (e) {
      setError(String(e?.message || e))
    } finally {
      setRowBusy((s) => {
        const n = { ...s }
        delete n[`scan:${item}`]
        return n
      })
    }
  }

  function closeDrawer() {
    setSelected(null)
  }

  const selectedRow = selected
    ? results.find((r) => r.normalized === selected.normalized && r.type === selected.type)
    : null

  const details = selectedRow || selected

  const detRatio = ratioParts(details?.detection_ratio)
  const detPct = detRatio.b > 0 ? detRatio.a / detRatio.b : 0
  const detSeverity = getRowSeverity(details)

  return (
    <div className="container">
      <div className="header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Shield className="h-6 w-6" style={{ color: 'var(--danger)' }} />
          <h1 className="h1">VirusTotal Bulk Check</h1>
        </div>
        <div className="row" style={{ gap: 10 }}>
          <button
            className="smallBtn"
            onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
            aria-label="Toggle dark mode"
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
            {theme === 'dark' ? 'Light mode' : 'Dark mode'}
          </button>
          <a
            className="smallBtn"
            href="https://forgeforward.app/tools/"
            target="_self"
            rel="noopener noreferrer"
            aria-label="Back to Tools"
          >
            <ArrowLeft size={14} />
            Back to Tools
          </a>
          <div className="muted">/tools/vt-bulk-check/</div>
        </div>
      </div>

      <div className="grid">
        <div className="card cardAccent">
          <div className="label">Paste domains and URLs</div>
          <textarea
            className="textarea"
            value={rawInput}
            onChange={(e) => setRawInput(e.target.value)}
            placeholder={`example.biz\nhttps://example.biz/path\nexample.biz/path`}
          />
          {estimatedLookups > 0 && (
            <>
              <div className="estimatedLookups" style={{ marginTop: 8, marginBottom: 4 }}>
                <span
                  className="muted"
                  title="Estimate is based on unique items entered. Extra actions like 'Request new scan' and 'Refresh report' use additional lookups."
                >
                  Estimated lookups: {estimatedLookups}
                  <Info size={12} style={{ marginLeft: 4, verticalAlign: 'middle', opacity: 0.7 }} />
                </span>
                {autoForceScanStaleEnabled && (
                  <span
                    className="muted"
                    style={{ marginLeft: 8 }}
                    title="Stale items are determined after the first lookup. This is a maximum estimate."
                  >
                    + up to {estimatedLookups} additional lookups (stale rescans)
                    <Info size={12} style={{ marginLeft: 4, verticalAlign: 'middle', opacity: 0.7 }} />
                  </span>
                )}
              </div>
              {hasUrlItems && (
                <div className="muted" style={{ marginTop: 2, marginBottom: 4, fontSize: '12px' }}>
                  <Info size={12} style={{ verticalAlign: 'middle', marginRight: 4, opacity: 0.7 }} />
                  Includes URL items — estimated up to 3 API requests each. Estimated total: <strong>{estimatedRequests}</strong> API requests.
                </div>
              )}
              {estimatedRunSeconds !== null && (
                <div className="muted" style={{ marginTop: 2, marginBottom: 4, fontSize: '12px' }}>
                  Est. run time: {formatRunTime(estimatedRunSeconds)}
                </div>
              )}
            </>
          )}
          {quotaWarnHard && (
            <div className="errorBanner" style={{ marginTop: 8 }}>
              <AlertTriangle size={16} style={{ flexShrink: 0 }} />
              <span>
                This job is estimated to use <strong>~{estimatedRequests} API requests</strong>, which exceeds the daily quota of {usage.dailyLookupsLimit}. Submission will be rejected. Please reduce or split your list.
              </span>
            </div>
          )}
          {quotaWarnSoft && (
            <div className="warnBanner" style={{ marginTop: 8 }}>
              <AlertTriangle size={16} style={{ flexShrink: 0 }} />
              <span>
                This job is estimated to use <strong>~{estimatedRequests} API requests</strong>. You have ~{dailyRemaining} remaining today. Some results may come back as errors if quota is exhausted mid-run.
              </span>
            </div>
          )}
          <div style={{ height: 12 }} />
          <div className="row">
            <button className="btn btnPrimary" onClick={onSubmit} disabled={submitting}>
              <Zap size={16} />
              Run check
            </button>
            {jobId ? (
              <>
                <div className="muted">Job: {jobId}</div>
                <button
                  className="smallBtn exportBtn"
                  onClick={downloadCsv}
                  disabled={exporting || !jobId || job?.status === 'running'}
                  title={job?.status === 'running' ? 'Export available when job completes' : 'Download CSV (domain, flagging)'}
                >
                  {exporting ? 'Exporting…' : 'Export CSV'}
                </button>
              </>
            ) : (
              <div className="muted">Enter items then run.</div>
            )}
          </div>
          {error ? <div style={{ marginTop: 10 }} className="err">{error}</div> : null}
        </div>

        <div className="card cardAccent">
          <div className="label">API Limits</div>
          <div className="limits">
            <div>Rate limit: {usage.rateLimitPerMin} / min</div>
            <div>Daily quota: {usage.dailyLookupsLimit} lookups / day</div>
            <div>Monthly quota: 15.5 K lookups / month</div>
          </div>

          <div style={{ height: 14 }} />
          <div className="label">Usage</div>
          
          {/* Lookups this run */}
          <div className="usageRow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <span
              className="muted"
              title="Counts VirusTotal requests made by this tool during the current run."
              style={{ cursor: 'help' }}
            >
              Lookups this run: <strong>{usage.jobLookupsUsed}</strong>
              <Info size={12} style={{ marginLeft: 4, verticalAlign: 'middle', opacity: 0.7 }} />
            </span>
          </div>
          
          {/* Daily usage with progress bar */}
          <div className="usageRow" style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <span
                className="muted"
                title="Your VirusTotal daily allowance resets at 00:00 UTC."
                style={{ cursor: 'help' }}
              >
                Daily usage: <strong>{usage.dailyLookupsUsed} / {usage.dailyLookupsLimit}</strong> ({Math.round(dailyUsagePercent)}%)
                <Info size={12} style={{ marginLeft: 4, verticalAlign: 'middle', opacity: 0.7 }} />
              </span>
            </div>
            <div className="progress" style={{ marginTop: 4 }}>
              <div
                className={`progressbar ${dailyUsagePercent >= 95 ? 'danger' : dailyUsagePercent >= 80 ? 'warning' : ''}`}
                aria-label="daily usage"
              >
                <div style={{ width: `${Math.min(100, dailyUsagePercent)}%` }} />
              </div>
            </div>
            {dailyUsageWarning && (
              <div className={`usageWarning ${dailyUsagePercent >= 95 ? 'danger' : 'warning'}`} style={{ marginTop: 6, fontSize: '0.85em' }}>
                {dailyUsageWarning}
              </div>
            )}
          </div>

          <div style={{ height: 14 }} />
          <div className="label">Progress</div>
          <div className="progress">
            <div className="progressbar" aria-label="progress">
              <div style={{ width: `${Math.round(effectiveProgress * 100)}%` }} />
            </div>
            <div className="muted" style={{ whiteSpace: 'nowrap' }}>
              {effectiveDone}/{effectiveTotal} ({formatPercent(effectiveProgress)})
            </div>
          </div>
          <div style={{ height: 12 }} />
          <div className="muted" style={{ marginBottom: 6 }}>
            Status: <span style={{ fontWeight: 800 }}>{progressPhaseLabel}</span>
            {updateActive && update?.message ? <span> · {update.message}</span> : null}
          </div>
          <div className="muted" style={{ marginBottom: 10 }}>
            Stale items: <strong>{staleCount}</strong>
          </div>
          
          {/* Advanced / Quota & Freshness Section */}
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, marginTop: 8 }}>
            <button
              className="advancedToggle"
              onClick={() => setAdvancedOpen(!advancedOpen)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                background: 'none',
                border: 'none',
                color: 'var(--muted)',
                cursor: 'pointer',
                padding: 0,
                fontSize: '12px',
                fontWeight: 700,
                textTransform: 'uppercase',
                letterSpacing: '0.08em'
              }}
            >
              {advancedOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              Advanced / Quota & Freshness
            </button>
            
            {advancedOpen && (
              <div style={{ marginTop: 12, padding: '12px', background: 'rgba(0,0,0,0.15)', borderRadius: 8 }}>
                <label
                  style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}
                  title={`Only rescans items older than 7 days. Capped at 25 rescans per run. Fire-and-forget: you must manually refresh to see updated results.`}
                >
                  <input
                    type="checkbox"
                    checked={autoForceScanStaleEnabled}
                    onChange={(e) => setAutoForceScanStaleEnabled(e.target.checked)}
                    style={{ marginTop: 2 }}
                  />
                  <div>
                    <span style={{ fontWeight: 700, color: 'var(--text)' }}>
                      Auto rescan stale results
                      <span style={{ color: 'var(--warn)', marginLeft: 6 }}>(uses extra lookups)</span>
                    </span>
                    <div className="muted" style={{ fontSize: '12px', marginTop: 4, lineHeight: 1.4 }}>
                      Requests rescans now. You'll need to manually refresh reports later to see updated results.
                    </div>
                    <div className="muted" style={{ fontSize: '11px', marginTop: 4, opacity: 0.8 }}>
                      Only items older than 7 days · Max 25 per run
                    </div>
                  </div>
                </label>
              </div>
            )}
          </div>
          
          {autoScanError ? <div style={{ marginTop: 10 }} className="err">{autoScanError}</div> : null}
        </div>
      </div>

      <div style={{ height: 16 }} />

      <div className="card cardAccent">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div className="label" style={{ margin: 0 }}>Results</div>
            <div className="muted">Click a row for details</div>
          </div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <button
              className="smallBtn"
              onClick={() => {
                const all = new Set(results.map((r) => r.normalized))
                setSelectedNormalized(all)
              }}
              disabled={results.length === 0}
              title="Select all rows"
            >
              Select all
            </button>
            <button
              className="smallBtn"
              onClick={() => setSelectedNormalized(new Set())}
              disabled={selectedNormalized.size === 0}
              title="Clear selection"
            >
              Clear
            </button>
            <button
              className="smallBtn primary"
              onClick={forceScanSelectedNow}
              disabled={!jobId || selectedNormalized.size === 0 || bulkBusy}
              title="Request new scan for selected rows"
            >
              {bulkBusy ? 'Scheduling…' : `Request new scan (${selectedNormalized.size})`}
            </button>
            <button
              className="smallBtn"
              onClick={autoUpdateStaleNow}
              disabled={!jobId || staleCount === 0}
              title="Request new scan for all stale rows"
            >
              Request new scan (stale)
            </button>
          </div>
        </div>

        <div style={{ height: 8 }} />

        {/* Error summary banner */}
        {errorCount > 0 && (
          <div className="errorBanner">
            <AlertTriangle size={16} style={{ flexShrink: 0 }} />
            <div>
              <strong>{errorCount} lookup{errorCount !== 1 ? 's' : ''} failed</strong>
              {quotaErrorCount > 0 && (
                <span> — {quotaErrorCount} due to daily quota (429). These domains were <strong>not checked</strong>. Results show <strong>—</strong> not 0.{quotaResetLabel && ` Quota resets at 00:00 UTC — ${quotaResetLabel}.`}</span>
              )}
              {quotaErrorCount === 0 && (
                <span> — these domains were not checked. Use "Retry" on each row or re-run when the issue is resolved.</span>
              )}
            </div>
          </div>
        )}

        <div className="tableWrap">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 42 }}>
                  <input
                    type="checkbox"
                    aria-label="Select all results"
                    checked={results.length > 0 && selectedNormalized.size === results.length}
                    onChange={(e) => {
                      if (e.target.checked) setSelectedNormalized(new Set(results.map((r) => r.normalized)))
                      else setSelectedNormalized(new Set())
                    }}
                    onClick={(e) => e.stopPropagation()}
                  />
                </th>
                <th>Input</th>
                <th>Type</th>
                <th>Flagging</th>
                <th>Total</th>
                <th>Ratio</th>
                <th>Last scanned</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {results.length === 0 ? (
                <tr>
                  <td colSpan={8} className="muted">No results yet.</td>
                </tr>
              ) : (
                results.map((r) => {
                  const severity = getRowSeverity(r)
                  const hasError = Boolean(r?.error)
                  return (
                  <tr
                    key={`${r.type}:${r.normalized}`}
                    className={`linkRow rowHover ${hasError ? '' : severity} ${r.is_stale ? 'isStale' : ''} ${hasError ? 'hasError' : ''}`}
                    onClick={() => setSelected(r)}
                    title="Open details"
                  >
                    <td onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Select ${r.input}`}
                        checked={selectedNormalized.has(r.normalized)}
                        onChange={(e) => {
                          setSelectedNormalized((prev) => {
                            const next = new Set(prev)
                            if (e.target.checked) next.add(r.normalized)
                            else next.delete(r.normalized)
                            return next
                          })
                        }}
                      />
                    </td>
                    <td>
                      <div className="inputCell">
                        <div className="inputMain">{r.input}</div>
                        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                          {r.error ? (
                            <span className="badge badgeBad">NOT CHECKED</span>
                          ) : (
                            <span className={`badge ${severity === 'bad' ? 'badgeBad' : severity === 'warn' ? 'badgeWarn' : 'badgeOk'}`}>
                              {severity === 'bad' ? 'MALICIOUS' : severity === 'warn' ? 'FLAGGED' : 'CLEAN'}
                            </span>
                          )}
                          {!r.error && (r.is_stale ? <span className="badge badgeWarn">STALE</span> : <span className="badge badgeOk">FRESH</span>)}
                          {r.scan_requested ? <span className="badge badgeInfo">SCAN_REQUESTED</span> : null}
                        </div>
                      </div>
                      {r.error ? (
                        <div className="errorDetail">
                          <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 1 }} />
                          <span>{String(r.error).includes('429') ? 'Quota exceeded (429) — not checked' : r.error}</span>
                        </div>
                      ) : null}
                    </td>
                    <td>{r.type}</td>
                    <td className={`flaggingCell ${hasError ? '' : severity}`} style={{ fontWeight: 900 }}>
                      {hasError ? <span className="muted">—</span> : r.flagging_engines}
                    </td>
                    <td>{hasError ? <span className="muted">—</span> : r.total_engines}</td>
                    <td>{hasError ? <span className="muted">—</span> : r.detection_ratio}</td>
                    <td>{r.last_scanned_display || '—'}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                        {hasError ? (
                          <button
                            className="smallBtn retryBtn"
                            onClick={() => refreshOne(r.input)}
                            disabled={!jobId}
                            title="Retry this lookup (uses 1 lookup)."
                          >
                            <RefreshCcw size={14} className={rowBusy[`refresh:${r.input}`] ? 'iconSpin' : undefined} />
                            Retry
                          </button>
                        ) : (
                          <>
                            <button
                              className="smallBtn"
                              onClick={() => refreshOne(r.input)}
                              disabled={!jobId}
                              title="Fetch the latest report (uses 1 lookup)."
                            >
                              <RefreshCcw size={14} className={rowBusy[`refresh:${r.input}`] ? 'iconSpin' : undefined} />
                              Refresh report
                            </button>
                            <button
                              className="smallBtn primary"
                              onClick={() => forceScanOne(r.input, r.type)}
                              disabled={!jobId}
                              title="Request a new scan (uses 1 lookup). Results may take a few minutes."
                            >
                              <RotateCcw size={14} className={rowBusy[`scan:${r.input}`] ? 'iconSpin' : undefined} />
                              Request new scan
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                )})
              )}
            </tbody>
          </table>
        </div>
      </div>

      {details ? (
        <>
          <div className="drawerBackdrop" onClick={closeDrawer} />
          <div className="drawer">
            <div className="drawerHeader">
              <div>
                <div className="label" style={{ margin: 0 }}>VirusTotal Results</div>
                <div style={{ fontWeight: 900, marginTop: 6 }}>{details.input}</div>
                <div className="muted">Type: {details.type}</div>
              </div>
              <button className="smallBtn" onClick={closeDrawer}>
                <X size={14} />
                Close
              </button>
            </div>

            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div />
              <button
                className="smallBtn primary"
                onClick={() => forceScanOne(details.input, details.type)}
                disabled={!jobId}
              >
                <RotateCcw size={14} />
                Request new scan
              </button>
            </div>

            <div style={{ height: 8 }} />
            <div className="muted">
              Refresh report pulls the latest available report. New scan requests may take time to show updated results.
            </div>

            <div style={{ height: 12 }} />

            <div className="tiles">
              <div className="tile">
                <div className="k">Malicious</div>
                <div className="v vBad">{details.malicious}</div>
              </div>
              <div className="tile">
                <div className="k">Suspicious</div>
                <div className="v vWarn">{details.suspicious}</div>
              </div>
              <div className="tile">
                <div className="k">Harmless</div>
                <div className="v vOk">{details.harmless}</div>
              </div>
              <div className="tile">
                <div className="k">Total Engines</div>
                <div className="v">{details.total_engines}</div>
              </div>
            </div>

            <div style={{ height: 12 }} />

            <div className="label">Detection Rate</div>
            <div className="progress" style={{ alignItems: 'center' }}>
              <div className={`progressbar ${detSeverity}`}>
                <div style={{ width: `${Math.round(detPct * 100)}%` }} />
              </div>
              <div className="muted" style={{ whiteSpace: 'nowrap' }}>{details.detection_ratio}</div>
            </div>

            <div style={{ height: 12 }} />
            <div className="muted">Last scanned: {details.last_scanned_display || '—'}</div>
            {details.scan_requested ? <div className="muted">Scan requested</div> : null}
            {details.error ? <div className="err" style={{ marginTop: 8 }}>{details.error}</div> : null}
            <div style={{ height: 12 }} />
            <button
              className="smallBtn"
              onClick={() => refreshOne(details.input)}
              disabled={!jobId}
              title="Fetch the latest report (uses 1 lookup)."
            >
              <RefreshCcw size={14} className={rowBusy[`refresh:${details.input}`] ? 'iconSpin' : undefined} />
              Refresh report
            </button>
          </div>
        </>
      ) : null}

      {/* Confirmation Modal for Auto-Rescan */}
      {confirmModal && (
        <>
          <div className="drawerBackdrop" onClick={() => setConfirmModal(null)} />
          <div className="confirmModal">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
              <AlertTriangle size={24} style={{ color: 'var(--warn)' }} />
              <div className="label" style={{ margin: 0, fontSize: 14 }}>Confirm Auto-Rescan</div>
            </div>
            
            <div style={{ marginBottom: 16 }}>
              <p style={{ margin: '0 0 12px 0' }}>
                This will use up to <strong style={{ color: 'var(--warn)' }}>{confirmModal.willRescan} additional lookup(s)</strong> now.
              </p>
              
              {confirmModal.eligibility && (
                <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
                  <div>• {confirmModal.eligibility.totalStale} stale items total</div>
                  <div>• {confirmModal.eligibility.eligibleCount} eligible (older than {confirmModal.eligibility.autoRescanThresholdDays} days)</div>
                  <div>• {confirmModal.willRescan} will be rescanned (max {confirmModal.eligibility.maxAutoRescansPerRun} per run)</div>
                  {confirmModal.eligibility.skippedDueToCap > 0 && (
                    <div style={{ color: 'var(--warn)' }}>• {confirmModal.eligibility.skippedDueToCap} will be skipped (cap reached)</div>
                  )}
                </div>
              )}
              
              {confirmModal.dailyRemaining < confirmModal.willRescan * 2 && (
                <div style={{ marginTop: 12, padding: '8px 12px', background: 'rgba(239,68,68,0.15)', borderRadius: 6, color: 'var(--danger)', fontSize: 13 }}>
                  <strong>Warning:</strong> Daily quota is low ({confirmModal.dailyRemaining} lookups remaining).
                </div>
              )}
              
              <div className="muted" style={{ marginTop: 12, fontSize: 12 }}>
                Note: You'll need to manually click "Refresh report" on each item to see updated results.
              </div>
            </div>
            
            <div className="row" style={{ justifyContent: 'flex-end', gap: 10 }}>
              <button className="smallBtn" onClick={() => setConfirmModal(null)}>
                Cancel
              </button>
              <button className="smallBtn primary" onClick={confirmModal.onConfirm}>
                Continue ({confirmModal.willRescan} lookups)
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
