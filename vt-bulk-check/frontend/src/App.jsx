import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Shield, RotateCcw, RefreshCcw, Zap, X, Sun, Moon } from 'lucide-react'

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

export default function App() {
  const [rawInput, setRawInput] = useState('')
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [rowBusy, setRowBusy] = useState({})
  const [exporting, setExporting] = useState(false)
  const [autoForceScanStaleEnabled, setAutoForceScanStaleEnabled] = useState(true)
  const [autoScanError, setAutoScanError] = useState(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [selectedNormalized, setSelectedNormalized] = useState(() => new Set())
  const [theme, setTheme] = useState('dark')
  const pollRef = useRef(null)
  const autoTriggeredForJobIdRef = useRef(null)

  const total = job?.total ?? 0
  const processed = job?.processed ?? 0

  const progress = total > 0 ? processed / total : 0

  const results = job?.results ?? []
  const staleCount = useMemo(() => results.filter((r) => r.is_stale).length, [results])
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

  async function fetchJob(id) {
    // Avoid any intermediary caching while we rely on polling for UI truth.
    const resp = await fetch(`/api/vt-bulk-check/jobs/${id}?t=${Date.now()}`, { cache: 'no-store' })
    if (!resp.ok) throw new Error(await resp.text())
    return resp.json()
  }

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

  async function autoUpdateStaleNow() {
    if (!jobId) return
    setAutoScanError(null)
    try {
      const resp = await fetch(`/api/vt-bulk-check/jobs/${jobId}/auto-update-stale`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ job_id: jobId })
      })
      if (!resp.ok) throw new Error(await resp.text())
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

        // Single authoritative trigger: when job is done and stale exists, kick off auto-update.
        if (
          data?.status === 'done' &&
          autoForceScanStaleEnabled &&
          staleNow > 0 &&
          !uActive &&
          (uPhase == null || uPhase === 'complete' || uPhase === 'error') &&
          autoTriggeredForJobIdRef.current !== jobId
        ) {
          autoTriggeredForJobIdRef.current = jobId
          autoUpdateStaleNow()
        }

        const shouldKeepPolling =
          data?.status === 'running' ||
          uActive ||
          (uPhase != null && !uIsComplete) ||
          (autoForceScanStaleEnabled && staleNow > 0)

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
            <div>Request rate 4 lookups / min</div>
            <div>Daily quota 500 lookups / day</div>
            <div>Monthly quota 15.5 K lookups / month</div>
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
          <div className="row" style={{ justifyContent: 'space-between', gap: 10 }}>
            <label className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="checkbox"
                checked={autoForceScanStaleEnabled}
                onChange={(e) => setAutoForceScanStaleEnabled(e.target.checked)}
              />
              Auto request new scan for stale results
            </label>
            <div className="muted" style={{ whiteSpace: 'nowrap' }}>
              Stale: {staleCount}
            </div>
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
                    className={`linkRow rowHover ${severity} ${r.is_stale ? 'isStale' : ''} ${hasError ? 'hasError' : ''}`}
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
                            <span className="badge badgeBad" title={r.error}>ERROR</span>
                          ) : (
                            <span className={`badge ${severity === 'bad' ? 'badgeBad' : severity === 'warn' ? 'badgeWarn' : 'badgeOk'}`}>
                              {severity === 'bad' ? 'MALICIOUS' : severity === 'warn' ? 'FLAGGED' : 'CLEAN'}
                            </span>
                          )}
                          {r.is_stale ? <span className="badge badgeWarn">STALE</span> : <span className="badge badgeOk">FRESH</span>}
                          {r.scan_requested ? <span className="badge badgeInfo">SCAN_REQUESTED</span> : null}
                        </div>
                      </div>
                      {r.error ? <div className="err">{r.error}</div> : null}
                    </td>
                    <td>{r.type}</td>
                    <td className={`flaggingCell ${severity}`} style={{ fontWeight: 900 }}>{r.flagging_engines}</td>
                    <td>{r.total_engines}</td>
                    <td>{r.detection_ratio}</td>
                    <td>{r.last_scanned_display || '—'}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                        <button className="smallBtn" onClick={() => refreshOne(r.input)} disabled={!jobId}>
                          <RefreshCcw size={14} className={rowBusy[`refresh:${r.input}`] ? 'iconSpin' : undefined} />
                          Refresh report
                        </button>
                        <button className="smallBtn primary" onClick={() => forceScanOne(r.input, r.type)} disabled={!jobId}>
                          <RotateCcw size={14} className={rowBusy[`scan:${r.input}`] ? 'iconSpin' : undefined} />
                          Request new scan
                        </button>
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
            <button className="smallBtn" onClick={() => refreshOne(details.input)} disabled={!jobId}>
              <RefreshCcw size={14} className={rowBusy[`refresh:${details.input}`] ? 'iconSpin' : undefined} />
              Refresh report
            </button>
          </div>
        </>
      ) : null}
    </div>
  )
}
