import { useCallback, useEffect, useMemo, useState } from 'react'
import { open as openNativeDialog } from '@tauri-apps/plugin-dialog'
import { isTauri } from '@tauri-apps/api/core'
import {
  createCase,
  deleteCase,
  exportCaseManifest,
  getCase,
  getCaseEvidence,
  getCases,
  getCaseTimeline,
  getForensicTimeline,
  generateCaseReport,
  importCaseEvidence,
  verifyCaseEvidence,
} from '../api/client'
import {
  forensicReportState,
  normalizeForensicTimeline,
  redactSensitiveText,
  sourceCitationText,
} from './casesForensics'

const SELECTED_CASE_STORAGE_KEY = 'custody_selected_case_id'

function caseId(value) {
  return value?.id ?? value?.case_id ?? value?.uuid ?? null
}

function caseName(value) {
  return String(value?.name ?? value?.title ?? `Case ${caseId(value) ?? ''}`).trim()
}

function unwrapCases(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.cases)) return payload.cases
  if (Array.isArray(payload?.items)) return payload.items
  return []
}

function unwrapCase(payload) {
  return payload?.case && typeof payload.case === 'object' ? payload.case : payload
}

function unwrapEvidence(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.evidence)) return payload.evidence
  if (Array.isArray(payload?.manifest)) return payload.manifest
  if (Array.isArray(payload?.rows)) return payload.rows
  return []
}

function unwrapTimeline(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.events)) return payload.events
  if (Array.isArray(payload?.timeline)) return payload.timeline
  return []
}

function firstNumber(source, keys) {
  for (const key of keys) {
    const value = Number(source?.[key])
    if (Number.isFinite(value)) return value
  }
  return 0
}

function summaryCounts(payload) {
  const source = payload?.summary && typeof payload.summary === 'object' ? payload.summary : payload || {}
  return {
    verified: firstNumber(source, ['verified', 'verified_count', 'ok']),
    changed: firstNumber(source, ['changed', 'changed_count', 'modified']),
    missing: firstNumber(source, ['missing', 'missing_count']),
    unreadable: firstNumber(source, ['unreadable', 'unreadable_count']),
    imported: firstNumber(source, ['imported', 'imported_count', 'added', 'created']),
    skipped: firstNumber(source, ['skipped', 'skipped_count', 'duplicates']),
    failed: firstNumber(source, ['failed', 'failed_count', 'errors']),
  }
}

function formatCount(value) {
  return Number(value || 0).toLocaleString()
}

function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

function formatMtime(value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value))) {
    const numeric = Number(value)
    const milliseconds = numeric > 1e14 ? numeric / 1e6 : numeric > 1e11 ? numeric : numeric * 1000
    return formatDate(milliseconds)
  }
  return formatDate(value)
}

function basename(path) {
  return String(path || '').split(/[\\/]/).filter(Boolean).pop() || '—'
}

function hashSnippet(value) {
  const hash = String(value || '')
  if (!hash) return '—'
  return hash.length > 18 ? `${hash.slice(0, 18)}…` : hash
}

function evidencePath(row) {
  return row?.path ?? row?.file_path ?? row?.source_path ?? row?.original_path ?? ''
}

function evidenceHash(row) {
  return row?.sha256 ?? row?.hash ?? row?.digest ?? row?.evidence_sha256 ?? ''
}

function evidenceSize(row) {
  return row?.size_bytes ?? row?.size ?? row?.file_size ?? row?.evidence_size_bytes
}

function evidenceMtime(row) {
  return row?.mtime_ns ?? row?.mtime ?? row?.modified_at ?? row?.evidence_mtime_ns
}

function evidenceStatus(row) {
  return String(row?.status ?? row?.acquisition_status ?? row?.state ?? 'acquired')
}

function EvidenceTable({ rows }) {
  if (!rows.length) {
    return <div className="case-empty-inline">No evidence has been imported into this case yet.</div>
  }

  return (
    <div className="case-table-wrap">
      <table className="case-table">
        <thead>
          <tr>
            <th>filename</th>
            <th>path</th>
            <th>sha-256</th>
            <th>size</th>
            <th>mtime</th>
            <th>status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const path = evidencePath(row)
            const hash = evidenceHash(row)
            return (
              <tr key={row.id ?? row.evidence_id ?? `${path}-${index}`}>
                <td className="case-table-strong" title={path}>{row.filename ?? row.file_name ?? basename(path)}</td>
                <td className="case-table-path" title={path}>{path || '—'}</td>
                <td className="case-table-mono" title={hash}>{hashSnippet(hash)}</td>
                <td>{formatBytes(evidenceSize(row))}</td>
                <td>{formatMtime(evidenceMtime(row))}</td>
                <td><span className={`case-status case-status-${evidenceStatus(row).toLowerCase()}`}>{evidenceStatus(row)}</span></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function SummaryCards({ title, counts, includeVerification = false }) {
  const metrics = includeVerification
    ? [
      ['verified', counts.verified],
      ['changed', counts.changed],
      ['missing', counts.missing],
      ['unreadable', counts.unreadable],
    ]
    : [
      ['imported', counts.imported],
      ['skipped', counts.skipped],
      ['failed', counts.failed],
    ]

  return (
    <div className="case-summary-block">
      <div className="case-summary-heading">{title}</div>
      <div className="case-summary-grid">
        {metrics.map(([label, value]) => (
          <div key={label} className="case-summary-card">
            <div className="case-summary-value">{formatCount(value)}</div>
            <div className="case-summary-label">{label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function CaseTimeline({ events }) {
  if (!events.length) {
    return <div className="case-empty-inline">No custody events recorded yet.</div>
  }

  return (
    <div className="case-timeline">
      {events.map((event, index) => {
        const timestamp = event.occurred_at ?? event.timestamp ?? event.created_at ?? event.at
        const type = event.event_type ?? event.type ?? event.action ?? 'custody event'
        const details = event.details && typeof event.details === 'object' ? event.details : {}
        const detail = event.message
          ?? event.description
          ?? event.detail
          ?? details.source_path
          ?? details.path
          ?? event.path
          ?? event.file_path
        const hash = details.sha256 || details.expected_sha256 || details.current_sha256
        const actor = event.actor ? ` · ${event.actor}` : ''
        return (
          <div key={event.id ?? event.event_id ?? `${timestamp}-${index}`} className="case-timeline-event">
            <div className="case-timeline-dot" />
            <div className="case-timeline-copy">
              <div className="case-timeline-topline">
                <span className="case-timeline-type">{String(type).replace(/_/g, ' ')}{actor}</span>
                <span className="case-timeline-date">{formatDate(timestamp)}</span>
              </div>
              {detail ? <div className="case-timeline-detail">{String(detail)}</div> : null}
              {hash ? <div className="case-timeline-hash">sha-256 {hash}</div> : null}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ForensicTimelinePanel({ payload, loading, error, onRefresh }) {
  const entries = payload?.entries || []
  const bounds = payload?.bounds || {}
  const stats = [
    ['evidence', payload?.evidence_count ?? 0],
    ['custody events', payload?.custody_event_count ?? 0],
    ['timeline entries', payload?.entry_count ?? entries.length],
  ]

  return (
    <div className="surface-card case-panel">
      <div className="case-card-heading-row">
        <div>
          <div className="case-section-label">forensic timeline</div>
          <p className="case-card-caption">Evidence registrations and case-scoped custody events in one ordered view.</p>
        </div>
        <button type="button" className="case-secondary-button" disabled={loading} onClick={() => void onRefresh()}>
          {loading ? 'refreshing…' : 'refresh timeline'}
        </button>
      </div>

      {error ? <div className="case-form-error">{error}</div> : null}
      {loading ? <div className="case-loading">loading forensic timeline…</div> : null}

      {!loading && !error ? (
        <>
          <div className="case-summary-grid case-forensic-stats">
            {stats.map(([label, value]) => (
              <div key={label} className="case-summary-card">
                <div className="case-summary-value">{formatCount(value)}</div>
                <div className="case-summary-label">{label}</div>
              </div>
            ))}
          </div>
          {payload?.truncated ? <div className="case-forensic-truncated">Timeline is truncated to the requested limit.</div> : null}
          {bounds.start_at || bounds.end_at ? (
            <div className="case-forensic-bounds">
              bounds {bounds.start_at ? formatDate(bounds.start_at) : 'beginning'} → {bounds.end_at ? formatDate(bounds.end_at) : 'now'}
            </div>
          ) : null}
          {!entries.length ? <div className="case-empty-inline">No forensic timeline entries returned for this case.</div> : null}
          {entries.length ? (
            <div className="case-forensic-timeline">
              {entries.map((entry, index) => {
                const timestamp = entry.timestamp
                const type = entry.event_type || entry.timeline_type || 'case event'
                const title = entry.item?.title || entry.item?.filename || entry.evidence?.source_name || 'Case-level event'
                const detail = entry.custody?.details?.status
                  || entry.custody?.details?.message
                  || entry.evidence?.source_path
                  || entry.item?.original_path
                const hash = entry.custody?.event_hash || entry.event_hash
                return (
                  <div key={entry.id ?? `${type}-${timestamp}-${index}`} className="case-forensic-event">
                    <div className="case-forensic-event-topline">
                      <span className="case-forensic-event-type">{String(type).replace(/_/g, ' ')}</span>
                      <span className="case-forensic-event-date">{formatDate(timestamp)}</span>
                    </div>
                    <div className="case-forensic-event-title">{redactSensitiveText(title)}</div>
                    {detail ? <div className="case-forensic-event-detail">{redactSensitiveText(detail)}</div> : null}
                    <div className="case-forensic-event-meta">
                      {entry.actor ? `${redactSensitiveText(entry.actor)} · ` : ''}{entry.item_id ? `item ${redactSensitiveText(entry.item_id)}` : 'case-level'}
                      {hash ? ` · sha-256 ${hashSnippet(hash)}` : ''}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function ForensicReportCard({ report }) {
  const reportBody = report?.report || {}
  const generation = report?.generation || {}
  const findings = Array.isArray(reportBody.findings) ? reportBody.findings : []
  const limitations = Array.isArray(reportBody.limitations) ? reportBody.limitations : []
  const warnings = Array.isArray(report?.warnings) ? report.warnings : []
  const sources = Array.isArray(report?.sources) ? report.sources : []
  const provider = report?.provider || generation.provider || 'unknown'
  const fallback = report?.fallback ?? generation.fallback_used

  return (
    <div className="case-forensic-report">
      <div className="case-forensic-report-heading">
        <span className="case-forensic-report-label">investigative aid</span>
        <span className="case-forensic-report-disclaimer">not a determination of legal admissibility</span>
      </div>

      <div className="case-forensic-summary">
        <div className="case-forensic-subheading">summary</div>
        <p>{redactSensitiveText(reportBody.summary || 'No summary was returned.')}</p>
        <div className="case-forensic-citations">{sourceCitationText(reportBody.summary_source_indices)}</div>
      </div>

      <div className="case-forensic-report-grid">
        <div>
          <div className="case-forensic-subheading">findings</div>
          {!findings.length ? <div className="case-empty-inline">No findings were returned for this question.</div> : null}
          {findings.length ? (
            <div className="case-forensic-list">
              {findings.map((finding, index) => (
                <div key={`${finding.statement}-${index}`} className="case-forensic-list-item">
                  <p>{redactSensitiveText(finding.statement)}</p>
                  <div className="case-forensic-citations">{sourceCitationText(finding.source_indices)}</div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
        <div>
          <div className="case-forensic-subheading">limitations</div>
          {!limitations.length ? <div className="case-empty-inline">No limitations were returned.</div> : null}
          {limitations.length ? (
            <ul className="case-forensic-bullet-list">
              {limitations.map((limitation, index) => <li key={`${limitation}-${index}`}>{redactSensitiveText(limitation)}</li>)}
            </ul>
          ) : null}
        </div>
      </div>

      {warnings.length ? (
        <div className="case-forensic-warning-block">
          <div className="case-forensic-subheading">source warnings</div>
          <ul className="case-forensic-bullet-list">
            {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{redactSensitiveText(warning)}</li>)}
          </ul>
        </div>
      ) : null}

      <div className="case-forensic-metadata">
        <div><span>provider</span><strong>{redactSensitiveText(provider)}</strong></div>
        <div><span>fallback</span><strong>{fallback ? 'yes' : 'no'}</strong></div>
        <div><span>model requested</span><strong>{generation.model_requested ? 'yes' : 'no'}</strong></div>
        <div><span>model used</span><strong>{generation.model_used ? 'yes' : 'no'}</strong></div>
        <div><span>sources</span><strong>{formatCount(report?.source_count ?? sources.length)}</strong></div>
      </div>
      {report?.report_sha256 ? (
        <div className="case-forensic-hash"><span>report sha-256</span><code>{redactSensitiveText(report.report_sha256)}</code></div>
      ) : null}

      <div className="case-forensic-subheading case-forensic-sources-heading">source citations</div>
      {!sources.length ? <div className="case-empty-inline">No source records were returned.</div> : null}
      {sources.length ? (
        <div className="case-forensic-sources">
          {sources.map((source) => {
            const verification = source.verification || {}
            const indexed = source.indexed || {}
            return (
              <div key={source.source_index} className="case-forensic-source">
                <div className="case-forensic-source-heading">
                  <span className="case-forensic-source-index">[{source.source_index}]</span>
                  <strong>{redactSensitiveText(source.title || source.filename || source.source_name || source.item_id || 'Unnamed source')}</strong>
                </div>
                <div className="case-forensic-source-meta">
                  {redactSensitiveText(source.filename || source.source_name || 'unnamed')} · acquisition {redactSensitiveText(source.acquisition_status || 'unknown')} · verification {redactSensitiveText(verification.status || 'unknown')}
                </div>
                {source.source_path ? <div className="case-forensic-source-path">{redactSensitiveText(source.source_path)}</div> : null}
                {source.content_excerpt ? <div className="case-forensic-source-excerpt">{redactSensitiveText(source.content_excerpt)}</div> : <div className="case-forensic-source-excerpt muted">No indexed content excerpt returned.</div>}
                <div className="case-forensic-source-meta">
                  {source.sha256 ? `sha-256 ${source.sha256}` : 'sha-256 unavailable'} · indexed {indexed.indexed_status || 'recorded'} · custody events {formatCount(source.custody_context?.length || 0)}
                </div>
              </div>
            )
          })}
        </div>
      ) : null}
      <p className="case-forensic-disclaimer-copy">{redactSensitiveText(report?.disclaimer || 'This analysis is an investigative aid based on local records. It does not determine legal admissibility or make a legal conclusion.')}</p>
    </div>
  )
}

export default function CasesPage({ authHeaders, onShowToast }) {
  const [cases, setCases] = useState([])
  const [selectedCaseId, setSelectedCaseId] = useState(() => (
    typeof window === 'undefined' ? '' : window.localStorage.getItem(SELECTED_CASE_STORAGE_KEY) || ''
  ))
  const [selectedCase, setSelectedCase] = useState(null)
  const [evidence, setEvidence] = useState([])
  const [timeline, setTimeline] = useState([])
  const [forensicTimeline, setForensicTimeline] = useState(null)
  const [loadingForensicTimeline, setLoadingForensicTimeline] = useState(false)
  const [forensicTimelineError, setForensicTimelineError] = useState('')
  const [loadingCases, setLoadingCases] = useState(true)
  const [loadingWorkspace, setLoadingWorkspace] = useState(false)
  const [casesError, setCasesError] = useState('')
  const [workspaceError, setWorkspaceError] = useState('')
  const [workspaceRefresh, setWorkspaceRefresh] = useState(0)
  const [nameInput, setNameInput] = useState('')
  const [descriptionInput, setDescriptionInput] = useState('')
  const [notesInput, setNotesInput] = useState('')
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)
  const [pathInput, setPathInput] = useState('')
  const [importError, setImportError] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const [verifying, setVerifying] = useState(false)
  const [verificationReport, setVerificationReport] = useState(null)
  const [verificationError, setVerificationError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [analysisQuestion, setAnalysisQuestion] = useState('')
  const [allowModel, setAllowModel] = useState(false)
  const [analysisReport, setAnalysisReport] = useState(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [analysisError, setAnalysisError] = useState('')

  const selectedFromList = useMemo(
    () => cases.find((item) => String(caseId(item)) === String(selectedCaseId)) || null,
    [cases, selectedCaseId],
  )
  const workspaceCase = selectedCase || selectedFromList
  const workspaceCaseName = caseName(workspaceCase)

  const persistCaseSelection = useCallback((nextCase, nextId = caseId(nextCase)) => {
    const value = nextId === null || nextId === undefined ? '' : String(nextId)
    setSelectedCaseId(value)
    setSelectedCase(nextCase || null)
    setEvidence([])
    setTimeline([])
    setForensicTimeline(null)
    setForensicTimelineError('')
    setImportResult(null)
    setVerificationReport(null)
    setWorkspaceError('')
    setImportError('')
    setVerificationError('')
    setAnalysisReport(null)
    setAnalysisError('')
    setAnalysisQuestion('')
    if (typeof window !== 'undefined') {
      if (value) window.localStorage.setItem(SELECTED_CASE_STORAGE_KEY, value)
      else window.localStorage.removeItem(SELECTED_CASE_STORAGE_KEY)
      window.dispatchEvent(new CustomEvent('custody-case-selected', {
        detail: { id: value, name: caseName(nextCase) },
      }))
    }
  }, [])

  const refreshCases = useCallback(async () => {
    setLoadingCases(true)
    setCasesError('')
    try {
      const payload = await getCases(authHeaders)
      const nextCases = unwrapCases(payload)
      setCases(nextCases)
      setSelectedCaseId((current) => {
        if (current && nextCases.some((item) => String(caseId(item)) === String(current))) return current
        return nextCases[0] ? String(caseId(nextCases[0])) : ''
      })
      return nextCases
    } catch (error) {
      setCasesError(error?.message || 'Unable to load cases')
      return []
    } finally {
      setLoadingCases(false)
    }
  }, [authHeaders])

  useEffect(() => {
    void refreshCases()
  }, [refreshCases])

  // A deleted or otherwise stale local selection should never survive a case
  // refresh and get reused by the search overlay.
  useEffect(() => {
    if (loadingCases) return

    const nextCase = cases.find((item) => String(caseId(item)) === String(selectedCaseId)) || null
    const nextId = nextCase ? String(caseId(nextCase)) : ''
    const currentId = String(selectedCaseId || '')

    if (currentId !== nextId) {
      persistCaseSelection(nextCase, nextId)
      return
    }

    if (typeof window !== 'undefined') {
      if (nextId) window.localStorage.setItem(SELECTED_CASE_STORAGE_KEY, nextId)
      else window.localStorage.removeItem(SELECTED_CASE_STORAGE_KEY)
    }
  }, [cases, loadingCases, persistCaseSelection, selectedCaseId])

  useEffect(() => {
    if (!selectedCaseId) {
      setSelectedCase(null)
      setEvidence([])
      setTimeline([])
      setForensicTimeline(null)
      setForensicTimelineError('')
      setWorkspaceError('')
      return undefined
    }

    let cancelled = false
    const loadWorkspace = async () => {
      setLoadingWorkspace(true)
      setWorkspaceError('')
      setVerificationError('')
      try {
        const [casePayload, evidencePayload, timelinePayload] = await Promise.all([
          getCase(selectedCaseId, authHeaders),
          getCaseEvidence(selectedCaseId, authHeaders),
          getCaseTimeline(selectedCaseId, authHeaders),
        ])
        if (cancelled) return
        setSelectedCase(unwrapCase(casePayload))
        setEvidence(unwrapEvidence(evidencePayload))
        setTimeline(unwrapTimeline(timelinePayload))
        if (typeof window !== 'undefined') {
          window.localStorage.setItem(SELECTED_CASE_STORAGE_KEY, String(selectedCaseId))
        }
      } catch (error) {
        if (!cancelled) setWorkspaceError(error?.message || 'Unable to load this case')
      } finally {
        if (!cancelled) setLoadingWorkspace(false)
      }
    }
    void loadWorkspace()
    return () => { cancelled = true }
  }, [authHeaders, selectedCaseId, workspaceRefresh])

  const refreshForensicTimeline = useCallback(async (nextCaseId = selectedCaseId) => {
    if (!nextCaseId) return
    setLoadingForensicTimeline(true)
    setForensicTimelineError('')
    try {
      const payload = await getForensicTimeline(nextCaseId, { limit: 1000 }, authHeaders)
      setForensicTimeline(normalizeForensicTimeline(payload))
    } catch (error) {
      setForensicTimelineError(error?.message || 'Unable to load forensic timeline')
    } finally {
      setLoadingForensicTimeline(false)
    }
  }, [authHeaders, selectedCaseId])

  useEffect(() => {
    if (!selectedCaseId) return undefined
    void refreshForensicTimeline(selectedCaseId)
    return undefined
  }, [refreshForensicTimeline, selectedCaseId, workspaceRefresh])

  const handleCreate = async (event) => {
    event.preventDefault()
    const name = nameInput.trim()
    if (!name) {
      setCreateError('Case name is required.')
      return
    }
    setCreating(true)
    setCreateError('')
    try {
      const payload = await createCase({
        name,
        description: descriptionInput.trim(),
        notes: notesInput.trim(),
      }, authHeaders)
      const created = unwrapCase(payload)
      const refreshed = await refreshCases()
      const createdId = caseId(created) ?? caseId(refreshed.find((item) => caseName(item) === name))
      if (createdId === null || createdId === undefined) {
        throw new Error('Case was created but its identifier was missing.')
      }
      setNameInput('')
      setDescriptionInput('')
      setNotesInput('')
      persistCaseSelection(created, createdId)
      onShowToast?.(`created ${name}`)
    } catch (error) {
      setCreateError(error?.message || 'Unable to create case')
    } finally {
      setCreating(false)
    }
  }

  const handlePickFolder = async () => {
    if (!isTauri()) {
      setImportError('Enter the full evidence path. Folder picking is available in the desktop app.')
      return
    }
    try {
      const picked = await openNativeDialog({
        directory: true,
        multiple: false,
        title: 'Choose evidence folder',
      })
      if (typeof picked === 'string' && picked) {
        setPathInput((current) => current.trim() ? `${current}\n${picked}` : picked)
        setImportError('')
      }
    } catch (error) {
      setImportError(error?.message || 'Unable to open the folder picker')
    }
  }

  const handleImport = async (event) => {
    event.preventDefault()
    if (!selectedCaseId) {
      setImportError('Select a case before importing evidence.')
      return
    }
    const paths = [...new Set(pathInput.split(/\r?\n/).map((path) => path.trim()).filter(Boolean))]
    if (!paths.length) {
      setImportError('Enter at least one evidence path.')
      return
    }
    setImporting(true)
    setImportError('')
    setImportResult(null)
    try {
      const payload = await importCaseEvidence(selectedCaseId, paths, authHeaders)
      setImportResult(payload?.summary ?? payload)
      if (payload?.case) setSelectedCase(unwrapCase(payload))
      setPathInput('')
      setVerificationReport(null)
      setWorkspaceRefresh((value) => value + 1)
      window.dispatchEvent(new CustomEvent('custody-evidence-changed', {
        detail: { caseId: selectedCaseId, type: 'import' },
      }))
      const counts = summaryCounts(payload)
      onShowToast?.(
        counts.failed || counts.skipped
          ? `evidence import finished · ${counts.failed} failed · ${counts.skipped} skipped`
          : 'evidence import complete',
      )
    } catch (error) {
      setImportError(error?.message || 'Unable to import evidence')
    } finally {
      setImporting(false)
    }
  }

  const handleVerify = async () => {
    if (!selectedCaseId) return
    setVerifying(true)
    setVerificationError('')
    try {
      const payload = await verifyCaseEvidence(selectedCaseId, authHeaders)
      setVerificationReport(payload)
      const [evidencePayload, timelinePayload, casePayload] = await Promise.all([
        getCaseEvidence(selectedCaseId, authHeaders),
        getCaseTimeline(selectedCaseId, authHeaders),
        getCase(selectedCaseId, authHeaders),
      ])
      setEvidence(unwrapEvidence(evidencePayload))
      setTimeline(unwrapTimeline(timelinePayload))
      setSelectedCase(unwrapCase(casePayload))
      window.dispatchEvent(new CustomEvent('custody-evidence-changed', {
        detail: { caseId: selectedCaseId, type: 'verify' },
      }))
      onShowToast?.('evidence verification complete')
    } catch (error) {
      setVerificationError(error?.message || 'Unable to verify evidence')
    } finally {
      setVerifying(false)
    }
  }

  const handleAnalyze = async (event) => {
    event.preventDefault()
    const question = analysisQuestion.trim()
    if (!selectedCaseId) return
    if (!question) {
      setAnalysisError('Enter a question or prompt before analyzing the case.')
      return
    }

    setAnalysisLoading(true)
    setAnalysisError('')
    try {
      const payload = await generateCaseReport(selectedCaseId, {
        question,
        allowModel,
        limit: 10,
      }, authHeaders)
      setAnalysisReport(payload)
      onShowToast?.('case analysis complete')
    } catch (error) {
      setAnalysisError(error?.message || 'Unable to generate case report')
    } finally {
      setAnalysisLoading(false)
    }
  }

  const handleExport = async () => {
    if (!selectedCaseId) return
    try {
      const payload = await exportCaseManifest(selectedCaseId, authHeaders)
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${workspaceCaseName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'case'}-manifest.json`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
      onShowToast?.('manifest downloaded')
    } catch (error) {
      setWorkspaceError(error?.message || 'Unable to export manifest')
    }
  }

  const handleDelete = async () => {
    if (!selectedCaseId || !window.confirm(`Delete ${workspaceCaseName}? This removes the case record.`)) return
    setDeleting(true)
    try {
      await deleteCase(selectedCaseId, authHeaders)
      persistCaseSelection(null)
      setSelectedCase(null)
      await refreshCases()
      onShowToast?.('case deleted')
    } catch (error) {
      setWorkspaceError(error?.message || 'Unable to delete case')
    } finally {
      setDeleting(false)
    }
  }

  const importCounts = summaryCounts(importResult)
  const verificationCounts = summaryCounts(verificationReport)
  const status = String(workspaceCase?.status ?? workspaceCase?.state ?? 'active')
  const analysisState = forensicReportState({
    loading: analysisLoading,
    error: analysisError,
    report: analysisReport,
  })

  return (
    <div className="cases-page" style={{ animation: 'fadeUp 0.45s ease both' }}>
      <div className="page-header">
        <div className="label page-subtitle">forensic custody</div>
        <div className="cases-page-title">cases</div>
        <p className="cases-page-intro">Create an auditable case, acquire evidence by path, and verify every file against its recorded hash.</p>
      </div>

      {casesError ? <div className="case-error-banner">{casesError}</div> : null}

      <div className="cases-layout">
        <aside className="cases-sidebar">
          <div className="case-section-label">case register</div>
          {loadingCases ? <div className="case-loading">loading cases…</div> : null}
          {!loadingCases && !cases.length ? <div className="case-empty-sidebar">No cases yet. Create the first case below.</div> : null}
          <div className="case-list">
            {cases.map((item) => {
              const id = caseId(item)
              const active = String(id) === String(selectedCaseId)
              return (
                <button
                  type="button"
                  key={String(id)}
                  className={`case-list-item${active ? ' active' : ''}`}
                  onClick={() => persistCaseSelection(item, id)}
                >
                  <span className="case-list-name">{caseName(item)}</span>
                  <span className="case-list-meta">{String(item.status ?? item.state ?? 'active')}</span>
                </button>
              )
            })}
          </div>

          <form className="case-create-form" onSubmit={(event) => void handleCreate(event)}>
            <div className="case-section-label">new case</div>
            <label className="case-field-label" htmlFor="case-name">name <span>*</span></label>
            <input
              id="case-name"
              className="case-input"
              value={nameInput}
              onChange={(event) => { setNameInput(event.target.value); setCreateError('') }}
              placeholder="e.g. Incident 2026-04"
              maxLength={200}
            />
            <label className="case-field-label" htmlFor="case-description">description</label>
            <textarea
              id="case-description"
              className="case-input case-textarea"
              value={descriptionInput}
              onChange={(event) => setDescriptionInput(event.target.value)}
              placeholder="What is this investigation about?"
              rows={2}
            />
            <label className="case-field-label" htmlFor="case-notes">notes</label>
            <textarea
              id="case-notes"
              className="case-input case-textarea"
              value={notesInput}
              onChange={(event) => setNotesInput(event.target.value)}
              placeholder="Working notes"
              rows={3}
            />
            {createError ? <div className="case-form-error">{createError}</div> : null}
            <button type="submit" className="case-primary-button" disabled={creating}>
              {creating ? 'creating…' : 'create case'}
            </button>
          </form>
        </aside>

        <section className="case-workspace">
          {!workspaceCase && !loadingCases ? (
            <div className="case-empty-state surface-card">
              <div className="case-empty-mark">⌁</div>
              <h2>No case selected</h2>
              <p>Create a case to start an evidence chain with paths, SHA-256 hashes, and custody events.</p>
            </div>
          ) : null}

          {workspaceCase ? (
            <>
              <div className="surface-card case-header-card">
                <div className="case-header-main">
                  <div className="label">selected case</div>
                  <h1>{workspaceCaseName}</h1>
                  {workspaceCase.description ? <p>{workspaceCase.description}</p> : null}
                  {workspaceCase.notes ? <div className="case-notes">{workspaceCase.notes}</div> : null}
                </div>
                <div className="case-header-actions">
                  <span className={`case-status case-status-${status.toLowerCase()}`}>{status}</span>
                  <button type="button" className="case-secondary-button" onClick={() => void handleExport()}>export manifest</button>
                  <button type="button" className="case-danger-button" disabled={deleting} onClick={() => void handleDelete()}>{deleting ? 'deleting…' : 'delete case'}</button>
                </div>
                <div className="case-metadata">
                  <div><span>case id</span><strong>{String(caseId(workspaceCase) ?? selectedCaseId)}</strong></div>
                  <div><span>created</span><strong>{formatDate(workspaceCase.created_at ?? workspaceCase.createdAt)}</strong></div>
                  <div><span>evidence files</span><strong>{formatCount(evidence.length)}</strong></div>
                </div>
              </div>

              <div className="surface-card case-panel case-analysis-card">
                <div className="case-section-label">case analysis</div>
                <p className="case-card-caption">Ask a natural-language question about this case. The default deterministic review uses recorded evidence and indexed metadata.</p>
                <form className="case-analysis-form" onSubmit={(event) => void handleAnalyze(event)}>
                  <label className="case-field-label" htmlFor="case-analysis-question">question or prompt</label>
                  <textarea
                    id="case-analysis-question"
                    className="case-input case-analysis-input"
                    value={analysisQuestion}
                    onChange={(event) => { setAnalysisQuestion(event.target.value); setAnalysisError('') }}
                    placeholder="e.g. What evidence was recorded and what integrity warnings should I review?"
                    maxLength={2000}
                    rows={3}
                  />
                  <div className="case-analysis-controls">
                    <label className="case-model-toggle" htmlFor="case-analysis-allow-model">
                      <input
                        id="case-analysis-allow-model"
                        type="checkbox"
                        checked={allowModel}
                        onChange={(event) => setAllowModel(event.target.checked)}
                      />
                      <span>allow model-assisted analysis</span>
                    </label>
                    <button type="submit" className="case-primary-button" disabled={analysisLoading || !analysisQuestion.trim()}>
                      {analysisLoading ? 'analyzing…' : 'analyze case'}
                    </button>
                  </div>
                </form>
                <div className={`case-model-warning${allowModel ? ' is-active' : ''}`} role="note">
                  {allowModel
                    ? 'Warning: enabling model-assisted analysis may send evidence excerpts to Gemini.'
                    : 'Model assistance is off. If enabled, evidence excerpts may be sent to Gemini.'}
                </div>
                {analysisState === 'loading' ? <div className="case-progress"><span className="case-progress-spinner" /> building a cited case report…</div> : null}
                {analysisState === 'error' ? <div className="case-form-error">{analysisError}</div> : null}
                {analysisState === 'empty' ? <div className="case-empty-inline">No analysis yet. Enter a question to review this case.</div> : null}
                {analysisState === 'ready' ? <ForensicReportCard report={analysisReport} /> : null}
              </div>

              <div className="surface-card case-import-card">
                <div className="case-card-heading-row">
                  <div>
                    <div className="case-section-label">acquire evidence</div>
                    <p className="case-card-caption">Paths are recorded and hashed with SHA-256 by the custody service.</p>
                  </div>
                  <button type="button" className="case-secondary-button" onClick={() => void handlePickFolder()}>choose folder</button>
                </div>
                <form className="case-import-form" onSubmit={(event) => void handleImport(event)}>
                  <textarea
                    className="case-input case-path-input"
                    value={pathInput}
                    onChange={(event) => { setPathInput(event.target.value); setImportError('') }}
                    placeholder="/Volumes/drive/evidence\n/Users/you/file.dd"
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    rows={3}
                  />
                  <button type="submit" className="case-primary-button" disabled={importing}>{importing ? 'hashing…' : 'import evidence'}</button>
                </form>
                <div className="case-path-hint">Add multiple paths on separate lines. The native folder picker is available in the desktop app.</div>
                {importing ? <div className="case-progress"><span className="case-progress-spinner" /> hashing and recording evidence…</div> : null}
                {importError ? <div className="case-form-error">{importError}</div> : null}
                {importResult ? <SummaryCards title="last import" counts={importCounts} /> : null}
              </div>

              <div className="surface-card case-panel">
                <div className="case-card-heading-row">
                  <div>
                    <div className="case-section-label">evidence manifest</div>
                    <p className="case-card-caption">{formatCount(evidence.length)} acquired file{evidence.length === 1 ? '' : 's'} in this case.</p>
                  </div>
                  <button type="button" className="case-primary-button" disabled={verifying || loadingWorkspace} onClick={() => void handleVerify()}>{verifying ? 'verifying…' : 'verify evidence'}</button>
                </div>
                {loadingWorkspace ? <div className="case-loading">refreshing custody records…</div> : <EvidenceTable rows={evidence} />}
                {verificationError ? <div className="case-form-error">{verificationError}</div> : null}
                {verificationReport ? <SummaryCards title="verification report" counts={verificationCounts} includeVerification /> : null}
              </div>

              <ForensicTimelinePanel
                payload={forensicTimeline}
                loading={loadingForensicTimeline}
                error={forensicTimelineError}
                onRefresh={refreshForensicTimeline}
              />

              <div className="case-lower-grid">
                <div className="surface-card case-panel">
                  <div className="case-section-label">custody timeline</div>
                  <p className="case-card-caption">Acquisition and verification events for this case.</p>
                  {workspaceError ? <div className="case-form-error">{workspaceError}</div> : null}
                  <CaseTimeline events={timeline} />
                </div>
                <div className="surface-card case-panel case-readiness-panel">
                  <div className="case-section-label">workflow status</div>
                  <div className="case-readiness-item"><span className="case-readiness-dot" /> case record ready</div>
                  <div className={`case-readiness-item${evidence.length ? '' : ' muted'}`}><span className="case-readiness-dot" /> evidence {evidence.length ? 'acquired' : 'awaiting import'}</div>
                  <div className={`case-readiness-item${verificationReport ? '' : ' muted'}`}><span className="case-readiness-dot" /> evidence {verificationReport ? 'verified' : 'not verified yet'}</div>
                  <p>Export the JSON manifest when you need to preserve the current chain of custody outside Custody.</p>
                </div>
              </div>
            </>
          ) : null}
        </section>
      </div>
    </div>
  )
}
