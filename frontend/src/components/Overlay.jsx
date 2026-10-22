import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { isTauri } from '@tauri-apps/api/core'
import { askQuestion, connectWebSocket, getCases, getHealth, getStatus, openItem, revealItem, searchItems } from '../api/client'
import { ensureBackendResponsive } from '../lib/backend'
import BrandLogo from './BrandLogo'
import { memoryDateLabel } from '../utils/time'
import { invokeIfTauri } from '../utils/tauri'
import { expandAliases } from '../utils/aliases'
import { storeAndApplyAccent } from '../utils/accent'
import { getBackendHttpUrl } from '../lib/backend'
import {
  getQueryCacheVersion,
  invalidateQueryCache,
  queryCacheGet,
  queryCacheSet,
} from '../utils/searchCache'
import { caseId, caseLabel, resolveSearchCase, searchScopeLabel } from '../utils/searchScope'
import { getSearchIndexNotice } from '../utils/searchStatus'

const COMPACT_OVERLAY_HEIGHT = 92
const OVERLAY_COUNT_POLL_MS = 15000
const OVERLAY_SEARCH_GUARD_MS = 26000
const TYPEWRITER_CPS = 120
const SELECTED_CASE_STORAGE_KEY = 'custody_selected_case_id'
const INDEXING_EVENT_TYPES = new Set([
  'indexing',
  'scan_phase',
  'scan_progress',
  'file_indexed',
  'file_duplicate',
  'item_enriched',
  'scan_complete',
])

// ─── File type config ────────────────────────────────────────────────────────
const FILE_TYPE = {
  pdf: {
    color: '#d58a7a',
    dim: 'rgba(213,138,122,0.12)',
    border: 'rgba(213,138,122,0.18)',
    openBorder: 'rgba(213,138,122,0.32)',
    openBg: 'rgba(213,138,122,0.1)',
  },
  image: {
    color: '#c4b06a',
    dim: 'rgba(196,176,106,0.12)',
    border: 'rgba(196,176,106,0.2)',
    openBorder: 'rgba(196,176,106,0.32)',
    openBg: 'rgba(196,176,106,0.1)',
  },
  spreadsheet: {
    color: '#7ab890',
    dim: 'rgba(122,184,144,0.12)',
    border: 'rgba(122,184,144,0.18)',
    openBorder: 'rgba(122,184,144,0.32)',
    openBg: 'rgba(122,184,144,0.1)',
  },
  presentation: {
    color: '#b98cb9',
    dim: 'rgba(185,140,185,0.12)',
    border: 'rgba(185,140,185,0.18)',
    openBorder: 'rgba(185,140,185,0.32)',
    openBg: 'rgba(185,140,185,0.1)',
  },
  docx: {
    color: '#8ea8e8',
    dim: 'rgba(142,168,232,0.12)',
    border: 'rgba(142,168,232,0.18)',
    openBorder: 'rgba(142,168,232,0.32)',
    openBg: 'rgba(142,168,232,0.1)',
  },
  text: {
    color: '#c4a050',
    dim: 'rgba(196,160,80,0.12)',
    border: 'rgba(196,160,80,0.2)',
    openBorder: 'rgba(196,160,80,0.32)',
    openBg: 'rgba(196,160,80,0.1)',
  },
  email: {
    color: '#7ec4d8',
    dim: 'rgba(126,196,216,0.12)',
    border: 'rgba(126,196,216,0.18)',
    openBorder: 'rgba(126,196,216,0.32)',
    openBg: 'rgba(126,196,216,0.1)',
  },
  default: {
    color: '#a09888',
    dim: 'rgba(160,152,136,0.1)',
    border: 'rgba(160,152,136,0.16)',
    openBorder: 'rgba(160,152,136,0.28)',
    openBg: 'rgba(255,255,255,0.03)',
  },
}

function typeConfig(fileType) {
  return FILE_TYPE[fileType] || FILE_TYPE.default
}

function normalizeCaseList(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.cases)) return payload.cases
  return []
}

function caseOptionLabel(value) {
  const count = Number(value?.evidence_count)
  if (Number.isFinite(count)) {
    return `${caseLabel(value)} · ${count.toLocaleString()} evidence`
  }
  return caseLabel(value)
}

function isSearchableCase(value) {
  if (!Object.prototype.hasOwnProperty.call(value || {}, 'evidence_count')) return true
  const count = Number(value?.evidence_count)
  return !Number.isFinite(count) || count > 0
}

// ─── SVG file type icons ─────────────────────────────────────────────────────
function FileTypeIcon({ fileType }) {
  const cfg = typeConfig(fileType)
  const base = {
    viewBox: '0 0 20 20',
    fill: 'none',
    width: 16,
    height: 16,
    stroke: 'currentColor',
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
  }

  let icon
  switch (fileType) {
    case 'pdf':
      icon = (
        <svg {...base}>
          <path d="M4 2.5A1.5 1.5 0 015.5 1h6L15 5.5V17a1.5 1.5 0 01-1.5 1.5h-8A1.5 1.5 0 014 17V2.5z" strokeWidth="1.35" />
          <path d="M11 1v5h4" strokeWidth="1.35" />
          <path d="M7 11h6M7 14h4" strokeWidth="1.2" />
        </svg>
      )
      break
    case 'image':
      icon = (
        <svg {...base}>
          <rect x="2" y="3.5" width="16" height="13" rx="2" strokeWidth="1.35" />
          <circle cx="7.5" cy="8.5" r="1.5" strokeWidth="1.2" />
          <path d="M2 14l4.5-4 3 3 2.5-2.5 6 5" strokeWidth="1.2" />
        </svg>
      )
      break
    case 'spreadsheet':
      icon = (
        <svg {...base}>
          <rect x="2" y="2" width="16" height="16" rx="2" strokeWidth="1.35" />
          <path d="M2 7.5h16M2 12.5h16M9 2v16" strokeWidth="1.1" />
        </svg>
      )
      break
    case 'presentation':
      icon = (
        <svg {...base}>
          <rect x="1.5" y="3" width="17" height="12" rx="2" strokeWidth="1.35" />
          <path d="M10 15v2.5M7 17.5h6" strokeWidth="1.35" />
          <path d="M7.5 10l2.5 2.5 4-5" strokeWidth="1.4" />
        </svg>
      )
      break
    case 'email':
      icon = (
        <svg {...base}>
          <rect x="2" y="4.5" width="16" height="12" rx="2" strokeWidth="1.35" />
          <path d="M2 8l8 5 8-5" strokeWidth="1.25" />
        </svg>
      )
      break
    case 'text':
      icon = (
        <svg {...base}>
          <path d="M4 4.5h12M4 8.5h12M4 12.5h12M4 16.5h7" strokeWidth="1.5" />
        </svg>
      )
      break
    case 'docx':
      icon = (
        <svg {...base}>
          <path d="M4 2.5A1.5 1.5 0 015.5 1h6L15 5.5V17a1.5 1.5 0 01-1.5 1.5h-8A1.5 1.5 0 014 17V2.5z" strokeWidth="1.35" />
          <path d="M11 1v5h4" strokeWidth="1.35" />
          <path d="M7 10h6M7 13h6M7 7h3" strokeWidth="1.2" />
        </svg>
      )
      break
    default:
      icon = (
        <svg {...base}>
          <path d="M4 2.5A1.5 1.5 0 015.5 1h6L15 5.5V17a1.5 1.5 0 01-1.5 1.5h-8A1.5 1.5 0 014 17V2.5z" strokeWidth="1.35" />
          <path d="M11 1v5h4" strokeWidth="1.35" />
        </svg>
      )
  }

  return (
    <div
      className="file-icon-wrap"
      style={{
        color: cfg.color,
        background: cfg.dim,
        border: `1px solid ${cfg.border}`,
      }}
    >
      {icon}
    </div>
  )
}

// ─── Search & spinner glyphs ─────────────────────────────────────────────────
function SearchGlyph() {
  return (
    <svg
      className="overlay-icon"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
    >
      <circle cx="9" cy="9" r="6.2" />
      <path d="M14 14l3.5 3.5" />
    </svg>
  )
}

function SpinnerGlyph() {
  return (
    <svg
      className="overlay-icon is-spinning"
      viewBox="0 0 20 20"
      fill="none"
      strokeLinecap="round"
      strokeWidth="2"
    >
      <path d="M10 3a7 7 0 017 7" stroke="currentColor" opacity="0.9" />
      <path d="M10 3a7 7 0 00-7 7" stroke="currentColor" opacity="0.2" />
      <path d="M3 10a7 7 0 007 7" stroke="currentColor" opacity="0.1" />
    </svg>
  )
}

// ─── Thinking dots ───────────────────────────────────────────────────────────
function ThinkingDots() {
  return (
    <span className="thinking-dots" aria-hidden="true">
      <span className="td" style={{ animationDelay: '0ms' }}>·</span>
      <span className="td" style={{ animationDelay: '180ms' }}>·</span>
      <span className="td" style={{ animationDelay: '360ms' }}>·</span>
    </span>
  )
}

// ─── Confidence pill (tiny, lowercase, top-right) ────────────────────────────
function ConfidencePill({ confidence }) {
  const map = {
    high: { bg: 'rgba(120,184,144,0.12)', color: 'rgba(120,184,144,0.75)', border: 'rgba(120,184,144,0.2)' },
    medium: { bg: 'rgba(196,160,80,0.10)', color: 'rgba(196,160,80,0.7)', border: 'rgba(196,160,80,0.2)' },
    low: { bg: 'rgba(213,138,122,0.10)', color: 'rgba(213,138,122,0.65)', border: 'rgba(213,138,122,0.18)' },
    insufficient: { bg: 'rgba(255,200,80,0.06)', color: 'rgba(255,200,80,0.5)', border: 'rgba(255,200,80,0.14)' },
  }
  const p = map[confidence] || map.low
  return (
    <span
      className="confidence-pill confidence-pill-small"
      style={{ background: p.bg, color: p.color, border: `1px solid ${p.border}` }}
    >
      {confidence}
    </span>
  )
}

// ─── Confidence border style for AskCard ─────────────────────────────────────
function confidenceBorderStyle(confidence) {
  const borders = {
    high: '2px solid rgba(120,184,144,0.5)',
    medium: '2px solid rgba(196,160,80,0.4)',
    low: '2px solid rgba(213,138,122,0.3)',
    insufficient: '2px solid rgba(255,200,80,0.3)',
  }
  return borders[confidence] || borders.low
}

// ─── Answer text renderer (handles line breaks + bullet differentiation) ─────
function AskAnswerText({ text, isTyping }) {
  if (!text) return null
  const lines = text.split('\n')
  return (
    <>
      {lines.map((line, i) => {
        const isLast = i === lines.length - 1
        if (line.trim() === '') return <span key={i} className="ask-line-blank" />
        const bulletMatch = line.match(/^(\s*)(•\s*)(.*)$/)
        if (bulletMatch) {
          return (
            <span key={i} className="ask-line-bullet">
              <span className="ask-bullet-char">{bulletMatch[2]}</span>
              <span className="ask-bullet-content">{bulletMatch[3]}</span>
              {isLast && isTyping && <span className="typewriter-cursor" aria-hidden="true" />}
            </span>
          )
        }
        return (
          <span key={i} className="ask-line">
            {line}
            {isLast && isTyping && <span className="typewriter-cursor" aria-hidden="true" />}
          </span>
        )
      })}
    </>
  )
}

// ─── Ask answer card ─────────────────────────────────────────────────────────
function AskCard({ isAsking, askAnswer, displayedAnswer, isTyping, onOpenSource, onDismiss, hasResults, copyToast, onCopy, onShowResults }) {
  const [previewSource, setPreviewSource] = useState(null)

  const handlePillClick = (src) => {
    if (!src.item_id) return
    setPreviewSource((prev) => (prev?.item_id === src.item_id ? null : src))
  }

  const borderStyle = askAnswer?.confidence
    ? { borderLeft: confidenceBorderStyle(askAnswer.confidence) }
    : {}
  return (
    <div className="ask-card" style={borderStyle}>
      <div className="ask-card-header">
        <span className="ask-label">Answer</span>
        <div className="ask-card-header-right">
          {copyToast && <span className="copy-toast">Copied!</span>}
          {askAnswer && (
            <button
              type="button"
              className="copy-btn"
              onClick={onCopy}
              title="Copy answer (⌘C)"
            >
              ⎘
            </button>
          )}
          {askAnswer?.provider && (
            <span className="ask-provider">{askAnswer.provider}</span>
          )}
          {askAnswer && <ConfidencePill confidence={askAnswer.confidence} />}
          {hasResults && onDismiss && (
            <button
              type="button"
              className="ask-back-btn"
              onClick={onDismiss}
              title="Back to results"
            >
              results →
            </button>
          )}
        </div>
      </div>

      <div className="ask-body">
        {isAsking && !askAnswer ? (
          <span className="ask-thinking">
            Thinking through your memory<ThinkingDots />
          </span>
        ) : (
          <AskAnswerText
            text={displayedAnswer || 'No answer produced.'}
            isTyping={isTyping}
          />
        )}
      </div>

      {hasResults && onShowResults && (
        <button
          type="button"
          className="ask-see-results-link"
          onClick={onShowResults}
        >
          See search results →
        </button>
      )}

      {askAnswer?.sources?.length > 0 && (
        <div className="ask-sources">
          {askAnswer.sources.map((src) => (
            <button
              key={src.item_id ?? src.index}
              type="button"
              className={`source-pill${previewSource?.item_id === src.item_id ? ' source-pill-active' : ''}`}
              onClick={() => handlePillClick(src)}
              title={`${src.filename || src.title} · click to preview`}
            >
              <span className="source-index">[{src.index}]</span>
              <span className="source-title">{src.title || src.filename || 'source'}</span>
              <span className="source-kbd">⌘{src.index}</span>
            </button>
          ))}

          {previewSource && (
            <div className="source-preview-panel">
              <div className="source-preview-header">
                <div className="source-preview-meta">
                  <strong className="source-preview-filename" title={previewSource.filename || previewSource.title}>
                    {previewSource.filename || previewSource.title || 'source'}
                  </strong>
                  {previewSource.file_type && (
                    <span className="source-preview-type-badge">{previewSource.file_type}</span>
                  )}
                  {(previewSource.date || previewSource.created_at) && (
                    <span className="source-preview-date">
                      {memoryDateLabel(previewSource)}
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  className="source-preview-close"
                  onClick={() => setPreviewSource(null)}
                  title="Close preview"
                >
                  ✕
                </button>
              </div>
              <div className="source-preview-body">
                {(previewSource.extracted_text || previewSource.preview_text)
                  ? (previewSource.extracted_text || previewSource.preview_text).slice(0, 600)
                  : <em>No preview available.</em>
                }
              </div>
              <div className="source-preview-footer">
                <button
                  type="button"
                  className="source-preview-open-link"
                  onClick={() => onOpenSource(previewSource.item_id)}
                >
                  Open file
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Result row ──────────────────────────────────────────────────────────────
function ResultRow({ result, index, selected, onSelect, onOpen, onReveal, caseNames = {} }) {
  const cfg = typeConfig(result.file_type)
  const metaBits = [
    result.filename,
    memoryDateLabel(result),
    result.sender_name,
  ].filter(Boolean)
  const snippet = result.snippet || result.text_preview || null
  const rawCaseIds = Array.isArray(result.case_ids)
    ? result.case_ids
    : result.case_id !== undefined && result.case_id !== null
      ? [result.case_id]
      : []
  const resultCaseIds = rawCaseIds.map((value) => (typeof value === 'object' ? caseId(value) : value)).filter(Boolean)
  const evidenceHash = result.evidence_sha256 || result.sha256 || result.evidence_hash || ''
  const acquisitionStatus = result.acquisition_status || result.evidence_status || ''

  return (
    <div
      className={`result-row${selected ? ' sel' : ''}`}
      style={{ animationDelay: `${Math.min(index * 38, 220)}ms` }}
      onMouseEnter={() => onSelect(index)}
      onClick={() => onSelect(index)}
      onDoubleClick={() => onOpen(result.id)}
    >
      <FileTypeIcon fileType={result.file_type} />

      <div className="result-body">
        <div className="result-title">{result.title || result.filename}</div>
        {snippet ? (
          <div className="result-snippet">{snippet}</div>
        ) : metaBits.length > 0 ? (
          <div className="result-meta">
            {metaBits.map((bit, i) => (
              <span key={`${bit}-${i}`}>
                {i > 0 && <span className="meta-sep">·</span>}
                {bit}
              </span>
            ))}
          </div>
        ) : null}
        {resultCaseIds.length || evidenceHash || acquisitionStatus ? (
          <div className="result-provenance" aria-label="Evidence provenance">
            {resultCaseIds.map((id) => (
              <span key={String(id)} className="result-provenance-badge">
                {caseNames[String(id)] || `case ${String(id).slice(0, 12)}`}
              </span>
            ))}
            {evidenceHash ? (
              <span className="result-provenance-hash" title={evidenceHash}>
                sha {String(evidenceHash).slice(0, 12)}…
              </span>
            ) : null}
            {acquisitionStatus ? <span className="result-provenance-status">{acquisitionStatus}</span> : null}
          </div>
        ) : null}
      </div>

      <div className="result-actions">
        <button
          type="button"
          className="btn-reveal"
          title="Reveal in Finder (⇥)"
          onClick={(e) => {
            e.stopPropagation()
            onReveal(result.id)
          }}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" width="11" height="11">
            <path d="M2 8h12M9 3l5 5-5 5" />
          </svg>
        </button>
        <button
          type="button"
          className="btn-open"
          title="Open (↵)"
          style={{
            borderColor: cfg.openBorder,
            background: cfg.openBg,
            color: cfg.color,
          }}
          onClick={(e) => {
            e.stopPropagation()
            onOpen(result.id)
          }}
        >
          open
        </button>
      </div>
    </div>
  )
}

// ─── Debug panel ─────────────────────────────────────────────────────────────
function DebugPanel({ debugPayload, selectedDebugRow }) {
  return (
    <div className="overlay-debug-panel">
      <div className="overlay-debug-head">
        <span className="overlay-debug-label">debug</span>
        <span className="overlay-debug-pill">
          {String(debugPayload?.pipeline || 'unknown')} · complexity {Number(debugPayload?.complexity_score || 0).toFixed(2)}
        </span>
      </div>
      <div className="overlay-debug-grid">
        <div className="overlay-debug-item">
          <div className="overlay-debug-key">intent</div>
          <div className="overlay-debug-value">{debugPayload?.intent_class || 'unknown'}</div>
        </div>
        <div className="overlay-debug-item">
          <div className="overlay-debug-key">graph</div>
          <div className="overlay-debug-value">{debugPayload?.use_graph ? 'enabled' : 'disabled'}</div>
        </div>
        <div className="overlay-debug-item">
          <div className="overlay-debug-key">candidates</div>
          <div className="overlay-debug-value">
            d:{debugPayload?.candidate_pool_sizes?.dense ?? 0} ·
            f:{debugPayload?.candidate_pool_sizes?.fts ?? 0} ·
            g:{debugPayload?.candidate_pool_sizes?.graph ?? 0}
          </div>
        </div>
        <div className="overlay-debug-item">
          <div className="overlay-debug-key">latency</div>
          <div className="overlay-debug-value">{debugPayload?.latency_ms?.total ?? 0}ms</div>
        </div>
      </div>
      {selectedDebugRow && (
        <div className="overlay-debug-result">
          <div className="overlay-debug-result-title">{selectedDebugRow.title}</div>
          <div className="overlay-debug-result-line">
            v:{selectedDebugRow?.scoring?.vector ?? 0} ·
            fts:{selectedDebugRow?.scoring?.fts ?? 0} ·
            g:{selectedDebugRow?.scoring?.graph ?? 0} ·
            t:{selectedDebugRow?.scoring?.time ?? 0} ·
            b:{selectedDebugRow?.scoring?.behavior ?? 0} ·
            final:{selectedDebugRow?.scoring?.final ?? 0}
          </div>
          {Array.isArray(selectedDebugRow?.graph_path) && selectedDebugRow.graph_path.length > 0 && (
            <div className="overlay-debug-result-path">
              {selectedDebugRow.graph_path.join(' → ')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Main overlay component ──────────────────────────────────────────────────
function Overlay() {
  const inputRef = useRef(null)
  const rootRef = useRef(null)
  const wrapRef = useRef(null)
  const selectedResultRef = useRef(null)
  const requestRef = useRef(0)
  const currentWindow = useMemo(
    () => (isTauri() ? getCurrentWindow() : null),
    [],
  )

  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [isSearching, setIsSearching] = useState(false)
  const [totalItems, setTotalItems] = useState(0)
  const [authHeaders, setAuthHeaders] = useState({})
  const [notice, setNotice] = useState('')
  const [debugMode, setDebugMode] = useState(false)
  const [debugPayload, setDebugPayload] = useState(null)
  const [askAnswer, setAskAnswer] = useState(null)
  const [isAsking, setIsAsking] = useState(false)
  const [copyToast, setCopyToast] = useState(false)
  const [hintsExpanded, setHintsExpanded] = useState(false)
  const [conversationHistory, setConversationHistory] = useState([])
  const askRequestRef = useRef(0)
  const queryCountRef = useRef(parseInt(localStorage.getItem('recurdo_query_count') || '0', 10))
  const typewriterRef = useRef(null)

  const [displayedAnswer, setDisplayedAnswer] = useState('')
  const [isTyping, setIsTyping] = useState(false)
  const [userAliases, setUserAliases] = useState({})
  const [userName, setUserName] = useState('')
  const [caseOptions, setCaseOptions] = useState([])
  const [selectedCaseId, setSelectedCaseId] = useState(() => (
    typeof window === 'undefined' ? '' : window.localStorage.getItem(SELECTED_CASE_STORAGE_KEY) || ''
  ))
  const [indexStatus, setIndexStatus] = useState(null)

  const visibleResults = results.slice(0, 8)
  const resolvedCase = useMemo(
    () => resolveSearchCase(selectedCaseId, caseOptions),
    [caseOptions, selectedCaseId],
  )
  const activeCaseId = resolvedCase.id
  const selectedCase = resolvedCase.value
  const scopeLabel = searchScopeLabel(selectedCase)
  const indexNotice = getSearchIndexNotice(indexStatus)
  const indexNoticeKind = indexNotice?.kind || ''
  const caseNames = useMemo(
    () => Object.fromEntries(caseOptions.map((item) => [String(caseId(item)), caseLabel(item)])),
    [caseOptions],
  )
  const showResultsArea =
    query.trim().length > 0 ||
    visibleResults.length > 0 ||
    isSearching ||
    Boolean(askAnswer) ||
    isAsking

  const syncOverlaySize = useCallback(() => {
    const node = rootRef.current
    if (!node) return
    const measuredHeight = Math.ceil(node.scrollHeight + 2)
    const height = Math.max(COMPACT_OVERLAY_HEIGHT, measuredHeight)
    void invokeIfTauri('resize_overlay', { height })
  }, [])

  // Load auth session
  useEffect(() => {
    let cancelled = false
    if (!isTauri()) return undefined

    invoke('load_session')
      .then((session) => {
        if (cancelled) return
        const token = session?.token
        if (!token) { setAuthHeaders({}); return }
        setAuthHeaders({
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        })
      })
      .catch(() => { if (!cancelled) setAuthHeaders({}) })

    return () => { cancelled = true }
  }, [])

  // Load custody cases so global search can stay scoped to the active case.
  useEffect(() => {
    let cancelled = false
    const loadCases = async () => {
      try {
        const payload = await getCases(authHeaders)
        const nextCases = normalizeCaseList(payload)
        if (cancelled) return
        setCaseOptions(nextCases)
        setSelectedCaseId((current) => {
           if (current && nextCases.some((item) => (
             String(caseId(item)) === String(current) && isSearchableCase(item)
           ))) return current
           const stored = typeof window === 'undefined' ? '' : window.localStorage.getItem(SELECTED_CASE_STORAGE_KEY) || ''
           const nextId = stored && nextCases.some((item) => (
             String(caseId(item)) === String(stored) && isSearchableCase(item)
           )) ? stored : ''
          if (!nextId && typeof window !== 'undefined') {
            window.localStorage.removeItem(SELECTED_CASE_STORAGE_KEY)
          }
          return nextId
        })
      } catch {
        // Cases are optional for the global search surface.
      }
    }
    void loadCases()
    return () => { cancelled = true }
  }, [authHeaders])

  useEffect(() => {
    const syncSelectedCase = (event) => {
      const nextId = event?.detail?.id ?? event?.newValue ?? ''
      setSelectedCaseId(String(nextId || ''))
      setAskAnswer(null)
      setIsAsking(false)
      setConversationHistory([])
      setResults([])
      setDebugPayload(null)
    }
    window.addEventListener('custody-case-selected', syncSelectedCase)
    window.addEventListener('storage', syncSelectedCase)
    return () => {
      window.removeEventListener('custody-case-selected', syncSelectedCase)
      window.removeEventListener('storage', syncSelectedCase)
    }
  }, [])

  useEffect(() => {
    const invalidateEvidenceCache = () => {
      invalidateQueryCache()
    }
    window.addEventListener('custody-evidence-changed', invalidateEvidenceCache)
    return () => window.removeEventListener('custody-evidence-changed', invalidateEvidenceCache)
  }, [])

  // Load user aliases and profile on mount
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const base = await getBackendHttpUrl()
        const res = await fetch(`${base}/user/settings`)
        if (!res.ok) return
        const data = await res.json()
        if (!cancelled && data.aliases && typeof data.aliases === 'object') {
          setUserAliases(data.aliases)
        }
        if (!cancelled && data.accent_color) {
          storeAndApplyAccent(data.accent_color)
        }
      } catch {
        // Aliases are best-effort — don't break the overlay
      }
      try {
        const base = await getBackendHttpUrl()
        const res = await fetch(`${base}/user/profile`)
        if (!res.ok) return
        const data = await res.json()
        if (!cancelled) {
          setUserName(data.display_name || data.name || '')
        }
      } catch {
        // Profile is best-effort — don't break the overlay
      }
    }
    void load()
    return () => { cancelled = true }
  }, [])

  const focusInput = useCallback(() => {
    window.setTimeout(() => {
      const input = inputRef.current
      if (!input) return
      input.focus()
      const end = input.value?.length ?? 0
      if (typeof input.setSelectionRange === 'function') {
        input.setSelectionRange(end, end)
      }
    }, 30)
  }, [])

  const refreshStatus = useCallback(async () => {
    const [healthResult, statusResult] = await Promise.allSettled([
      getHealth(),
      getStatus(authHeaders),
    ])

    if (healthResult.status === 'fulfilled') {
      const count = healthResult.value?.unique_memories ?? healthResult.value?.indexed_items
      if (Number.isFinite(count)) setTotalItems(count)
    }

    if (statusResult.status === 'fulfilled') {
      const status = statusResult.value || {}
      setIndexStatus(status)
      const count = status.unique_memories ?? status.total_items
      if (Number.isFinite(Number(count))) setTotalItems(Number(count))
    }

    if (healthResult.status === 'rejected' && statusResult.status === 'rejected') {
      console.warn('Overlay status refresh failed', healthResult.reason || statusResult.reason)
    }
  }, [authHeaders])

  const wakeBackend = useCallback(async () => {
    try { await ensureBackendResponsive({ forceRefresh: true }) }
    catch (error) { console.warn('Overlay backend wake failed', error) }
  }, [])

  // The overlay is a separate Tauri window, so it listens to the backend's
  // indexing stream directly instead of relying on MainApp's event handler.
  // Keep visible results stable while invalidating the cache; clearing them
  // here would make an active query look broken during a normal scan.
  useEffect(() => {
    const handleIndexingEvent = (event) => {
      if (!INDEXING_EVENT_TYPES.has(event?.type)) return
      invalidateQueryCache()

      if (event.type === 'scan_progress') {
        setIndexStatus((current) => ({
          ...(current || {}),
          is_scanning: true,
          scan_progress: {
            ...(current?.scan_progress || {}),
            processed: event.processed ?? event.indexed ?? current?.scan_progress?.processed ?? 0,
            indexed: event.indexed ?? current?.scan_progress?.indexed ?? 0,
            total: event.total ?? current?.scan_progress?.total ?? 0,
            remaining: event.remaining ?? current?.scan_progress?.remaining ?? 0,
            percent: event.percent ?? current?.scan_progress?.percent ?? 0,
          },
        }))
      }

      if (event.type === 'scan_phase') {
        setIndexStatus((current) => ({
          ...(current || {}),
          is_scanning: true,
          scan_phase: {
            ...(current?.scan_phase || {}),
            phase: event.phase || current?.scan_phase?.phase || '',
            message: event.message || current?.scan_phase?.message || '',
            found: event.found ?? current?.scan_phase?.found ?? 0,
            checked: event.checked ?? current?.scan_phase?.checked ?? 0,
            total: event.total ?? current?.scan_phase?.total ?? 0,
            new_found: event.new_found ?? current?.scan_phase?.new_found ?? 0,
          },
        }))
      }

      if (event.type === 'scan_complete') {
        void refreshStatus()
      }
    }

    const websocket = connectWebSocket(handleIndexingEvent, () => {})
    return () => websocket.destroy()
  }, [refreshStatus])

  const hideOverlay = useCallback(async () => {
    setQuery('')
    setResults([])
    setSelectedIndex(0)
    setNotice('')
    setAskAnswer(null)
    setIsAsking(false)
    setConversationHistory([])  // clear thread on close
    await invokeIfTauri('hide_overlay')
  }, [])

  const handleCopyAnswer = useCallback(() => {
    if (!askAnswer?.text) return
    void navigator.clipboard.writeText(askAnswer.text).then(() => {
      setCopyToast(true)
      window.setTimeout(() => setCopyToast(false), 1800)
    })
  }, [askAnswer])

  const handleAsk = useCallback(async () => {
    const trimmed = query.trim()
    if (!trimmed) return
    const requestId = askRequestRef.current + 1
    askRequestRef.current = requestId
    setIsAsking(true)
    setNotice('')
    try {
      // Send prior turns so Gemini has follow-up context (last 3 turns max)
      const history = conversationHistory.slice(-3)
      const expandedQ = expandAliases(trimmed, userAliases)
      const response = await askQuestion(
        expandedQ,
        {
          limit: 10,
          caseId: activeCaseId || undefined,
          conversationHistory: history.length ? history : null,
        },
        authHeaders,
      )
      if (askRequestRef.current !== requestId) return
      const newAnswer = {
        question: response?.question || trimmed,
        text: response?.answer?.text || '',
        provider: response?.answer?.provider || 'heuristic',
        confidence: response?.answer?.confidence || 'low',
        sources: Array.isArray(response?.sources) ? response.sources : [],
      }
      setAskAnswer(newAnswer)
      // Append to conversation history for follow-up questions
      setConversationHistory(prev => [
        ...prev,
        { question: trimmed, answer: newAnswer.text }
      ])
    } catch (error) {
      if (askRequestRef.current !== requestId) return
      console.warn('Overlay ask failed', error)
      setAskAnswer(null)
      setNotice(error?.message || 'Unable to answer right now')
    } finally {
      if (askRequestRef.current === requestId) setIsAsking(false)
    }
  }, [activeCaseId, authHeaders, query, conversationHistory, userAliases])

  const handleOpen = useCallback(async (itemId) => {
    try {
      await openItem(itemId, authHeaders)
      await hideOverlay()
    } catch (error) {
      console.warn('Open failed', error)
      setNotice(error?.message || 'Unable to open file')
    }
  }, [authHeaders, hideOverlay])

  const handleReveal = useCallback(async (itemId) => {
    try {
      await revealItem(itemId, authHeaders)
      await hideOverlay()
    } catch (error) {
      console.warn('Reveal failed', error)
      setNotice(error?.message || 'Unable to reveal file')
    }
  }, [authHeaders, hideOverlay])

  // Typewriter effect — triggers whenever askAnswer changes
  useEffect(() => {
    // Clear any running interval from a previous answer
    if (typewriterRef.current !== null) {
      window.clearInterval(typewriterRef.current)
      typewriterRef.current = null
    }

    if (!askAnswer) {
      setDisplayedAnswer('')
      setIsTyping(false)
      return undefined
    }

    const fullText = askAnswer.text || ''
    if (!fullText) {
      setDisplayedAnswer('')
      setIsTyping(false)
      return undefined
    }

    // Start typewriter
    setDisplayedAnswer('')
    setIsTyping(true)

    let charIndex = 0
    const intervalMs = Math.round(1000 / TYPEWRITER_CPS)

    typewriterRef.current = window.setInterval(() => {
      charIndex += 1
      setDisplayedAnswer(fullText.slice(0, charIndex))
      if (charIndex >= fullText.length) {
        window.clearInterval(typewriterRef.current)
        typewriterRef.current = null
        setIsTyping(false)
      }
    }, intervalMs)

    return () => {
      if (typewriterRef.current !== null) {
        window.clearInterval(typewriterRef.current)
        typewriterRef.current = null
      }
    }
  }, [askAnswer])

  // Focus, polling, visibility
  useEffect(() => {
    refreshStatus()
    focusInput()
    syncOverlaySize()

    const pollMs = indexNoticeKind ? Math.min(OVERLAY_COUNT_POLL_MS, 5000) : OVERLAY_COUNT_POLL_MS
    const intervalId = window.setInterval(refreshStatus, pollMs)

    let unlistenPromise
    if (currentWindow) {
      unlistenPromise = currentWindow.onFocusChanged(({ payload }) => {
        if (payload) {
          void (async () => {
            await wakeBackend()
            focusInput()
            void refreshStatus()
            syncOverlaySize()
            window.requestAnimationFrame(syncOverlaySize)
          })()
        }
      })
    }

    const handleWindowFocus = () => {
      void (async () => {
        await wakeBackend()
        focusInput()
        void refreshStatus()
        syncOverlaySize()
        window.requestAnimationFrame(syncOverlaySize)
      })()
    }
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return
      void (async () => {
        await wakeBackend()
        void refreshStatus()
        syncOverlaySize()
      })()
    }

    window.addEventListener('focus', handleWindowFocus)
    document.addEventListener('visibilitychange', handleVisibility)

    return () => {
      window.clearInterval(intervalId)
      window.removeEventListener('focus', handleWindowFocus)
      document.removeEventListener('visibilitychange', handleVisibility)
      if (unlistenPromise) {
        unlistenPromise.then((unlisten) => unlisten())
      }
    }
  }, [currentWindow, focusInput, indexNoticeKind, refreshStatus, syncOverlaySize, wakeBackend])

  // Search effect
  useEffect(() => {
    const trimmed = query.trim()
    const requestId = requestRef.current + 1
    requestRef.current = requestId

    if (!trimmed) {
      setResults([])
      setSelectedIndex(0)
      setIsSearching(false)
      setDebugPayload(null)
      setAskAnswer(null)
      return undefined
    }

    setAskAnswer(null)

    // Serve from LRU cache instantly (no spinner) then skip network.
    // Capture the version so an in-flight response cannot repopulate the
    // cache after an indexing event invalidates it.
    const cacheKey = `${trimmed}|${activeCaseId || 'all'}|${debugMode ? '1' : '0'}`
    const cacheVersion = getQueryCacheVersion()
    const cached = queryCacheGet(cacheKey, cacheVersion)
    if (cached) {
      setResults(cached.results)
      setSelectedIndex(0)
      setDebugPayload(debugMode ? cached.debug : null)
      setIsSearching(false)
      return undefined
    }

    let cancelled = false
    let settled = false

    const timeoutId = window.setTimeout(async () => {
      setIsSearching(true)
      setNotice('')

      const guardId = window.setTimeout(() => {
        if (cancelled || settled || requestRef.current !== requestId) return
        cancelled = true
        setIsSearching(false)
        setResults([])
        setSelectedIndex(0)
        setNotice('Search is taking too long. Please try again.')
      }, OVERLAY_SEARCH_GUARD_MS)

      try {
        const expandedQuery = expandAliases(trimmed, userAliases)
        const responsePayload = await searchItems(expandedQuery, 8, authHeaders, {
          explain: debugMode,
          caseId: activeCaseId || undefined,
        })
        const nextResults = Array.isArray(responsePayload)
          ? responsePayload
          : (Array.isArray(responsePayload?.results) ? responsePayload.results : [])
        const nextDebug = Array.isArray(responsePayload) ? null : (responsePayload?.debug || null)

        if (cancelled || requestRef.current !== requestId) return

        queryCacheSet(cacheKey, { results: nextResults, debug: nextDebug }, cacheVersion)
        queryCountRef.current += 1
        localStorage.setItem('recurdo_query_count', String(queryCountRef.current))

        setResults(Array.isArray(nextResults) ? nextResults : [])
        setSelectedIndex(0)
        setDebugPayload(debugMode ? nextDebug : null)
      } catch (error) {
        console.warn('Overlay search failed', error)
        if (!cancelled && requestRef.current === requestId) {
          setResults([])
          setSelectedIndex(0)
          setDebugPayload(null)
          setNotice(error?.message || 'Unable to search right now')
        }
      } finally {
        settled = true
        window.clearTimeout(guardId)
        if (!cancelled && requestRef.current === requestId) setIsSearching(false)
      }
    }, 120)

    return () => {
      cancelled = true
      window.clearTimeout(timeoutId)
    }
  }, [activeCaseId, authHeaders, debugMode, query, userAliases])

  // Debug row
  const selectedDebugRow = useMemo(() => {
    const ranked = Array.isArray(debugPayload?.ranked_results) ? debugPayload.ranked_results : []
    const selected = visibleResults[selectedIndex]
    if (!selected) return ranked[0] || null
    return ranked.find((row) => row?.id === selected.id) || ranked[selectedIndex] || null
  }, [debugPayload, selectedIndex, visibleResults])

  // Resize observer
  useLayoutEffect(() => {
    syncOverlaySize()
    if (typeof ResizeObserver === 'undefined') return undefined
    const node = wrapRef.current
    if (!node) return undefined
    const observer = new ResizeObserver(syncOverlaySize)
    observer.observe(node)
    return () => observer.disconnect()
  }, [isSearching, query, showResultsArea, syncOverlaySize, visibleResults.length])

  // Scroll selected into view
  useEffect(() => {
    selectedResultRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [selectedIndex, visibleResults])

  // Keyboard handler
  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        // If typewriter is running, snap to full text instead of closing
        if (typewriterRef.current !== null) {
          window.clearInterval(typewriterRef.current)
          typewriterRef.current = null
          setIsTyping(false)
          if (askAnswer?.text) setDisplayedAnswer(askAnswer.text)
          return
        }
        void hideOverlay()
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === ',') {
        event.preventDefault()
        void invokeIfTauri('open_settings')
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        setDebugMode((prev) => !prev)
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault()
        if (!isAsking) void handleAsk()
        return
      }
      // ⌘C when in answer mode — copy answer text (only if no text is selected)
      if ((event.metaKey || event.ctrlKey) && event.key === 'c' && askAnswer?.text) {
        const sel = window.getSelection()
        if (!sel || sel.toString() === '') {
          event.preventDefault()
          void navigator.clipboard.writeText(askAnswer.text).then(() => {
            setCopyToast(true)
            window.setTimeout(() => setCopyToast(false), 1800)
          })
          return
        }
      }
      // ⌘1..⌘6 — open the Nth source from the ask answer
      if ((event.metaKey || event.ctrlKey) && askAnswer?.sources?.length > 0) {
        const n = parseInt(event.key, 10)
        if (n >= 1 && n <= 6) {
          event.preventDefault()
          const src = askAnswer.sources[n - 1]
          if (src?.item_id) void handleOpen(src.item_id)
          return
        }
      }
      if (!visibleResults.length) return
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setSelectedIndex((prev) => Math.min(prev + 1, visibleResults.length - 1))
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setSelectedIndex((prev) => Math.max(prev - 1, 0))
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        const item = visibleResults[selectedIndex]
        if (item) void handleOpen(item.id)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        const item = visibleResults[selectedIndex]
        if (item) void handleReveal(item.id)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [askAnswer, handleAsk, handleOpen, handleReveal, hideOverlay, isAsking, selectedIndex, visibleResults, setCopyToast])

  return (
    <div ref={rootRef} className="overlay-root">
      <div ref={wrapRef} className="overlay-wrap">

        {/* ── Search bar ── */}
        <div className="overlay-search">
          <div className="overlay-icon-slot">
            {isSearching ? <SpinnerGlyph /> : <SearchGlyph />}
          </div>

           <input
            ref={inputRef}
            className="overlay-input-field"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`search ${totalItems > 0 ? totalItems.toLocaleString() : '…'} memories`}
            autoCapitalize="none"
            autoCorrect="off"
             spellCheck={false}
           />

          <select
            className="overlay-case-select"
            aria-label="Search evidence scope"
             value={activeCaseId}
            onChange={(event) => {
              const nextId = event.target.value
              setSelectedCaseId(nextId)
              setAskAnswer(null)
              setIsAsking(false)
              setConversationHistory([])
              setResults([])
              setDebugPayload(null)
              if (nextId) window.localStorage.setItem(SELECTED_CASE_STORAGE_KEY, nextId)
              else window.localStorage.removeItem(SELECTED_CASE_STORAGE_KEY)
            }}
          >
            <option value="">all evidence</option>
             {caseOptions.map((item) => (
               <option
                 key={String(caseId(item))}
                 value={String(caseId(item))}
                 disabled={!isSearchableCase(item)}
               >
                 {caseOptionLabel(item)}{!isSearchableCase(item) ? ' · import evidence first' : ''}
               </option>
             ))}
          </select>

          {query.trim() ? (
            <div className="overlay-search-end">
              {!askAnswer && !isAsking && (
                <span className="ask-hint-label">⌘↵ ask</span>
              )}
              <span className="overlay-count">{visibleResults.length}</span>
              <span className="kbd-esc">esc</span>
            </div>
           ) : null}
         </div>

        {(query.trim() || selectedCase) ? (
          <div className={`overlay-scope-status${selectedCase ? ' is-case' : ''}`} aria-live="polite">
            scope · {scopeLabel}
          </div>
        ) : null}

        {indexNotice ? (
          <div className={`overlay-index-status is-${indexNotice.kind}`} role="status" aria-live="polite">
            <span className="overlay-index-status-dot" aria-hidden="true" />
            <span>{indexNotice.message}</span>
          </div>
        ) : null}

        {/* ── Home greeting (idle state only) ── */}
        {!showResultsArea && userName && (
          <div className="overlay-greeting">
            Hey, {userName.split(' ')[0]} 👋
          </div>
        )}

        {/* ── Results area ── */}
        {showResultsArea ? (
          <>
            <div className="overlay-sep" />

            {/* Ask card — takes over when answer is available */}
            {askAnswer && askAnswer.provider === 'heuristic' && (
              <div className="ask-fallback-notice">
                AI synthesis unavailable — showing your top search results instead
              </div>
            )}
            {(isAsking || askAnswer) ? (
              <AskCard
                isAsking={isAsking}
                askAnswer={askAnswer}
                displayedAnswer={displayedAnswer}
                isTyping={isTyping}
                onOpenSource={handleOpen}
                onDismiss={() => setAskAnswer(null)}
                hasResults={visibleResults.length > 0}
                copyToast={copyToast}
                onCopy={handleCopyAnswer}
                onShowResults={() => setAskAnswer(null)}
              />
            ) : null}

            {/* Result list — hidden while answer is shown */}
            {!askAnswer && !isAsking ? (
              <div className="overlay-results">
                {visibleResults.length > 0 ? (
                  visibleResults.map((result, index) => (
                    <div
                      key={result.id ?? `${result.filename}-${index}`}
                      ref={selectedIndex === index ? selectedResultRef : null}
                    >
                      <ResultRow
                        result={result}
                        index={index}
                        selected={selectedIndex === index}
                        onSelect={setSelectedIndex}
                        onOpen={handleOpen}
                        onReveal={handleReveal}
                        caseNames={caseNames}
                      />
                    </div>
                  ))
                 ) : !isSearching && query.trim().length > 0 ? (
                   <div className="overlay-empty-state">
                     <span className="overlay-empty-state-icon">◌</span>
                     <div className="overlay-empty-state-text">
                       {selectedCase
                         ? <>Nothing found in {caseLabel(selectedCase)} for &ldquo;{query.trim()}&rdquo;</>
                         : <>Nothing found for &ldquo;{query.trim()}&rdquo;</>}
                     </div>
                     <div className="overlay-empty-state-sub">
                       {selectedCase
                         ? 'This case may not have matching indexed evidence yet. Try all evidence or different words.'
                         : 'Try different words, or ⌘↵ to ask Recurdo directly'}
                     </div>
                   </div>
                ) : null}
              </div>
            ) : null}

            {/* Footer */}
            <div className="overlay-footer">
              <div className="footer-hints">
                {askAnswer ? (
                  // Answer mode: always show contextual hints
                  <>
                    <span className="footer-hint">
                      <span className="footer-key">⌘C</span> copy
                    </span>
                    <span className="footer-hint">
                      <span className="footer-key">esc</span> reset
                    </span>
                  </>
                ) : queryCountRef.current >= 5 && !hintsExpanded ? (
                  // Collapsed after 5 uses
                  <button
                    type="button"
                    className="footer-hint footer-hint-btn footer-hints-toggle"
                    onClick={() => setHintsExpanded(true)}
                    title="Show keyboard shortcuts"
                  >
                    ?
                  </button>
                ) : (
                  // Full hints
                  <>
                    <span className="footer-hint">
                      <span className="footer-key">↑↓</span> navigate
                    </span>
                    <span className="footer-hint">
                      <span className="footer-key">↵</span> open
                    </span>
                    <span className="footer-hint">
                      <span className="footer-key">⇥</span> reveal
                    </span>
                    <span className="footer-hint">
                      <span className="footer-key">⌘↵</span> ask
                    </span>
                    <button
                      type="button"
                      className="footer-hint footer-hint-btn"
                      onClick={() => void invokeIfTauri('open_settings')}
                    >
                      <span className="footer-key">⌘,</span> settings
                    </button>
                    {debugMode && (
                      <span className="footer-hint footer-hint-debug">
                        <span className="footer-key">⌘⇧D</span> debug on
                      </span>
                    )}
                    {queryCountRef.current >= 5 && (
                      <button
                        type="button"
                        className="footer-hint footer-hint-btn"
                        onClick={() => setHintsExpanded(false)}
                      >
                        ×
                      </button>
                    )}
                  </>
                )}
              </div>
              <BrandLogo className="brand-logo overlay-brand-logo" alt="Custody" />
            </div>

            {/* Debug panel */}
            {debugMode ? (
              <DebugPanel
                debugPayload={debugPayload}
                selectedDebugRow={selectedDebugRow}
              />
            ) : null}
          </>
        ) : null}

        {/* Notice */}
        {notice ? (
          <div className={`overlay-notice${isSearching ? ' overlay-notice-starting' : ''}`}>
            {isSearching
              ? 'Starting up, one moment\u2026'
              : notice}
          </div>
        ) : null}
      </div>
    </div>
  )
}

export default Overlay
