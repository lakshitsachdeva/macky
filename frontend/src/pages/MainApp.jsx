import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { open as openNativeDialog } from '@tauri-apps/plugin-dialog'
import {
  addWatchedFolder,
  cleanupGmail,
  configureLlm,
  connectGmail,
  connectWebSocket,
  disconnectGmail,
  discoverFolders,
  getContext,
  getHealth,
  getItems,
  getStatus,
  getWatchedFolders,
  openFolderPicker,
  removeWatchedFolder,
  stopGmailSync,
  triggerCognitiveBootstrap,
  triggerGmailSync,
  triggerScan,
} from '../api/client'
import BrandLogo from '../components/BrandLogo'
import NowPanel from '../components/NowPanel'
import CasesPage from './CasesPage'
import { getBackendHttpUrl } from '../lib/backend'
import Onboarding from './Onboarding'
import { ACCENT_OPTIONS, loadStoredAccentId, storeAndApplyAccent } from '../utils/accent'
import { invokeIfTauri } from '../utils/tauri'
import { memoryDateLabel, relativeTime } from '../utils/time'
import { checkForRecurdoUpdate, installRecurdoUpdate } from '../utils/updater'
import { isTauri } from '@tauri-apps/api/core'

const FORCE_ONBOARDING = import.meta.env.VITE_FORCE_ONBOARDING === 'true'

const PAGES = [
  { id: 'overview', label: 'Overview' },
  { id: 'files', label: 'Files' },
  { id: 'cases', label: 'Cases' },
  { id: 'settings', label: 'Settings' },
]

const FILE_TYPE = {
  pdf: { label: 'PDF', color: '#d58a7a', dim: 'rgba(213,138,122,0.14)', border: 'rgba(213,138,122,0.2)' },
  image: { label: 'IMG', color: '#b8a15a', dim: 'rgba(184,161,90,0.14)', border: 'rgba(184,161,90,0.22)' },
  spreadsheet: { label: 'XLS', color: '#78a485', dim: 'rgba(120,164,133,0.14)', border: 'rgba(120,164,133,0.2)' },
  presentation: { label: 'PPT', color: '#b98cb9', dim: 'rgba(185,140,185,0.14)', border: 'rgba(185,140,185,0.2)' },
  docx: { label: 'DOC', color: '#8ea0d5', dim: 'rgba(142,160,213,0.14)', border: 'rgba(142,160,213,0.2)' },
  text: { label: 'TXT', color: '#c4a050', dim: 'rgba(196,160,80,0.14)', border: 'rgba(196,160,80,0.22)' },
  default: { label: 'FILE', color: '#8d8370', dim: 'rgba(141,131,112,0.12)', border: 'rgba(141,131,112,0.18)' },
}

const DISCOVERY_GROUPS = [
  { id: 'icloud', title: 'iCloud Drive', note: 'iCloud files will only be indexed when downloaded' },
  { id: 'external', title: 'External Drives', note: '' },
  { id: 'cloud', title: 'Cloud Storage', note: '' },
  { id: 'suggested', title: 'Other Locations', note: 'recently active folders' },
]
const STATUS_POLL_MS_IDLE = 30000
const STATUS_POLL_MS_SCANNING = 5000
const WS_POLL_FALLBACK_AFTER_MS = 10000
const WEBSITE_URL = 'https://recurdo.lakshitsachdeva.tech'
const PRIVACY_URL = `${WEBSITE_URL}/privacy/`
const TERMS_URL = `${WEBSITE_URL}/terms/`
const WAITLIST_URL = `${WEBSITE_URL}/#waitlist`

function fileTypeConfig(fileType) {
  return FILE_TYPE[fileType] || FILE_TYPE.default
}

function relativeHomePath(path) {
  if (!path) {
    return ''
  }
  const match = String(path).match(/^\/Users\/[^/]+(\/.*)?$/)
  if (match) {
    return `~${match[1] || ''}`
  }
  return String(path)
}

function normalizePath(path) {
  return String(path || '').replace(/\/+$/, '')
}

function formatFolderCount(count) {
  if (!count) {
    return '0'
  }
  if (count >= 9999) {
    return '999+'
  }
  return Number(count).toLocaleString()
}

function formatDuration(seconds) {
  if (seconds < 60) {
    return `${Math.max(1, Math.ceil(seconds))} sec`
  }
  if (seconds < 3600) {
    return `${Math.ceil(seconds / 60)} min`
  }
  return `${(seconds / 3600).toFixed(1)} hr`
}

function formatStorage(bytes) {
  if (bytes < 1024 * 1024) {
    return `${Math.ceil(bytes / 1024)} KB`
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${Math.ceil(bytes / 1024 / 1024)} MB`
  }
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString()
}

function normalizeBackgroundSnapshot(background = {}, scanPhase = {}, scanProgress = {}, pendingFiles = 0, isScanning = false) {
  const phase = String(background.phase || scanPhase.phase || '')
  const message = String(background.message || scanPhase.message || '')
  const hintedBackground = message.toLowerCase().includes('background')
  const activeBackgroundPhase = phase === 'background' || (message.toLowerCase().startsWith('continuing ') && hintedBackground)
  const derivedPending = activeBackgroundPhase
    ? Number(background.pending ?? scanProgress.remaining ?? 0)
    : (!isScanning && pendingFiles > 0 ? pendingFiles : Number(background.pending ?? 0))
  const pending = Math.max(0, Number(derivedPending || 0))
  const total = Math.max(
    pending,
    Number(background.total ?? ((hintedBackground || phase === 'background') ? scanProgress.total ?? 0 : 0) ?? 0),
  )
  const completed = Math.max(
    0,
    Number(background.completed ?? ((hintedBackground || phase === 'background') ? scanProgress.processed ?? 0 : 0) ?? 0),
  )

  let state = String(background.state || '')
  if (!state) {
    if (activeBackgroundPhase) {
      state = isScanning ? 'running' : 'queued'
    } else if (!isScanning && pendingFiles > 0) {
      state = 'retrying'
    } else {
      state = 'idle'
    }
  }

  return {
    phase,
    state,
    pending,
    active: Math.max(0, Number(background.active ?? ((state === 'running' || state === 'queued') ? 1 : 0) ?? 0)),
    completed,
    total,
    failed: Math.max(0, Number(background.failed ?? 0)),
    message,
  }
}

async function openExternalLink(url) {
  if (!url) {
    return
  }

  if (isTauri()) {
    try {
      await invokeIfTauri('open_url', { url })
      return
    } catch (error) {
      console.warn('Unable to open external link with Tauri bridge, falling back to browser open', error)
    }
  }

  if (typeof window !== 'undefined') {
    const popup = window.open(url, '_blank', 'noopener,noreferrer')
    if (popup) {
      popup.opener = null
      return
    }

    window.location.assign(url)
  }
}

function normalizeStatusSnapshot(next = {}) {
  const cognitive = normalizeCognitiveStatusSnapshot(next.cognitive || {}, next)
  const gmail = normalizeGmailStatusSnapshot(next.gmail || {}, next)
  const uniqueMemories = Number(next.unique_memories ?? next.total_items ?? 0) || 0
  const filesWatched = Number(next.files_watched ?? 0) || 0
  const duplicatesSkipped = Number(next.duplicates_skipped ?? 0) || 0
  const pendingFiles = Number(next.pending_files ?? 0) || 0
  const isScanning = Boolean(next.is_scanning)
  const rawProgress = next.scan_progress || {}
  const processed = Number(rawProgress.processed ?? rawProgress.indexed ?? 0) || 0
  const total = Number(rawProgress.total ?? 0) || 0
  const remaining = Number(rawProgress.remaining ?? Math.max(total - processed, 0)) || 0
  const percent = Number(rawProgress.percent ?? (total ? (processed / total) * 100 : 0)) || 0
  const shouldClearStaleProgress = !isScanning && pendingFiles === 0 && uniqueMemories === 0 && filesWatched === 0
  const phase = {
    phase: shouldClearStaleProgress ? '' : (next.scan_phase?.phase || ''),
    message: shouldClearStaleProgress ? '' : (next.scan_phase?.message || ''),
    found: shouldClearStaleProgress ? 0 : (Number(next.scan_phase?.found ?? 0) || 0),
    checked: shouldClearStaleProgress ? 0 : (Number(next.scan_phase?.checked ?? 0) || 0),
    total: shouldClearStaleProgress ? 0 : (Number(next.scan_phase?.total ?? 0) || 0),
    new_found: shouldClearStaleProgress ? 0 : (Number(next.scan_phase?.new_found ?? 0) || 0),
  }
  const progress = {
    processed: shouldClearStaleProgress ? 0 : processed,
    indexed: shouldClearStaleProgress ? 0 : processed,
    total: shouldClearStaleProgress ? 0 : total,
    remaining: shouldClearStaleProgress ? 0 : remaining,
    percent: shouldClearStaleProgress ? 0 : percent,
  }
  const background = normalizeBackgroundSnapshot(next.background, phase, progress, pendingFiles, isScanning)
  const isFullyIndexed = Boolean(next.is_fully_indexed ?? (!isScanning && background.pending === 0))

  return {
    total_items: uniqueMemories,
    unique_memories: uniqueMemories,
    files_watched: filesWatched,
    duplicates_skipped: duplicatesSkipped,
    pending_files: pendingFiles,
    queue_size: Number(next.queue_size ?? 0) || 0,
    is_scanning: isScanning,
    is_fully_indexed: isFullyIndexed,
    background,
    scan_phase: phase,
    scan_progress: progress,
    cognitive,
    gmail,
    llm: next.llm || null,
  }
}

function normalizeCognitiveBootstrapSnapshot(next = {}) {
  const source = next && typeof next === 'object' ? next : {}
  const summary = source.summary && typeof source.summary === 'object' ? source.summary : {}

  return {
    state: String(source.state || 'idle'),
    phase: String(source.phase || ''),
    message: String(source.message || ''),
    processed: Number(source.processed ?? 0) || 0,
    total: Number(source.total ?? 0) || 0,
    percent: Number(source.percent ?? 0) || 0,
    dry_run: Boolean(source.dry_run),
    reset: Boolean(source.reset),
    started_at: source.started_at || null,
    completed_at: source.completed_at || null,
    error: source.error || null,
    summary: {
      items: Number(summary.items ?? 0) || 0,
      activation_events: Number(summary.activation_events ?? 0) || 0,
      entity_edges: Number(summary.entity_edges ?? 0) || 0,
      frame_instances: Number(summary.frame_instances ?? 0) || 0,
    },
  }
}

function normalizeCognitiveStatusSnapshot(next = {}, fallback = {}) {
  const source = next && typeof next === 'object' ? next : {}
  const flat = fallback && typeof fallback === 'object' ? fallback : {}

  return {
    ready: Boolean(source.ready ?? flat.cognitive_layer_ready ?? false),
    db_path: String(source.db_path ?? flat.cognitive_db_path ?? ''),
    activation_events: Number(source.activation_events ?? flat.cognitive_activation_events ?? 0) || 0,
    entity_edges: Number(source.entity_edges ?? flat.cognitive_entity_edges ?? 0) || 0,
    frame_instances: Number(source.frame_instances ?? flat.cognitive_frame_instances ?? 0) || 0,
    bootstrap: normalizeCognitiveBootstrapSnapshot(source.bootstrap ?? flat.cognitive_bootstrap ?? {}),
  }
}

function normalizeGmailJobSnapshot(next = {}) {
  const source = next && typeof next === 'object' ? next : {}
  const summary = source.summary && typeof source.summary === 'object' ? source.summary : {}

  return {
    state: String(source.state || 'idle'),
    phase: String(source.phase || ''),
    message: String(source.message || ''),
    processed: Number(source.processed ?? 0) || 0,
    total: Number(source.total ?? 0) || 0,
    percent: Number(source.percent ?? 0) || 0,
    started_at: source.started_at || null,
    completed_at: source.completed_at || null,
    error: source.error || null,
    summary: { ...summary },
  }
}

function normalizeGmailStatusSnapshot(next = {}) {
  const source = next && typeof next === 'object' ? next : {}
  const policy = source.policy && typeof source.policy === 'object' ? source.policy : {}
  return {
    configured: Boolean(source.configured),
    oauthlib_available: Boolean(source.oauthlib_available),
    connected: Boolean(source.connected),
    client_secrets_path: String(source.client_secrets_path || ''),
    token_path: String(source.token_path || ''),
    sync_state_path: String(source.sync_state_path || ''),
    account_email: String(source.account_email || ''),
    last_history_id: String(source.last_history_id || ''),
    last_sync_at: source.last_sync_at || null,
    connected_at: source.connected_at || null,
    indexed_items: Number(source.indexed_items ?? 0) || 0,
    last_summary: source.last_summary && typeof source.last_summary === 'object' ? source.last_summary : {},
    policy: {
      default_bootstrap_days: Number(policy.default_bootstrap_days ?? 30) || 30,
      allowed_historical_days: Array.isArray(policy.allowed_historical_days)
        ? policy.allowed_historical_days.map((value) => Number(value) || 0).filter(Boolean)
        : [30, 90, 180],
      attachments_only_supported: policy.attachments_only_supported !== false,
      attachment_ingestion_supported: policy.attachment_ingestion_supported !== false,
      attachment_max_bytes: Number(policy.attachment_max_bytes ?? 0) || 0,
      attachment_supported_suffixes: Array.isArray(policy.attachment_supported_suffixes)
        ? policy.attachment_supported_suffixes
        : [],
      allowed_labels: Array.isArray(policy.allowed_labels) ? policy.allowed_labels : [],
      excluded_labels: Array.isArray(policy.excluded_labels) ? policy.excluded_labels : [],
    },
    oauth: normalizeGmailJobSnapshot(source.oauth || {}),
    sync: normalizeGmailJobSnapshot(source.sync || {}),
  }
}

function buildCognitiveSummaryLabel(cognitive) {
  const next = normalizeCognitiveStatusSnapshot(cognitive)
  const bootstrap = next.bootstrap

  if (bootstrap.state === 'running') {
    const progressLabel = bootstrap.total
      ? `${formatNumber(bootstrap.processed)} / ${formatNumber(bootstrap.total)}`
      : 'starting'
    return `${bootstrap.message || 'building cognitive memory'} · ${progressLabel}`
  }

  if (bootstrap.state === 'error') {
    return bootstrap.message || 'cognitive bootstrap needs attention'
  }

  if (bootstrap.state === 'completed' && bootstrap.dry_run) {
    const items = bootstrap.summary.items ? formatNumber(bootstrap.summary.items) : '0'
    return `dry run checked ${items} memories without writing`
  }

  if (bootstrap.state === 'completed' && bootstrap.summary.items) {
    return `last bootstrap seeded ${formatNumber(bootstrap.summary.items)} memories`
  }

  if (next.ready && next.activation_events) {
    return `${formatNumber(next.activation_events)} activations · ${formatNumber(next.entity_edges)} links · ${formatNumber(next.frame_instances)} frames`
  }

  if (next.ready) {
    return 'cognitive layer is ready to be seeded'
  }

  return 'cognitive layer has not been bootstrapped yet'
}

function buildCognitiveStateLabel(cognitive) {
  const next = normalizeCognitiveStatusSnapshot(cognitive)
  const bootstrap = next.bootstrap

  if (bootstrap.state === 'running') {
    return bootstrap.percent ? `bootstrapping ${Math.round(bootstrap.percent)}%` : 'bootstrapping'
  }

  if (bootstrap.state === 'error') {
    return 'needs attention'
  }

  if (next.activation_events || next.frame_instances || next.entity_edges) {
    return `${formatNumber(next.frame_instances)} frames · ${formatNumber(next.entity_edges)} links`
  }

  if (next.ready) {
    return 'ready to seed'
  }

  return 'not bootstrapped'
}

function buildGmailSummaryLabel(gmail) {
  const next = normalizeGmailStatusSnapshot(gmail)
  const bootstrapDays = next.policy.default_bootstrap_days || 30
  if (next.oauth.state === 'running') {
    return next.oauth.message || 'waiting for Gmail authorization'
  }
  if (next.oauth.state === 'error') {
    return next.oauth.message || 'gmail authentication needs attention'
  }
  if (next.sync.state === 'running') {
    const progress = next.sync.total
      ? `${formatNumber(next.sync.processed)} / ${formatNumber(next.sync.total)}`
      : formatNumber(next.sync.processed)
    return `${next.sync.message || 'syncing gmail'} · ${progress}`
  }
  if (next.sync.state === 'cancelling') {
    return next.sync.message || 'stopping gmail sync'
  }
  if (next.sync.state === 'error') {
    return next.sync.message || 'gmail sync needs attention'
  }
  if (next.sync.state === 'cancelled') {
    return next.sync.message || 'gmail sync stopped'
  }
  if (!next.configured) {
    return 'gmail client secrets are not configured yet'
  }
  if (!next.oauthlib_available) {
    return 'gmail oauth dependency is missing in this build'
  }
  if (!next.connected) {
    return 'gmail is ready to connect'
  }
  if (next.last_sync_at) {
    return `${formatNumber(next.indexed_items)} gmail memories · last sync ${relativeTime(next.last_sync_at)}`
  }
  return `${formatNumber(next.indexed_items)} gmail memories · first safe import uses the last ${bootstrapDays} days`
}

function buildGmailStateLabel(gmail) {
  const next = normalizeGmailStatusSnapshot(gmail)
  if (next.oauth.state === 'running') {
    return 'connecting'
  }
  if (next.oauth.state === 'error') {
    return 'needs attention'
  }
  if (next.sync.state === 'running') {
    return next.sync.percent ? `syncing ${Math.round(next.sync.percent)}%` : 'syncing'
  }
  if (next.sync.state === 'cancelling') {
    return 'stopping'
  }
  if (next.sync.state === 'error') {
    return 'needs attention'
  }
  if (next.sync.state === 'cancelled') {
    return 'stopped'
  }
  if (next.connected) {
    return next.last_sync_at ? 'connected and synced' : 'connected'
  }
  if (next.configured) {
    return 'ready to connect'
  }
  return 'not configured'
}

function buildProactiveSubtitle(context = {}) {
  if (context.script_name && context.current_scene && context.next_scene) {
    return `${context.script_name} · ${context.current_scene} → ${context.next_scene}`
  }
  if (context.script_name && context.current_scene) {
    return `${context.script_name} · ${context.current_scene}`
  }
  if (context.current_frame) {
    return `${String(context.current_frame).replace(/_/g, ' ')} context`
  }
  return 'local proactive surfaces from your memory graph'
}

function formatRuleTypeLabel(actionType = '') {
  return String(actionType || '')
    .replace(/_/g, ' ')
    .trim() || 'proactive rule'
}

function ProactiveContextCard({ payload, loading, onRefresh }) {
  const rules = Array.isArray(payload?.rules) ? payload.rules : []
  const context = payload?.context && typeof payload.context === 'object' ? payload.context : {}

  return (
    <div className="surface-card proactive-card">
      <div className="proactive-card-head">
        <div>
          <div className="status-pill proactive-pill">
            <span className="pulse" />
            noticed before you asked
          </div>
          <div className="proactive-subtitle">{buildProactiveSubtitle(context)}</div>
        </div>
        <button type="button" className="proactive-refresh" onClick={() => void onRefresh?.()}>
          refresh
        </button>
      </div>

      {loading && !rules.length ? (
        <div className="proactive-empty">reading your current working context…</div>
      ) : null}

      {!loading && !rules.length ? (
        <div className="proactive-empty">
          no proactive surfaces right now. as more scripts and frames build up, this card will get sharper.
        </div>
      ) : null}

      {rules.length ? (
        <div className="proactive-rule-list">
          {rules.map((rule) => (
            <div key={rule.rule_id} className="proactive-rule-row">
              <div className="proactive-rule-top">
                <div className="proactive-rule-kind">
                  {formatRuleTypeLabel(rule.action?.type)}
                </div>
                <div className="proactive-rule-priority">p{rule.priority}</div>
              </div>
              <div className="proactive-rule-message">{rule.action?.message || 'proactive surface'}</div>
              {Array.isArray(rule.surfaced_items) && rule.surfaced_items.length ? (
                <div className="proactive-related-list">
                  {rule.surfaced_items.slice(0, 3).map((item) => (
                    <div key={`${rule.rule_id}-${item.item_id || item.title}`} className="proactive-related-item">
                      <span className="proactive-related-title">{item.title || item.item_id}</span>
                      {item.frame_type ? (
                        <span className="proactive-related-meta">
                          {String(item.frame_type).replace(/_/g, ' ')}
                        </span>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function buildStartupHealthMessage(actions = []) {
  if (actions.length === 1 && actions.includes('flagged_missing_folders')) {
    return 'Recurdo found a startup issue · some watched folders are unavailable.'
  }
  if (actions.includes('started_faiss_rebuild')) {
    return 'Recurdo repaired an issue on startup · rebuilding index...'
  }
  if (actions.includes('started_scan')) {
    return 'Recurdo repaired an issue on startup · rechecking your library...'
  }
  return 'Recurdo repaired an issue on startup.'
}

function buildScanProgressLabel(status) {
  if (status?.background?.state === 'retrying' && status?.background?.pending) {
    return `search is ready · ${formatNumber(status.background.pending)} files retrying in background`
  }

  if (status?.background?.state === 'running' && status?.background?.pending) {
    return `search is ready · indexing ${formatNumber(status.background.pending)} larger files in background`
  }

  if (!status?.is_scanning) {
    return status?.is_fully_indexed ? 'your memory is up to date' : ''
  }

  const phaseMessage = String(status.scan_phase?.message || '').toLowerCase()

  if (status.scan_phase?.phase === 'discovering') {
    const found = Number(status.scan_phase?.found || 0).toLocaleString()
    return found ? `discovering ${found} files` : 'discovering files'
  }

  if (status.scan_phase?.phase === 'checking' && status.scan_phase?.total) {
    const checked = Number(status.scan_phase?.checked || 0).toLocaleString()
    const total = Number(status.scan_phase?.total || 0).toLocaleString()
    return `checking ${checked} / ${total} files`
  }

  if (status.scan_phase?.phase === 'rebuilding') {
    return 'rebuilding search index from stored memories'
  }

  if ((phaseMessage.includes('background') || status?.background?.state === 'running') && status.background?.pending) {
    return `search is ready · finishing ${formatNumber(status.background.pending)} larger files in background`
  }

  if (status.scan_progress?.total) {
    return `indexing · ${formatNumber(status.scan_progress.processed || 0)} files processed · ${formatNumber(status.scan_progress.remaining || 0)} remaining`
  }

  return status.scan_phase?.message || ''
}

function buildStatusPillLabel(status) {
  if (status?.is_scanning && status.scan_phase?.phase === 'discovering') {
    return 'discovering files'
  }
  if (status?.is_scanning && status.scan_phase?.phase === 'rebuilding') {
    return 'rebuilding index'
  }
  if (status?.background?.state === 'running' && status?.background?.pending) {
    return 'search ready'
  }
  if (status?.background?.state === 'retrying' && status?.background?.pending) {
    return `retrying ${formatNumber(status.background.pending)} files`
  }
  if (status?.is_scanning && status.scan_phase?.phase === 'checking') {
    return 'checking changes'
  }
  if (status?.is_scanning && status.scan_progress?.percent) {
    return `indexing ${status.scan_progress.percent}%`
  }
  if (status?.is_scanning) {
    return 'indexing now'
  }
  if (status?.is_fully_indexed) {
    return 'fully indexed'
  }
  return 'ready to search'
}

function IndexStatusBar({ status }) {
  const backgroundPending = Number(status?.background?.pending || 0)
  const backgroundActive = status?.background?.state === 'running' && backgroundPending > 0
  const retrying = status?.background?.state === 'retrying' && backgroundPending > 0

  return (
    <div className={`index-status-bar${backgroundActive ? ' is-indexing' : ''}${status?.is_fully_indexed ? ' is-done' : ''}`}>
      <div className="index-status-item index-status-item-primary">
        <span className="index-status-number">{formatNumber(status?.unique_memories)}</span>
        <span className="index-status-label">memories</span>
      </div>

      <div className="index-status-divider" />

      <div className="index-status-item">
        <span className="index-status-number">{formatNumber(status?.files_watched)}</span>
        <span className="index-status-label">files watched</span>
      </div>

      {backgroundActive || retrying ? (
        <>
          <div className="index-status-divider" />
          <div className={`index-status-item ${backgroundActive ? 'index-status-item-active' : 'index-status-item-retrying'}`}>
            {backgroundActive ? <span className="index-status-pulse" /> : null}
            <span className="index-status-number">{formatNumber(backgroundPending)}</span>
            <span className="index-status-label">
              {backgroundActive ? 'indexing in background' : 'retrying'}
            </span>
          </div>
        </>
      ) : null}

      {status?.is_fully_indexed ? (
        <>
          <div className="index-status-divider" />
          <div className="index-status-item index-status-item-done">
            <span className="index-status-label">your memory is up to date</span>
          </div>
        </>
      ) : null}
    </div>
  )
}

function websocketStatusMeta(status) {
  if (status === 'connected') {
    return {
      label: 'connected',
      dotColor: '#78d08f',
      pulse: false,
      title: 'Live updates connected.',
    }
  }

  if (status === 'disconnected') {
    return {
      label: 'disconnected',
      dotColor: '#d86f6f',
      pulse: false,
      title: 'Live updates disconnected. Recurdo will retry automatically.',
    }
  }

  return {
    label: 'connecting',
    dotColor: 'rgba(255,255,255,0.28)',
    pulse: true,
    title: 'Connecting to live updates…',
  }
}

function accessLabel(session) {
  const tier = String(session?.access?.tier || '').toLowerCase()
  if (tier === 'whitelist' || tier === 'beta') {
    return 'private beta'
  }
  if (tier === 'expired') {
    return 'beta expired'
  }
  if (tier) {
    return tier
  }
  return 'active'
}

function accessVerificationLabel(session) {
  const access = session?.access || {}

  if (access.verification_unavailable) {
    return 'could not verify access right now'
  }
  if (access.offline_cached) {
    return 'temporarily using saved access state'
  }

  const mode = String(access.verification_mode || '')
  if (mode === 'supabase-rpc') {
    return 'account verified'
  }
  if (mode === 'remote-access-api') {
    return 'account verified'
  }
  if (mode === 'local-dev') {
    return 'development access'
  }

  return 'account verified'
}

function Greeting({ session }) {
  const name = (() => {
    const fullName = String(session?.user?.name || '').trim()
    if (fullName) return fullName.split(/\s+/)[0].toLowerCase()
    const email = String(session?.user?.email || '').trim()
    if (email) return email.split('@')[0].toLowerCase()
    return 'there'
  })()

  return (
    <div style={{ marginBottom: 12 }}>
      <div
        style={{
          fontFamily: 'var(--serif)',
          fontSize: 'clamp(32px, 4vw, 52px)',
          fontWeight: 300,
          fontStyle: 'italic',
          color: 'var(--text-1)',
          letterSpacing: '-0.01em',
          lineHeight: 1.1,
        }}
      >
        hello, {name}.
      </div>
    </div>
  )
}

function GridIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  )
}

function FileIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

function CaseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h3l1.5 2h6.5A2.5 2.5 0 0 1 20 9.5v8A2.5 2.5 0 0 1 17.5 20h-11A2.5 2.5 0 0 1 4 17.5z" />
      <path d="M8 12h8M8 15.5h5" />
    </svg>
  )
}

function FolderGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2h8.5A1.5 1.5 0 0 1 21 8.5v8A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z" />
    </svg>
  )
}

function DriveGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <rect x="3" y="6" width="18" height="10" rx="2" />
      <path d="M7 19h10" />
      <circle cx="16.5" cy="11" r="0.75" fill="currentColor" stroke="none" />
    </svg>
  )
}

function CloudGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M7 18a4 4 0 1 1 .62-7.95A5.5 5.5 0 0 1 18 11.5h.5a3.5 3.5 0 0 1 0 7H7z" />
    </svg>
  )
}

function TinyBadge({ fileType }) {
  const config = fileTypeConfig(fileType)
  return (
    <div
      className="tiny-badge"
      style={{
        background: config.dim,
        color: config.color,
        borderColor: config.border,
      }}
    >
      {config.label}
    </div>
  )
}

function DiscoveryGroup({
  group,
  entries,
  isOpen,
  onToggle,
  onAdd,
  addingPaths,
  watchedPaths,
}) {
  if (!entries.length) {
    return null
  }

  const icon =
    group.id === 'external' ? <DriveGlyph /> : group.id === 'cloud' || group.id === 'icloud' ? <CloudGlyph /> : <FolderGlyph />

  return (
    <div className="folder-group">
      <button type="button" className="folder-group-head" onClick={onToggle}>
        <div className="folder-group-title-wrap">
          <span className="folder-group-chevron">{isOpen ? '−' : '+'}</span>
          <span className="folder-group-title">{group.title}</span>
          {group.note ? <span className="folder-group-note">{group.note}</span> : null}
        </div>
      </button>

      {isOpen ? (
        <div className="folder-group-body">
          {entries.map((entry) => {
            const isAdded = watchedPaths.has(entry.path) || addingPaths.has(entry.path)
            const fileCountLabel = `${formatFolderCount(entry.file_count)} files`
            return (
              <div key={entry.path} className="folder-discovery-row">
                <div className="folder-discovery-left">
                  <span className="folder-discovery-icon">{icon}</span>
                  <div className="folder-discovery-copy">
                    <div className="folder-discovery-label">
                      {entry.label}
                      {group.id === 'suggested' ? (
                        <span className="folder-discovery-path">
                          {relativeHomePath(entry.path)}
                        </span>
                      ) : null}
                      {entry.is_network ? <span className="folder-discovery-tag">(network)</span> : null}
                    </div>
                    {group.id !== 'suggested' ? (
                      <div className="folder-discovery-path">{relativeHomePath(entry.path)}</div>
                    ) : null}
                  </div>
                </div>
                <div className="folder-discovery-right">
                  <span className="folder-count-badge">{fileCountLabel}</span>
                  <button
                    type="button"
                    className="folder-add-btn"
                    disabled={isAdded}
                    onClick={() => onAdd(entry, group.id)}
                  >
                    {isAdded ? 'added' : 'watch'}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

// ─── Memory Browser Section ──────────────────────────────────────────────────

const FILE_TYPE_TABS = [
  { label: 'All', value: '' },
  { label: 'Docs', value: 'doc' },
  { label: 'PDFs', value: 'pdf' },
  { label: 'Emails', value: 'email' },
  { label: 'Images', value: 'image' },
  { label: 'Spreadsheets', value: 'spreadsheet' },
]

const FT_LABELS = { pdf: 'PDF', doc: 'DOC', email: 'EMAIL', image: 'IMG', spreadsheet: 'XLS', presentation: 'PPT', text: 'TXT' }

function MemoryBrowserSection({ authHeaders }) {
  const [open, setOpen] = useState(true)
  const [searchQ, setSearchQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [fileType, setFileType] = useState('')
  const [sort, setSort] = useState('recent')
  const [page, setPage] = useState(1)
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const debounceRef = useRef(null)

  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => {
      setDebouncedQ(searchQ)
      setPage(1)
      setItems([])
    }, 300)
    return () => { if (debounceRef.current) window.clearTimeout(debounceRef.current) }
  }, [searchQ])

  const fetchItems = useCallback(async (nextPage, reset) => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      params.set('page', String(nextPage))
      params.set('limit', '20')
      if (debouncedQ) params.set('q', debouncedQ)
      if (fileType) params.set('file_type', fileType)
      params.set('sort', sort)
      const base = await getBackendHttpUrl()
      const res = await fetch(`${base}/items?${params.toString()}`, { headers: authHeaders })
      if (!res.ok) throw new Error(`${res.status}`)
      const data = await res.json()
      const newItems = Array.isArray(data.items) ? data.items : []
      setItems((prev) => reset ? newItems : [...prev, ...newItems])
      setTotal(Number(data.total) || 0)
    } catch (err) {
      setError(err?.message || 'Failed to load memories')
    } finally {
      setLoading(false)
    }
  }, [authHeaders, debouncedQ, fileType, sort])

  useEffect(() => {
    setItems([])
    setPage(1)
    void fetchItems(1, true)
  }, [debouncedQ, fileType, sort]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleLoadMore = useCallback(() => {
    const nextPage = page + 1
    setPage(nextPage)
    void fetchItems(nextPage, false)
  }, [fetchItems, page])

  const handleDelete = useCallback(async (id) => {
    try {
      const base = await getBackendHttpUrl()
      await fetch(`${base}/items/${id}`, { method: 'DELETE', headers: authHeaders })
      setItems((prev) => prev.filter((item) => item.id !== id))
      setTotal((prev) => Math.max(0, prev - 1))
    } catch (err) {
      console.warn('Delete item failed', err)
    }
  }, [authHeaders])

  const canLoadMore = items.length < total

  return (
    <div className="settings-sub-section">
      <div
        className="settings-sub-header"
        onClick={() => setOpen((o) => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setOpen((o) => !o)}
      >
        <span className="section-title">Memory Browser</span>
        <span className="settings-sub-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="folder-settings-block">
          <input
            className="mb-search"
            type="text"
            placeholder="Filter by filename…"
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
          />
          <div className="mb-filter-bar">
            {FILE_TYPE_TABS.map((tab) => (
              <button
                key={tab.value}
                type="button"
                onClick={() => { setFileType(tab.value); setPage(1); setItems([]) }}
                className={`mb-tab${fileType === tab.value ? ' active' : ''}`}
              >
                {tab.label}
              </button>
            ))}
            <span className="mb-sort-group">
              {['recent', 'name'].map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => { setSort(s); setPage(1); setItems([]) }}
                  className={`mb-sort-btn${sort === s ? ' active' : ''}`}
                >
                  {s === 'recent' ? 'Recent' : 'Name'}
                </button>
              ))}
            </span>
          </div>

          <div className="mb-count">
            {loading && items.length === 0
              ? 'loading…'
              : error
                ? `error: ${error}`
                : `${items.length} of ${total} memories`}
          </div>

          <div className="mb-scrollbox">
            {items.map((item) => (
              <MemoryRow key={item.id} item={item} onDelete={handleDelete} />
            ))}
            {!loading && !error && items.length === 0 && (
              <div className="mb-empty">No memories found.</div>
            )}
            {loading && <div className="mb-empty">Loading…</div>}
          </div>

          {canLoadMore && !loading && (
            <button type="button" onClick={handleLoadMore} className="mb-load-more">
              Load more
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function MemoryRow({ item, onDelete }) {
  const filename = (item.original_path || item.title || '').split('/').pop() || item.original_path || '—'
  const ft = item.file_type || 'default'
  const cfg = fileTypeConfig(ft)
  const dateStr = item.file_mtime ? relativeTime(item.file_mtime) : '—'

  return (
    <div className="mb-row">
      <span
        className="mb-badge"
        style={{ background: cfg.dim, color: cfg.color, border: `1px solid ${cfg.border}` }}
      >
        {FT_LABELS[ft] || 'FILE'}
      </span>
      <span className="mb-row-name" title={item.original_path || item.title || ''}>
        {filename}
      </span>
      <span className="mb-row-date">{dateStr}</span>
      <button
        type="button"
        className="mb-row-delete"
        onClick={() => onDelete(item.id)}
        title="Remove from memory"
      >
        ✕
      </button>
    </div>
  )
}

// ─── Activity Timeline Section ────────────────────────────────────────────────

function formatTimelineDate(dateStr) {
  if (!dateStr) return ''
  const today = new Date()
  const d = new Date(dateStr)
  const todayStr = today.toISOString().slice(0, 10)
  const yesterdayDate = new Date(today)
  yesterdayDate.setDate(today.getDate() - 1)
  const yesterdayStr = yesterdayDate.toISOString().slice(0, 10)
  if (dateStr === todayStr) return 'Today'
  if (dateStr === yesterdayStr) return 'Yesterday'
  return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric' })
}

function formatTimePart(isoString) {
  if (!isoString) return ''
  try {
    return new Date(isoString).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
}

function TimelineSection({ authHeaders }) {
  const [open, setOpen] = useState(true)
  const [days, setDays] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const base = await getBackendHttpUrl()
        const res = await fetch(`${base}/activity/timeline?days=30`, { headers: authHeaders })
        if (!res.ok) throw new Error(`${res.status}`)
        const data = await res.json()
        if (!cancelled) setDays(Array.isArray(data.days) ? data.days : [])
      } catch (err) {
        if (!cancelled) setError(err?.message || 'Failed to load timeline')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [authHeaders])

  return (
    <div className="settings-sub-section">
      <div
        className="settings-sub-header"
        onClick={() => setOpen((o) => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setOpen((o) => !o)}
      >
        <span className="section-title">Activity Timeline</span>
        <span className="settings-sub-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="folder-settings-block">
          {loading && <div className="tl-empty">Loading timeline…</div>}
          {error && <div className="tl-empty" style={{ color: 'rgba(213,138,122,0.8)' }}>Error: {error}</div>}
          {!loading && !error && days.length === 0 && (
            <div className="tl-empty">No activity in the last 30 days.</div>
          )}
          <div className="tl-scrollbox">
            {days.map((day) => {
              const visibleItems = Array.isArray(day.items) ? day.items.slice(0, 5) : []
              const extra = (day.count || 0) - visibleItems.length
              return (
                <div key={day.date}>
                  <div className="tl-day-header">
                    {formatTimelineDate(day.date)}
                    <span className="tl-day-count">{day.count} {day.count === 1 ? 'file' : 'files'}</span>
                  </div>
                  {visibleItems.map((item) => {
                    const ft = item.file_type || 'default'
                    const cfg = fileTypeConfig(ft)
                    const filename = (item.file_path || '').split('/').pop() || item.file_path || '—'
                    const timeStr = formatTimePart(item.file_mtime)
                    return (
                      <div key={item.id} className="tl-file-row">
                        <span
                          className="mb-badge"
                          style={{ background: cfg.dim, color: cfg.color, border: `1px solid ${cfg.border}` }}
                        >
                          {FT_LABELS[ft] || 'FILE'}
                        </span>
                        <span className="tl-file-name">{filename}</span>
                        {timeStr && <span className="tl-file-time">{timeStr}</span>}
                      </div>
                    )
                  })}
                  {extra > 0 && <div className="tl-more">+{extra} more</div>}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Smart Aliases Section ────────────────────────────────────────────────────

function AliasesSection({ authHeaders }) {
  const [open, setOpen] = useState(true)
  const [aliases, setAliases] = useState({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [nameInput, setNameInput] = useState('')
  const [expansionInput, setExpansionInput] = useState('')
  const [addError, setAddError] = useState('')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const base = await getBackendHttpUrl()
        const res = await fetch(`${base}/user/settings`, { headers: authHeaders })
        if (!res.ok) throw new Error(`${res.status}`)
        const data = await res.json()
        if (!cancelled) setAliases(data.aliases && typeof data.aliases === 'object' ? data.aliases : {})
      } catch (err) {
        if (!cancelled) setError(err?.message || 'Failed to load aliases')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [authHeaders])

  const saveAliases = useCallback(async (updated) => {
    setSaving(true)
    setError('')
    try {
      const base = await getBackendHttpUrl()
      const res = await fetch(`${base}/user/settings`, {
        method: 'POST',
        headers: { ...authHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ aliases: updated }),
      })
      if (!res.ok) throw new Error(`${res.status}`)
      setAliases(updated)
    } catch (err) {
      setError(err?.message || 'Failed to save aliases')
    } finally {
      setSaving(false)
    }
  }, [authHeaders])

  const handleAdd = useCallback(async () => {
    const name = nameInput.trim()
    const expansion = expansionInput.trim()
    if (!name) { setAddError('Name is required'); return }
    if (!expansion) { setAddError('Expansion is required'); return }
    setAddError('')
    const updated = { ...aliases, [name]: expansion }
    await saveAliases(updated)
    setNameInput('')
    setExpansionInput('')
  }, [aliases, expansionInput, nameInput, saveAliases])

  const handleDelete = useCallback(async (name) => {
    const updated = { ...aliases }
    delete updated[name]
    await saveAliases(updated)
  }, [aliases, saveAliases])

  const aliasEntries = Object.entries(aliases)

  return (
    <div className="settings-sub-section">
      <div
        className="settings-sub-header"
        onClick={() => setOpen((o) => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setOpen((o) => !o)}
      >
        <span className="section-title">Smart Aliases</span>
        <span className="settings-sub-chevron">{open ? '▲' : '▼'}</span>
      </div>
      {open && (
        <div className="folder-settings-block">
          <div className="alias-caption">
            Define shorthand terms that auto-expand when you search — e.g. &ldquo;uni&rdquo; → &ldquo;university project&rdquo;.
          </div>

          {loading && <div className="alias-empty">Loading…</div>}
          {error && <div className="alias-empty" style={{ color: 'rgba(213,138,122,0.8)' }}>Error: {error}</div>}

          {aliasEntries.length > 0 ? (
            <div className="alias-list">
              {aliasEntries.map(([name, expansion]) => (
                <div key={name} className="alias-row">
                  <span className="alias-key">{name}</span>
                  <span className="alias-arrow">→</span>
                  <span className="alias-val">{expansion}</span>
                  <button
                    type="button"
                    className="alias-delete"
                    onClick={() => void handleDelete(name)}
                    disabled={saving}
                    title="Remove alias"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          ) : (
            !loading && <div className="alias-empty">No aliases defined yet.</div>
          )}

          <div className="alias-add-row">
            <input
              className="alias-input"
              type="text"
              placeholder="shortcut"
              value={nameInput}
              onChange={(e) => { setNameInput(e.target.value); setAddError('') }}
              onKeyDown={(e) => e.key === 'Enter' && void handleAdd()}
              style={{ maxWidth: 130 }}
            />
            <input
              className="alias-input"
              type="text"
              placeholder="expands to…"
              value={expansionInput}
              onChange={(e) => { setExpansionInput(e.target.value); setAddError('') }}
              onKeyDown={(e) => e.key === 'Enter' && void handleAdd()}
            />
            <button
              type="button"
              className="alias-add-btn"
              disabled={saving || !nameInput.trim() || !expansionInput.trim()}
              onClick={() => void handleAdd()}
            >
              {saving ? '…' : 'Add'}
            </button>
          </div>
          {addError && <div className="alias-add-error">{addError}</div>}
        </div>
      )}
    </div>
  )
}

function AppearanceSection({ authHeaders, onSaved }) {
  const [accent, setAccent] = useState(() => loadStoredAccentId())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const base = await getBackendHttpUrl()
        const res = await fetch(`${base}/user/settings`, { headers: authHeaders })
        if (!res.ok) return
        const data = await res.json()
        if (cancelled || !data?.accent_color) return
        const nextAccent = storeAndApplyAccent(data.accent_color)
        setAccent(nextAccent)
      } catch {
        // Local accent is already applied from localStorage.
      }
    }
    void load()
    return () => { cancelled = true }
  }, [authHeaders])

  const saveAccent = useCallback(async (nextAccent) => {
    setAccent(storeAndApplyAccent(nextAccent))
    setSaving(true)
    setError('')
    try {
      const base = await getBackendHttpUrl()
      const res = await fetch(`${base}/user/settings`, {
        method: 'POST',
        headers: { ...authHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ accent_color: nextAccent }),
      })
      if (!res.ok) throw new Error(`${res.status}`)
      onSaved?.('accent saved')
    } catch (err) {
      setError(err?.message || 'failed to save accent')
    } finally {
      setSaving(false)
    }
  }, [authHeaders, onSaved])

  return (
    <>
      <div className="section-title">appearance</div>
      <div className="section-caption">
        personalize the accent used across search, settings, and status highlights.
      </div>
      <div className="folder-settings-block accent-settings-block">
        <div className="accent-grid" role="radiogroup" aria-label="Accent color">
          {ACCENT_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`accent-swatch${accent === option.id ? ' is-active' : ''}`}
              onClick={() => void saveAccent(option.id)}
              disabled={saving}
              role="radio"
              aria-checked={accent === option.id}
              title={`${option.label} accent`}
            >
              <span
                className="accent-swatch-dot"
                style={{ backgroundColor: option.color }}
              />
              <span className="accent-swatch-label">{option.label}</span>
            </button>
          ))}
        </div>
        {error ? <div className="alias-add-error">could not save accent: {error}</div> : null}
      </div>
    </>
  )
}

function UpdatesSection() {
  const [state, setState] = useState('idle')
  const [message, setMessage] = useState('check for the newest private beta build')
  const [availableUpdate, setAvailableUpdate] = useState(null)
  const [downloadProgress, setDownloadProgress] = useState(null)
  const [notes, setNotes] = useState('')

  const checking = state === 'checking'
  const installing = state === 'downloading' || state === 'installing'
  const busy = checking || installing

  const handleCheck = useCallback(async () => {
    setState('checking')
    setMessage('checking for updates...')
    setDownloadProgress(null)
    setAvailableUpdate(null)
    setNotes('')

    try {
      const result = await checkForRecurdoUpdate()
      if (result.unsupported) {
        setState('error')
        setMessage(result.message)
        return
      }

      if (!result.available) {
        setState('up_to_date')
        setMessage('you are on the latest private beta')
        return
      }

      setState('available')
      setAvailableUpdate(result.update)
      setNotes(result.notes || '')
      setMessage(`version ${result.version} is ready`)
    } catch (error) {
      setState('error')
      setMessage(String(error?.message || 'could not check for updates'))
    }
  }, [])

  const handleInstall = useCallback(async () => {
    if (!availableUpdate) {
      return
    }

    setState('downloading')
    setMessage('downloading update...')
    setDownloadProgress({ percent: 0 })

    try {
      await installRecurdoUpdate(availableUpdate, (progress) => {
        setDownloadProgress(progress)
        if (progress.event === 'Finished') {
          setState('installing')
          setMessage('installing and relaunching...')
        }
      })
    } catch (error) {
      setState('error')
      setMessage(String(error?.message || 'could not install update'))
    }
  }, [availableUpdate])

  return (
    <>
      <div className="section-title">updates</div>
      <div className="section-caption">
        private beta builds install from the signed Recurdo update feed.
      </div>
      <div className="folder-settings-block settings-links-block update-settings-block">
        <div className="cognitive-header-row">
          <div className="cognitive-header-copy">
            <div className={`cognitive-state-pill cognitive-state-${state === 'error' ? 'error' : installing || checking ? 'running' : state === 'available' ? 'completed' : 'idle'}`}>
              {state === 'available' ? 'update available' : state === 'up_to_date' ? 'latest build' : installing ? 'installing' : checking ? 'checking' : state === 'error' ? 'needs attention' : 'manual check'}
            </div>
            <div className="settings-links-copy">{message}</div>
            {notes ? <div className="update-notes">{notes}</div> : null}
          </div>
          <div className="cognitive-actions">
            <button
              type="button"
              className="folder-add-btn"
              disabled={busy}
              onClick={() => void handleCheck()}
            >
              {checking ? 'checking...' : 'check'}
            </button>
            <button
              type="button"
              className="folder-add-btn"
              disabled={busy || !availableUpdate}
              onClick={() => void handleInstall()}
            >
              {installing ? 'installing...' : 'install'}
            </button>
          </div>
        </div>

        {downloadProgress ? (
          <div className="update-progress-shell">
            <div className="update-progress-track">
              <div
                className="update-progress-fill"
                style={{ width: `${Math.max(4, Math.min(downloadProgress.percent || 0, 100))}%` }}
              />
            </div>
            <div className="folder-progress-copy">
              {downloadProgress.percent
                ? `${downloadProgress.percent}% downloaded`
                : 'starting download...'}
            </div>
          </div>
        ) : null}
      </div>
    </>
  )
}

// ─────────────────────────────────────────────────────────────────────────────

function FolderSettingsPage({
  authHeaders,
  session,
  watchedFolders,
  discovery,
  discoveryLoading,
  status,
  addingPaths,
  onBootstrapCognitive,
  onCleanupGmail,
  onConnectGmail,
  onDisconnectGmail,
  onStopGmailSync,
  onSyncGmail,
  onOpenPicker,
  onRemoveFolder,
  onAddFolder,
  customPath,
  customPathError,
  onCustomPathChange,
  onCustomPathSubmit,
  onSaveExcludedDirs,
  onSignOut,
  onShowToast,
}) {
  const watchedPaths = useMemo(
    () => new Set(watchedFolders.map((folder) => folder.path)),
    [watchedFolders],
  )
  const signedInEmail = String(session?.user?.email || session?.email || '').trim() || 'unknown email'
  const accessSummary = accessLabel(session)
  const accessVerification = accessVerificationLabel(session)
  const cognitive = normalizeCognitiveStatusSnapshot(status?.cognitive || {})
  const cognitiveBootstrap = cognitive.bootstrap
  const cognitiveBusy = cognitiveBootstrap.state === 'running'
  const cognitiveSummary = buildCognitiveSummaryLabel(cognitive)
  const gmail = normalizeGmailStatusSnapshot(status?.gmail || {})
  const gmailOauthBusy = gmail.oauth.state === 'running'
  const gmailSyncBusy = gmail.sync.state === 'running' || gmail.sync.state === 'cancelling'
  const gmailBusy = gmailOauthBusy || gmailSyncBusy
  const gmailSummary = buildGmailSummaryLabel(gmail)
  const gmailBootstrapDays = gmail.policy.default_bootstrap_days || 30
  const gmailHistoricalDays = Array.isArray(gmail.policy.allowed_historical_days) && gmail.policy.allowed_historical_days.length
    ? gmail.policy.allowed_historical_days
    : [30, 90, 180]
  const llm = status?.llm || {}
  const llmAvailable = Boolean(llm.available)
  const llmProvider = String(llm.provider || 'heuristic')
  const geminiVisionEnabled = Boolean(llm.gemini_vision_enabled)
  // Excluded dirs state
  const [excludedDirs, setExcludedDirs] = useState(() => llm.excluded_dirs || [])
  const [excludeInput, setExcludeInput] = useState('')
  const [excludeSaving, setExcludeSaving] = useState(false)
  // Sync excluded_dirs when status updates
  useState(() => { setExcludedDirs(llm.excluded_dirs || []) })
  const [openGroups, setOpenGroups] = useState({
    icloud: true,
    external: true,
    cloud: true,
    suggested: true,
  })
  const discoveredCount = DISCOVERY_GROUPS.reduce(
    (count, group) => count + (Array.isArray(discovery[group.id]) ? discovery[group.id].length : 0),
    0,
  )

  return (
    <div className="folder-settings-page">
      <AppearanceSection authHeaders={authHeaders} onSaved={onShowToast} />

      <div className="section-title">ask mode (AI)</div>
      <div className="section-caption">
        ask mode gives you real synthesized answers when you press ⌘↵. during private beta, api keys are managed by the recurdo team (there is no in-app key field).
      </div>
      <div className="folder-settings-block settings-links-block">
        <div className="cognitive-header-row">
          <div className="cognitive-header-copy">
            <div className={`cognitive-state-pill cognitive-state-${llmAvailable ? 'completed' : 'idle'}`}>
              {llmAvailable ? `active · ${llmProvider}` : 'not configured'}
            </div>
            <div className="settings-links-copy">
              {llmAvailable
                ? `ask mode is powered by ${llmProvider}. questions get real synthesized answers with citations.`
                : 'ask mode is not enabled in this build yet.'}
            </div>
          </div>
        </div>
      </div>

      {llmAvailable ? (
        <>
          <div className="section-title">photo descriptions</div>
          <div className="section-caption">
            coming soon. we will roll out gemini image descriptions later after we ship the private beta.
          </div>
          <div className="folder-settings-block settings-links-block">
            <div className="cognitive-header-row" style={{ alignItems: 'center' }}>
              <div className="cognitive-header-copy" style={{ flex: 1 }}>
                <div className="cognitive-state-pill cognitive-state-idle">
                  coming soon
                </div>
                <div className="settings-links-copy">
                  photos are indexed by filename and OCR text only in this beta. no image data is sent to any server.
                </div>
              </div>
              <button
                type="button"
                className="folder-add-btn"
                disabled
                style={{ flexShrink: 0, marginLeft: 12, opacity: 0.55 }}
                title="Coming soon"
              >
                enable
              </button>
            </div>
            {geminiVisionEnabled ? (
              <div className="settings-vision-warn">
                photo descriptions are currently locked. restart the app to reset this beta build.
              </div>
            ) : (
              <div className="settings-vision-warn">
                locked for now. we will add a consent flow and clear pricing before turning it on.
              </div>
            )}
          </div>
        </>
      ) : null}

      <div className="section-title">currently watching</div>
      <div className="folder-settings-block">
        {watchedFolders.map((folder) => (
          <div key={folder.path} className="watched-folder-row">
            <div className="watched-folder-left">
              <span className="watched-folder-icon">
                <FolderGlyph />
              </span>
              <span className="watched-folder-path">{relativeHomePath(folder.path)}</span>
            </div>
            {folder.is_default ? (
              <span className="watched-folder-meta">default</span>
            ) : (
              <button
                type="button"
                className="watched-folder-action"
                onClick={() => onRemoveFolder(folder)}
              >
                remove
              </button>
            )}
          </div>
        ))}

        <button type="button" className="watched-folder-add" onClick={onOpenPicker}>
          + add folder
        </button>
      </div>

      {status.is_scanning ? (
        <div className="folder-progress-copy">
          {status.scan_phase?.phase === 'rebuilding'
            ? 'rebuilding search index from stored memories…'
            : status.scan_phase?.phase === 'checking'
            ? `checking ${formatNumber(status.scan_phase?.checked || 0)} / ${formatNumber(status.scan_phase?.total || 0)} files…`
            : buildScanProgressLabel(status)}
        </div>
      ) : null}

      <div className="section-title">
        Excluded Folders
        {excludedDirs.length > 0 && (
          <span className="settings-excluded-count">{excludedDirs.length} excluded</span>
        )}
      </div>
      <div className="section-caption">
        files inside these folders are never indexed — useful for blocking datasets, build outputs, or any directory with junk files.
      </div>
      <div className="folder-settings-block">
        {excludedDirs.length === 0 ? (
          <div className="empty-copy">no folders excluded</div>
        ) : (
          excludedDirs.map((dir) => (
            <div key={dir} className="watched-folder-row">
              <div className="watched-folder-left">
                <span className="watched-folder-icon"></span>
                <span className="watched-folder-path">{dir}</span>
              </div>
              <button
                type="button"
                className="folder-remove-btn"
                onClick={() => {
                  const next = excludedDirs.filter((d) => d !== dir)
                  setExcludedDirs(next)
                  void (async () => {
                    setExcludeSaving(true)
                    try { await onSaveExcludedDirs?.(next) } finally { setExcludeSaving(false) }
                  })()
                }}
              >
                remove
              </button>
            </div>
          ))
        )}
        <div className="custom-path-row">
          <input
            className="custom-path-input"
            placeholder="/Users/you/datasets  or  ~/Downloads/opencv"
            value={excludeInput}
            onChange={(e) => setExcludeInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && excludeInput.trim()) {
                const next = [...new Set([...excludedDirs, excludeInput.trim()])]
                setExcludedDirs(next)
                setExcludeInput('')
                void (async () => {
                  setExcludeSaving(true)
                  try { await onSaveExcludedDirs?.(next) } finally { setExcludeSaving(false) }
                })()
              }
            }}
          />
          <button
            type="button"
            className="folder-add-btn"
            disabled={!excludeInput.trim() || excludeSaving}
            onClick={() => {
              const next = [...new Set([...excludedDirs, excludeInput.trim()])]
              setExcludedDirs(next)
              setExcludeInput('')
              void (async () => {
                setExcludeSaving(true)
                try { await onSaveExcludedDirs?.(next) } finally { setExcludeSaving(false) }
              })()
            }}
          >
            {excludeSaving ? 'saving…' : 'exclude'}
          </button>
        </div>
      </div>

      <div className="section-title">discover more</div>
      <div className="section-caption">
        quick estimates from readable folders on this mac. nothing is indexed until you choose to watch it.
      </div>
      {discoveryLoading ? (
        <div className="folder-discovery-skeleton">
          <div className="folder-skel-row" />
          <div className="folder-skel-row" />
          <div className="folder-skel-row" />
        </div>
      ) : (
        <div className="folder-settings-block">
          {DISCOVERY_GROUPS.map((group) => (
            <DiscoveryGroup
              key={group.id}
              group={group}
              entries={Array.isArray(discovery[group.id]) ? discovery[group.id] : []}
              isOpen={Boolean(openGroups[group.id])}
              onToggle={() =>
                setOpenGroups((current) => ({
                  ...current,
                  [group.id]: !current[group.id],
                }))
              }
              onAdd={onAddFolder}
              addingPaths={addingPaths}
              watchedPaths={watchedPaths}
            />
          ))}
          {!discoveredCount ? (
            <div className="empty-copy">no additional folders discovered yet</div>
          ) : null}
        </div>
      )}

      <div className="section-title">custom path</div>
      <div className="folder-settings-block">
        <div className="custom-path-row">
          <input
            className={`custom-path-input${customPathError ? ' error' : ''}`}
            value={customPath}
            onChange={(event) => onCustomPathChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                onCustomPathSubmit()
              }
            }}
            placeholder="enter any path..."
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
          <button type="button" className="folder-add-btn" onClick={onCustomPathSubmit}>
            add
          </button>
        </div>
        {customPathError ? <div className="custom-path-error">{customPathError}</div> : null}
      </div>

      <div className="section-title">cognitive layer</div>
      <div className="section-caption">
        act-r activation, association links, and frame memory that will drive the next search ranking pass.
      </div>
      <div className="folder-settings-block settings-links-block cognitive-settings-block">
        <div className="cognitive-header-row">
          <div className="cognitive-header-copy">
            <div className={`cognitive-state-pill cognitive-state-${cognitiveBootstrap.state}`}>
              {buildCognitiveStateLabel(cognitive)}
            </div>
            <div className="settings-links-copy">{cognitiveSummary}</div>
            {cognitive.db_path ? (
              <div className="cognitive-db-path">{relativeHomePath(cognitive.db_path)}</div>
            ) : null}
          </div>
          <div className="cognitive-actions">
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onBootstrapCognitive?.({ dryRun: true, reset: false })}
              disabled={cognitiveBusy}
            >
              dry run
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onBootstrapCognitive?.({ dryRun: false, reset: false })}
              disabled={cognitiveBusy}
            >
              bootstrap
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onBootstrapCognitive?.({ dryRun: false, reset: true })}
              disabled={cognitiveBusy}
            >
              rebuild
            </button>
          </div>
        </div>

        <div className="cognitive-stats-grid">
          <div className="cognitive-stat-card">
            <div className="settings-account-label">activation events</div>
            <div className="cognitive-stat-value">{formatNumber(cognitive.activation_events)}</div>
          </div>
          <div className="cognitive-stat-card">
            <div className="settings-account-label">entity links</div>
            <div className="cognitive-stat-value">{formatNumber(cognitive.entity_edges)}</div>
          </div>
          <div className="cognitive-stat-card">
            <div className="settings-account-label">frame instances</div>
            <div className="cognitive-stat-value">{formatNumber(cognitive.frame_instances)}</div>
          </div>
        </div>

        {cognitiveBootstrap.state === 'running' ? (
          <div className="cognitive-progress-shell">
            <div className="cognitive-progress-track">
              <div
                className="cognitive-progress-fill"
                style={{
                  width: `${Math.max(6, Math.min(cognitiveBootstrap.percent || 0, 100))}%`,
                }}
              />
            </div>
            <div className="folder-progress-copy">
              {cognitiveBootstrap.total
                ? `${formatNumber(cognitiveBootstrap.processed)} / ${formatNumber(cognitiveBootstrap.total)} · ${Math.round(cognitiveBootstrap.percent || 0)}%`
                : 'preparing cognitive bootstrap…'}
            </div>
          </div>
        ) : null}

        {cognitiveBootstrap.state === 'completed' && cognitiveBootstrap.summary.items ? (
          <div className="cognitive-run-note">
            {cognitiveBootstrap.dry_run
              ? `last dry run scanned ${formatNumber(cognitiveBootstrap.summary.items)} memories without changing the database`
              : `last run seeded ${formatNumber(cognitiveBootstrap.summary.items)} memories, ${formatNumber(cognitiveBootstrap.summary.entity_edges)} links, and ${formatNumber(cognitiveBootstrap.summary.frame_instances)} frames`}
          </div>
        ) : null}

        {cognitiveBootstrap.state === 'error' ? (
          <div className="custom-path-error">
            {cognitiveBootstrap.error || cognitiveBootstrap.message || 'cognitive bootstrap failed'}
          </div>
        ) : null}
      </div>

      <div className="section-title">gmail memory</div>
      <div className="section-caption">
        connect Gmail with read-only access so higher-signal inbox and sent emails can join the same search, graph, and cognitive layers as files without taking over normal file search. first sync now stays bounded to the last {gmailBootstrapDays} days unless you explicitly backfill more. supported attachments are indexed as separate memories without saving raw attachment files permanently.
      </div>
      <div className="folder-settings-block settings-links-block cognitive-settings-block">
        <div className="cognitive-header-row">
          <div className="cognitive-header-copy">
            <div className={`cognitive-state-pill cognitive-state-${gmailOauthBusy || gmailSyncBusy ? 'running' : gmail.oauth.state === 'error' || gmail.sync.state === 'error' ? 'error' : 'completed'}`}>
              {buildGmailStateLabel(gmail)}
            </div>
            <div className="settings-links-copy">{gmailSummary}</div>
            {gmail.account_email ? (
              <div className="cognitive-db-path">{gmail.account_email}</div>
            ) : gmail.client_secrets_path ? (
              <div className="cognitive-db-path">{relativeHomePath(gmail.client_secrets_path)}</div>
            ) : null}
          </div>
          <div className="cognitive-actions">
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onConnectGmail?.()}
              disabled={gmailBusy || !gmail.configured || !gmail.oauthlib_available}
            >
              {gmail.connected ? 'reconnect' : 'connect'}
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onSyncGmail?.({ fullSync: false })}
              disabled={gmailBusy || !gmail.connected}
            >
              sync latest
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onSyncGmail?.({ historicalDays: gmailHistoricalDays[0] || 30 })}
              disabled={gmailBusy || !gmail.connected}
            >
              import {gmailHistoricalDays[0] || 30}d
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onSyncGmail?.({ historicalDays: gmailHistoricalDays[1] || 90 })}
              disabled={gmailBusy || !gmail.connected}
            >
              import {gmailHistoricalDays[1] || 90}d
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onSyncGmail?.({ historicalDays: gmailHistoricalDays[0] || 30, requireAttachments: true })}
              disabled={gmailBusy || !gmail.connected}
            >
              attachments only
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onStopGmailSync?.()}
              disabled={!gmailSyncBusy}
            >
              stop sync
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onCleanupGmail?.()}
              disabled={gmailBusy || !gmail.indexed_items}
            >
              clear imported
            </button>
            <button
              type="button"
              className="folder-add-btn"
              onClick={() => void onDisconnectGmail?.()}
              disabled={gmailBusy || !gmail.connected}
            >
              disconnect
            </button>
          </div>
        </div>

        <div className="cognitive-stats-grid">
          <div className="cognitive-stat-card">
            <div className="settings-account-label">gmail memories</div>
            <div className="cognitive-stat-value">{formatNumber(gmail.indexed_items)}</div>
          </div>
          <div className="cognitive-stat-card">
            <div className="settings-account-label">last history id</div>
            <div className="cognitive-stat-value">{gmail.last_history_id ? String(gmail.last_history_id).slice(-8) : 'none'}</div>
          </div>
          <div className="cognitive-stat-card">
            <div className="settings-account-label">last sync</div>
            <div className="cognitive-stat-value">{gmail.last_sync_at ? relativeTime(gmail.last_sync_at) : 'never'}</div>
          </div>
        </div>

        {gmailSyncBusy ? (
          <div className="cognitive-progress-shell">
            <div className="cognitive-progress-track">
              <div
                className="cognitive-progress-fill"
                style={{
                  width: `${Math.max(6, Math.min(gmail.sync.percent || 0, 100))}%`,
                }}
              />
            </div>
            <div className="folder-progress-copy">
              {gmail.sync.total
                ? `${formatNumber(gmail.sync.processed)} / ${formatNumber(gmail.sync.total)} · ${Math.round(gmail.sync.percent || 0)}%`
                : gmail.sync.message || 'preparing gmail sync…'}
            </div>
          </div>
        ) : null}

        {gmail.sync.state === 'completed' && ((gmail.sync.summary?.processed || 0) || (gmail.sync.summary?.skipped || 0)) ? (
          <div className="cognitive-run-note">
            {`last gmail sync ${gmail.sync.summary.mode || 'incremental'}${gmail.sync.summary.historical_days ? ` · ${gmail.sync.summary.historical_days}d` : ''}${gmail.sync.summary.require_attachments ? ' · attachments only' : ''} · ${formatNumber(gmail.sync.summary.processed || 0)} emails processed · ${formatNumber(gmail.sync.summary.created || 0)} new · ${formatNumber(gmail.sync.summary.updated || 0)} updated · ${formatNumber(gmail.sync.summary.skipped || 0)} filtered out · ${formatNumber(gmail.sync.summary.attachments_processed || 0)} attachments indexed`}
          </div>
        ) : null}

        <div className="cognitive-run-note">
          safe policy: inbox, sent, important, and starred mail only. promotions, social, updates, forums, drafts, spam, and bulk-list mail stay out by default. attachments are capped at {gmail.policy.attachment_max_bytes ? `${Math.round(gmail.policy.attachment_max_bytes / (1024 * 1024))} MB` : 'a safe size'} each.
        </div>

        {gmail.sync.state === 'cancelled' ? (
          <div className="cognitive-run-note">
            gmail sync stopped. already imported Gmail memories stay available until you clear them.
          </div>
        ) : null}

        {!gmail.configured ? (
          <div className="custom-path-error">
            add your Google OAuth desktop client file at `~/.recurdo/client_secrets.json` to enable Gmail connection.
          </div>
        ) : null}

        {gmail.configured && !gmail.oauthlib_available ? (
          <div className="custom-path-error">
            google-auth-oauthlib is missing in this environment, so Gmail auth cannot start yet.
          </div>
        ) : null}

        {gmail.oauth.state === 'error' || gmail.sync.state === 'error' ? (
          <div className="custom-path-error">
            {gmail.sync.error || gmail.sync.message || gmail.oauth.error || gmail.oauth.message || 'gmail needs attention'}
          </div>
        ) : null}
      </div>

      <UpdatesSection />

      <div className="section-title">account</div>
      <div className="folder-settings-block settings-links-block">
        <div className="settings-links-copy">
          manage the account connected to this Mac. sign out here if you want to switch to a different email.
        </div>
        <div className="settings-account-grid">
          <div className="settings-account-item">
            <div className="settings-account-label">signed in as</div>
            <div className="settings-account-value">{signedInEmail}</div>
          </div>
          <div className="settings-account-item">
            <div className="settings-account-label">access</div>
            <div className="settings-account-value">{accessSummary}</div>
          </div>
          <div className="settings-account-item settings-account-item-wide">
            <div className="settings-account-label">verification</div>
            <div className="settings-account-value">{accessVerification}</div>
          </div>
        </div>
        <div className="settings-account-actions">
          <button type="button" className="folder-add-btn" onClick={() => void onSignOut?.()}>
            sign out
          </button>
        </div>
      </div>

      <div className="section-title">links</div>
      <div className="folder-settings-block settings-links-block">
        <div className="settings-links-copy">
          need more context? open the main site, privacy notes, or the beta page in your browser.
        </div>
        <div className="settings-links-grid">
          <button
            type="button"
            className="settings-link-card"
            onClick={() => void openExternalLink(WEBSITE_URL)}
          >
            <span className="settings-link-label">website</span>
            <span className="settings-link-note">see the product page and latest beta story</span>
            <span className="settings-link-arrow">↗</span>
          </button>
          <button
            type="button"
            className="settings-link-card"
            onClick={() => void openExternalLink(PRIVACY_URL)}
          >
            <span className="settings-link-label">privacy</span>
            <span className="settings-link-note">read how local storage and access work</span>
            <span className="settings-link-arrow">↗</span>
          </button>
          <button
            type="button"
            className="settings-link-card"
            onClick={() => void openExternalLink(TERMS_URL)}
          >
            <span className="settings-link-label">terms</span>
            <span className="settings-link-note">view the current desktop beta terms</span>
            <span className="settings-link-arrow">↗</span>
          </button>
          <button
            type="button"
            className="settings-link-card"
            onClick={() => void openExternalLink(WAITLIST_URL)}
          >
            <span className="settings-link-label">beta updates</span>
            <span className="settings-link-note">jump to the site if you need the waitlist or access page</span>
            <span className="settings-link-arrow">↗</span>
          </button>
        </div>
      </div>

      <div className="section-title">Privacy</div>
      <div className="folder-settings-block settings-links-block">
        <div className="settings-privacy-card">
          <div className="settings-privacy-row">
            <span className="settings-privacy-icon">🔒</span>
            <div className="settings-privacy-text">
              <div className="settings-privacy-heading">What gets sent to Gemini</div>
              <div className="settings-privacy-body">
                When you ask a question, relevant excerpts from your files (up to 1,200 characters per file) are sent to Google Gemini to generate your answer. Your file paths, full file contents, and personal metadata are never sent. All indexing and embeddings happen locally on your Mac.
              </div>
            </div>
          </div>
          {geminiVisionEnabled && (
            <div className="settings-privacy-row settings-privacy-warn">
              <span className="settings-privacy-icon">📷</span>
              <div className="settings-privacy-text">
                <div className="settings-privacy-heading">Photo descriptions are ON</div>
                <div className="settings-privacy-body">
                  When enabled, image files from your indexed folders are sent to Google's servers for visual analysis. This beta build ships with photo descriptions locked off by default.
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function MainApp({ session, daysRemaining, forceOnboarding = false, onOnboardingComplete, onSignOut }) {
  const [page, setPage] = useState('overview')
  const [status, setStatus] = useState(() => normalizeStatusSnapshot())
  const [health, setHealth] = useState({
    status: 'ok',
    text_model: 'nomic-embed-text-v1.5',
    ocr_available: false,
    enrichment_available: false,
    uptime_s: 0,
  })
  const [items, setItems] = useState([])
  const [watchedFolders, setWatchedFolders] = useState([])
  const [discovery, setDiscovery] = useState({
    standard: [],
    icloud: [],
    external: [],
    cloud: [],
    suggested: [],
    custom: [],
  })
  const [discoveryLoading, setDiscoveryLoading] = useState(false)
  const [addingPaths, setAddingPaths] = useState(new Set())
  const [customPath, setCustomPath] = useState('')
  const [customPathError, setCustomPathError] = useState('')
  const [, setActiveFolderScan] = useState('')
  const [tooltip, setTooltip] = useState(null)
  const [toast, setToast] = useState('')
  const [healthBanner, setHealthBanner] = useState(null)
  const [healthBannerExpanded, setHealthBannerExpanded] = useState(false)
  const [resolvedName, setResolvedName] = useState('')
  const [recentIndexedItems, setRecentIndexedItems] = useState([])
  const [wsStatus, setWsStatus] = useState('connecting')
  const [wsFallbackPolling, setWsFallbackPolling] = useState(false)
  const [proactiveContext, setProactiveContext] = useState({ rules: [], context: {} })
  const [contextLoading, setContextLoading] = useState(true)
  const toastTimeoutRef = useRef(null)
  const healthBannerTimeoutRef = useRef(null)
  const wsFallbackTimeoutRef = useRef(null)
  const headers = useMemo(
    () => ({
      Authorization: `Bearer ${session?.token || ''}`,
      'Content-Type': 'application/json',
    }),
    [session?.token],
  )

  useEffect(() => {
    if (!session?.token) return undefined
    let cancelled = false
    const loadAppearance = async () => {
      try {
        const backend = await getBackendHttpUrl()
        const response = await fetch(`${backend}/user/settings`, { headers })
        if (!response.ok) return
        const settings = await response.json()
        if (!cancelled && settings?.accent_color) {
          storeAndApplyAccent(settings.accent_color)
        }
      } catch {
        // Accent falls back to the local setting applied during bootstrap.
      }
    }
    void loadAppearance()
    return () => { cancelled = true }
  }, [headers, session?.token])

  const showToast = useCallback((message) => {
    setToast(message)
    if (toastTimeoutRef.current) {
      window.clearTimeout(toastTimeoutRef.current)
    }
    toastTimeoutRef.current = window.setTimeout(() => {
      setToast('')
    }, 2200)
  }, [])

  const dismissHealthBanner = useCallback(() => {
    setHealthBanner(null)
    setHealthBannerExpanded(false)
    if (healthBannerTimeoutRef.current) {
      window.clearTimeout(healthBannerTimeoutRef.current)
      healthBannerTimeoutRef.current = null
    }
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      const nextStatus = await getStatus(headers)
      const normalized = normalizeStatusSnapshot(nextStatus)
      setStatus(normalized)
      return normalized
    } catch (error) {
      console.warn('Main status fetch failed', error)
      return null
    }
  }, [headers])

  const refreshHealth = useCallback(async () => {
    try {
      const nextHealth = await getHealth()
      setHealth(nextHealth)
      return nextHealth
    } catch (error) {
      console.warn('Main health fetch failed', error)
      return null
    }
  }, [])

  const refreshContext = useCallback(async (options = {}) => {
    try {
      const nextContext = await getContext(headers, options)
      setProactiveContext(
        nextContext && typeof nextContext === 'object'
          ? nextContext
          : { rules: [], context: {} },
      )
      return nextContext
    } catch (error) {
      console.warn('Context fetch failed', error)
      return null
    } finally {
      setContextLoading(false)
    }
  }, [headers])

  const refreshItems = useCallback(async () => {
    try {
      const response = await getItems(1, 100, headers)
      setItems(Array.isArray(response.items) ? response.items : [])
      return response
    } catch (error) {
      console.warn('Main items fetch failed', error)
      return null
    }
  }, [headers])

  const refreshWatchedFolders = useCallback(async () => {
    try {
      const rows = await getWatchedFolders(headers)
      setWatchedFolders(Array.isArray(rows) ? rows : [])
      return rows
    } catch (error) {
      console.warn('Watched folders fetch failed', error)
      return null
    }
  }, [headers])

  const refreshDiscovery = useCallback(async () => {
    setDiscoveryLoading(true)
    try {
      const nextDiscovery = await discoverFolders(headers)
      setDiscovery(
        nextDiscovery && typeof nextDiscovery === 'object'
          ? nextDiscovery
          : { standard: [], icloud: [], external: [], cloud: [], suggested: [], custom: [] },
      )
      return nextDiscovery
    } catch (error) {
      console.warn('Folder discovery failed', error)
      return null
    } finally {
      setDiscoveryLoading(false)
    }
  }, [headers])

  const handleWebSocketMessage = useCallback((event) => {
    if (event.type === 'cognitive_bootstrap') {
      setStatus((current) => {
        const currentCognitive = normalizeCognitiveStatusSnapshot(current.cognitive || {})
        const nextBootstrap = normalizeCognitiveBootstrapSnapshot(event)
        const nextSummary = nextBootstrap.summary
        const completedWrite = nextBootstrap.state === 'completed' && !nextBootstrap.dry_run

        return normalizeStatusSnapshot({
          ...current,
          cognitive: {
            ...currentCognitive,
            ready: currentCognitive.ready || completedWrite,
            activation_events:
              completedWrite && nextSummary.activation_events
                ? nextSummary.activation_events
                : currentCognitive.activation_events,
            entity_edges:
              completedWrite && nextSummary.entity_edges
                ? nextSummary.entity_edges
                : currentCognitive.entity_edges,
            frame_instances:
              completedWrite && nextSummary.frame_instances
                ? nextSummary.frame_instances
                : currentCognitive.frame_instances,
            bootstrap: nextBootstrap,
          },
        })
      })

      if (event.state === 'completed') {
        showToast(event.dry_run ? 'cognitive dry run complete' : 'cognitive layer updated')
        void Promise.all([refreshStatus(), refreshHealth()])
      }

      if (event.state === 'error') {
        showToast(event.message || 'cognitive bootstrap failed')
        void Promise.all([refreshStatus(), refreshHealth()])
      }
    }

    if (event.type === 'gmail_oauth' || event.type === 'gmail_sync') {
      const kind = event.type === 'gmail_oauth' ? 'oauth' : 'sync'
      const eventSummary = event.summary && typeof event.summary === 'object' ? event.summary : {}

      setStatus((current) => {
        const currentGmail = normalizeGmailStatusSnapshot(current.gmail || {})
        const nextJob = normalizeGmailJobSnapshot(event)

        return normalizeStatusSnapshot({
          ...current,
          gmail: {
            ...currentGmail,
            account_email:
              kind === 'oauth' && eventSummary.account_email
                ? String(eventSummary.account_email)
                : kind === 'sync' && eventSummary.account_email
                ? String(eventSummary.account_email)
                : currentGmail.account_email,
            connected:
              kind === 'oauth' && event.state === 'completed'
                ? true
                : kind === 'oauth' && event.state === 'error'
                ? false
                : currentGmail.connected,
            last_history_id:
              kind === 'oauth' && eventSummary.history_id
                ? String(eventSummary.history_id)
                : kind === 'sync' && eventSummary.last_history_id
                ? String(eventSummary.last_history_id)
                : currentGmail.last_history_id,
            last_sync_at:
              kind === 'sync' && eventSummary.last_sync_at
                ? eventSummary.last_sync_at
                : currentGmail.last_sync_at,
            indexed_items:
              kind === 'sync' && Object.prototype.hasOwnProperty.call(eventSummary, 'indexed_items')
                ? Number(eventSummary.indexed_items ?? 0) || 0
                : currentGmail.indexed_items,
            last_summary:
              kind === 'sync' && Object.keys(eventSummary).length
                ? eventSummary
                : currentGmail.last_summary,
            oauth: kind === 'oauth' ? nextJob : currentGmail.oauth,
            sync: kind === 'sync' ? nextJob : currentGmail.sync,
          },
        })
      })

      if (event.type === 'gmail_oauth' && event.state === 'completed') {
        showToast('gmail connected')
        void Promise.all([refreshStatus(), refreshHealth()])
      }

      if (event.type === 'gmail_oauth' && event.state === 'error') {
        showToast(event.message || 'gmail authentication failed')
        void Promise.all([refreshStatus(), refreshHealth()])
      }

      if (event.type === 'gmail_sync' && event.state === 'completed') {
        showToast('gmail sync complete')
        void Promise.all([refreshStatus(), refreshHealth(), refreshItems(), refreshContext()])
      }

      if (event.type === 'gmail_sync' && event.state === 'error') {
        showToast(event.message || 'gmail sync failed')
        void Promise.all([refreshStatus(), refreshHealth(), refreshItems(), refreshContext()])
      }

      if (event.type === 'gmail_sync' && event.state === 'cancelled') {
        showToast(event.message || 'gmail sync stopped')
        void Promise.all([refreshStatus(), refreshHealth(), refreshItems(), refreshContext()])
      }
    }

    if (event.type === 'file_indexed') {
      setStatus((current) => normalizeStatusSnapshot({
        ...current,
        total_items: current.unique_memories + 1,
        unique_memories: current.unique_memories + 1,
      }))
      setRecentIndexedItems((current) => [
        {
          kind: 'indexed',
          title: event.title || '',
          file_type: event.file_type || '',
          handler_type: event.handler_type || '',
        },
        ...current,
      ].slice(0, 6))
      void refreshItems()
    }

    if (event.type === 'file_duplicate') {
      setStatus((current) => normalizeStatusSnapshot({
        ...current,
        duplicates_skipped: current.duplicates_skipped + 1,
      }))
      setRecentIndexedItems((current) => [
        {
          kind: 'duplicate',
          title: event.title || event.filename || '',
          file_type: event.file_type || '',
          handler_type: event.handler_type || '',
        },
        ...current,
      ].slice(0, 6))
    }

    if (event.type === 'startup_health' && event.status === 'degraded') {
      const nextBanner = {
        message: buildStartupHealthMessage(Array.isArray(event.actions) ? event.actions : []),
        issues: Array.isArray(event.issues) ? event.issues : [],
        actions: Array.isArray(event.actions) ? event.actions : [],
      }
      setHealthBanner(nextBanner)
      setHealthBannerExpanded(false)
      if (healthBannerTimeoutRef.current) {
        window.clearTimeout(healthBannerTimeoutRef.current)
      }
      healthBannerTimeoutRef.current = window.setTimeout(() => {
        setHealthBanner(null)
        setHealthBannerExpanded(false)
        healthBannerTimeoutRef.current = null
      }, 8000)
    }

    if (event.type === 'scan_progress') {
      setStatus((current) => normalizeStatusSnapshot({
        ...current,
        is_scanning: true,
        scan_phase: {
          ...current.scan_phase,
          phase: 'indexing',
          message: event.message || current.scan_phase?.message || '',
          total: event.total ?? current.scan_phase?.total ?? 0,
          checked: event.indexed ?? current.scan_phase?.checked ?? 0,
          new_found: event.new_found ?? current.scan_phase?.new_found ?? event.total ?? 0,
        },
        scan_progress: {
          processed: event.processed ?? event.indexed ?? current.scan_progress?.processed ?? 0,
          total: event.total ?? current.scan_progress?.total ?? 0,
          remaining:
            event.remaining
            ?? Math.max(
              Number(event.total ?? current.scan_progress?.total ?? 0)
              - Number(event.processed ?? event.indexed ?? current.scan_progress?.processed ?? 0),
              0,
            ),
          percent: event.percent ?? current.scan_progress?.percent ?? 0,
        },
      }))
    }

    if (event.type === 'scan_phase') {
      setStatus((current) => normalizeStatusSnapshot({
        ...current,
        is_scanning: true,
        scan_phase: {
          phase: event.phase || current.scan_phase?.phase || '',
          message: event.message || current.scan_phase?.message || '',
          found: event.found ?? current.scan_phase?.found ?? 0,
          checked: event.checked ?? current.scan_phase?.checked ?? 0,
          total: event.total ?? current.scan_phase?.total ?? 0,
          new_found: event.new_found ?? current.scan_phase?.new_found ?? 0,
        },
      }))
    }

    if (event.type === 'scan_complete') {
      setStatus((current) => normalizeStatusSnapshot({
        ...current,
        is_scanning: false,
        scan_phase: {
          ...current.scan_phase,
          phase: 'complete',
          message: `stored ${event.indexed ?? current.scan_progress?.processed ?? 0} memories`,
          checked: event.total ?? current.scan_phase?.checked ?? 0,
          total: event.total ?? current.scan_phase?.total ?? 0,
          new_found: event.indexed ?? current.scan_phase?.new_found ?? 0,
        },
        scan_progress: {
          processed: event.processed ?? event.indexed ?? current.scan_progress?.processed ?? 0,
          total: event.total ?? current.scan_progress?.total ?? 0,
          remaining: event.remaining ?? 0,
          percent: event.percent ?? current.scan_progress?.percent ?? 100,
        },
      }))
      setAddingPaths(new Set())
      setActiveFolderScan('')
      void Promise.all([refreshStatus(), refreshItems(), refreshDiscovery(), refreshContext()])
    }
  }, [refreshContext, refreshDiscovery, refreshHealth, refreshItems, refreshStatus, showToast])

  useEffect(() => {
    const bootstrapId = window.setTimeout(() => {
      void Promise.all([
        refreshStatus(),
        refreshHealth(),
        refreshContext(),
        refreshItems(),
        refreshWatchedFolders(),
        refreshDiscovery(),
      ])
    }, 0)

    return () => {
      window.clearTimeout(bootstrapId)
      if (toastTimeoutRef.current) {
        window.clearTimeout(toastTimeoutRef.current)
      }
      if (healthBannerTimeoutRef.current) {
        window.clearTimeout(healthBannerTimeoutRef.current)
      }
      if (wsFallbackTimeoutRef.current) {
        window.clearTimeout(wsFallbackTimeoutRef.current)
      }
    }
  }, [refreshContext, refreshDiscovery, refreshHealth, refreshItems, refreshStatus, refreshWatchedFolders])

  const cognitiveBootstrapRunning = status.cognitive?.bootstrap?.state === 'running'
  const gmailTaskRunning = status.gmail?.oauth?.state === 'running' || status.gmail?.sync?.state === 'running'
  const showHealthReconnect = health.status === 'degraded' && Number(health.uptime_s || 0) >= 10

  const statusPollMs = status.is_scanning || cognitiveBootstrapRunning || gmailTaskRunning || wsFallbackPolling
    ? STATUS_POLL_MS_SCANNING
    : STATUS_POLL_MS_IDLE

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      if (document.visibilityState === 'hidden') {
        return
      }
      void refreshStatus()
      void refreshHealth()
      if (page === 'overview') {
        void refreshContext()
      }
    }, statusPollMs)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [page, refreshContext, refreshHealth, refreshStatus, statusPollMs])

  useEffect(() => {
    const websocket = connectWebSocket(handleWebSocketMessage, setWsStatus)
    return () => {
      websocket.destroy()
    }
  }, [handleWebSocketMessage])

  useEffect(() => {
    if (wsStatus === 'connected') {
      setWsFallbackPolling(false)
      if (wsFallbackTimeoutRef.current) {
        window.clearTimeout(wsFallbackTimeoutRef.current)
        wsFallbackTimeoutRef.current = null
      }
      return
    }

    if (!wsFallbackTimeoutRef.current) {
      wsFallbackTimeoutRef.current = window.setTimeout(() => {
        setWsFallbackPolling(true)
        wsFallbackTimeoutRef.current = null
      }, WS_POLL_FALLBACK_AFTER_MS)
    }
  }, [wsStatus])

  const showOnboarding = useMemo(
    () => forceOnboarding || FORCE_ONBOARDING,
    [forceOnboarding],
  )

  const totalItemsLabel = useMemo(
    () => formatNumber(status.unique_memories),
    [status.unique_memories],
  )
  const filesWatchedLabel = useMemo(
    () => formatNumber(status.files_watched),
    [status.files_watched],
  )
  const duplicatesSkippedLabel = useMemo(
    () => formatNumber(status.duplicates_skipped),
    [status.duplicates_skipped],
  )
  const wsMeta = useMemo(
    () => websocketStatusMeta(wsStatus),
    [wsStatus],
  )
  const scanProgressLabel = buildScanProgressLabel(status)
  const watchedFilesContextLabel = useMemo(
    () => (status.files_watched ? `${filesWatchedLabel} files watched` : ''),
    [filesWatchedLabel, status.files_watched],
  )
  const watchedFolderNamesLabel = useMemo(() => {
    const labels = watchedFolders
      .map((folder) => folder.label || normalizePath(folder.path).split('/').filter(Boolean).pop() || folder.path)
      .filter(Boolean)
    if (!labels.length) {
      return 'no folders yet'
    }
    if (labels.length <= 3) {
      return labels.join(' / ')
    }
    return `${labels.slice(0, 3).join(' / ')} +${labels.length - 3}`
  }, [watchedFolders])
  const memoryTooltip = useMemo(
    () => `${totalItemsLabel} unique memories indexed · ${filesWatchedLabel} files watched`,
    [filesWatchedLabel, totalItemsLabel],
  )
  const statusPillLabel = useMemo(
    () => buildStatusPillLabel(status),
    [status],
  )
  const greetingSession = useMemo(
    () => ({
      ...session,
      user: {
        ...session?.user,
        name: resolvedName || session?.user?.name || '',
      },
    }),
    [resolvedName, session],
  )
  const accessState = useMemo(
    () => `${accessLabel(session)} · ${accessVerificationLabel(session)}`,
    [session],
  )
  const cognitiveSummaryLabel = useMemo(
    () => buildCognitiveSummaryLabel(status.cognitive),
    [status.cognitive],
  )
  const cognitiveStateLabel = useMemo(
    () => buildCognitiveStateLabel(status.cognitive),
    [status.cognitive],
  )
  const handleOnboardingStartIndexing = useCallback(async (selectedFolders) => {
    const chosen = Array.isArray(selectedFolders) ? selectedFolders : []
    const chosenPaths = new Set(chosen.map((folder) => normalizePath(folder.path)))
    const currentFolders = Array.isArray(watchedFolders) ? watchedFolders : []

    const foldersToRemove = currentFolders.filter(
      (folder) => !chosenPaths.has(normalizePath(folder.path)),
    )
    const foldersToAdd = chosen.filter(
      (folder) => !currentFolders.some((current) => normalizePath(current.path) === normalizePath(folder.path)),
    )

    const addedPaths = []

    try {
      for (const folder of foldersToAdd) {
        await addWatchedFolder(folder.path, folder.label, headers, { startScan: false })
        addedPaths.push(folder.path)
      }

      await Promise.all(
        foldersToRemove.map((folder) => removeWatchedFolder(folder.path, headers)),
      )

      await Promise.all([refreshWatchedFolders(), refreshDiscovery()])

      if (chosen.length) {
        setRecentIndexedItems([])
        await triggerScan(headers)
      }

      await Promise.all([refreshStatus(), refreshItems(), refreshHealth(), refreshWatchedFolders(), refreshContext()])
    } catch (error) {
      await Promise.allSettled(
        addedPaths.map((path) => removeWatchedFolder(path, headers)),
      )
      await Promise.allSettled([refreshWatchedFolders(), refreshDiscovery(), refreshStatus(), refreshContext()])
      throw error
    }
  }, [headers, refreshContext, refreshDiscovery, refreshHealth, refreshItems, refreshStatus, refreshWatchedFolders, watchedFolders])

  const handleOnboardingComplete = useCallback(async () => {
    await Promise.all([
      refreshStatus(),
      refreshHealth(),
      refreshContext(),
      refreshItems(),
      refreshWatchedFolders(),
      refreshDiscovery(),
    ])
    await onOnboardingComplete?.()
  }, [onOnboardingComplete, refreshContext, refreshDiscovery, refreshHealth, refreshItems, refreshStatus, refreshWatchedFolders])

  const handleTooltipEnter = useCallback((label, event) => {
    const rect = event.currentTarget.getBoundingClientRect()
    setTooltip({
      label,
      left: rect.right + 8,
      top: rect.top + rect.height / 2,
    })
  }, [])

  const handleTooltipLeave = useCallback(() => {
    setTooltip(null)
  }, [])

  const addFolderWithChecks = useCallback(async (entry, category = '') => {
    const path = String(entry.path || '')
    const label = String(entry.label || path.split('/').pop() || path)
    const fileCount = Number(entry.file_count || 0)
    const isExternal = category === 'external' || path.startsWith('/Volumes/')

    if (isExternal && fileCount > 0) {
      const timeEstimate = formatDuration(fileCount * 0.1)
      const storageEstimate = formatStorage(fileCount * 50 * 1024)
      const okay = window.confirm(
        `This folder has ${fileCount.toLocaleString()} files. Indexing will take ~${timeEstimate} and use ~${storageEstimate}. Continue?`,
      )
      if (!okay) {
        return false
      }
    }

    setAddingPaths((current) => new Set([...current, path]))
    try {
      await addWatchedFolder(path, label, headers)
      setCustomPath('')
      setCustomPathError('')
      setActiveFolderScan(label)
      showToast(`watching ${label}`)
      await Promise.all([refreshWatchedFolders(), refreshDiscovery(), refreshStatus(), refreshContext()])
      return true
    } catch (error) {
      showToast(error.message || 'unable to add folder')
      setAddingPaths((current) => {
        const next = new Set(current)
        next.delete(path)
        return next
      })
      return false
    }
  }, [headers, refreshContext, refreshDiscovery, refreshStatus, refreshWatchedFolders, showToast])

  const handleOpenPicker = useCallback(async () => {
    try {
      let picked = null

      if (isTauri()) {
        try {
          const nativeSelection = await openNativeDialog({
            directory: true,
            multiple: false,
            title: 'Choose a folder for Recurdo to watch',
          })
          if (typeof nativeSelection === 'string' && nativeSelection) {
            picked = {
              path: nativeSelection,
              label: nativeSelection.split('/').filter(Boolean).pop() || nativeSelection,
            }
          }
        } catch (error) {
          console.warn('Native folder picker failed, falling back to backend picker', error)
        }
      } else {
        picked = await openFolderPicker(headers)
      }

      if (!picked?.path) {
        picked = await openFolderPicker(headers)
      }

      if (picked?.path) {
        await addFolderWithChecks(picked, 'custom')
      } else {
        showToast('no folder selected')
      }
    } catch (error) {
      showToast(error.message || 'unable to open folder picker')
    }
  }, [addFolderWithChecks, headers, showToast])

  const handleRemoveFolder = useCallback(async (folder) => {
    try {
      await removeWatchedFolder(folder.path, headers)
      showToast(`removed ${folder.label}`)
      await Promise.all([refreshWatchedFolders(), refreshDiscovery(), refreshStatus(), refreshContext()])
    } catch (error) {
      showToast(error.message || 'unable to remove folder')
    }
  }, [headers, refreshContext, refreshDiscovery, refreshStatus, refreshWatchedFolders, showToast])

  const handleCustomSubmit = useCallback(async () => {
    const nextPath = customPath.trim()
    if (!nextPath) {
      return
    }
    const added = await addFolderWithChecks(
      { path: nextPath, label: nextPath.split('/').pop() || nextPath },
      'custom',
    )
    if (!added) {
      setCustomPathError('path not found or unavailable')
    }
  }, [addFolderWithChecks, customPath])

  const handleCognitiveBootstrap = useCallback(async (options = {}) => {
    try {
      const response = await triggerCognitiveBootstrap(headers, options)
      if (response?.cognitive) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          cognitive: response.cognitive,
        }))
      }
      if (response?.status === 'already_running') {
        showToast('cognitive bootstrap is already running')
      } else if (options.dryRun) {
        showToast('starting cognitive dry run')
      } else if (options.reset) {
        showToast('rebuilding cognitive layer')
      } else {
        showToast('starting cognitive bootstrap')
      }
      await Promise.allSettled([refreshStatus(), refreshHealth()])
    } catch (error) {
      showToast(error.message || 'unable to start cognitive bootstrap')
    }
  }, [headers, refreshHealth, refreshStatus, showToast])

  const handleGmailConnect = useCallback(async () => {
    try {
      const response = await connectGmail(headers)
      if (response?.gmail) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          gmail: response.gmail,
        }))
      }
      if (response?.status === 'already_running') {
        showToast('gmail authentication is already running')
      } else {
        showToast('opening gmail authentication')
      }
      await Promise.allSettled([refreshStatus(), refreshHealth()])
    } catch (error) {
      showToast(error.message || 'unable to start gmail authentication')
    }
  }, [headers, refreshHealth, refreshStatus, showToast])

  const handleGmailSync = useCallback(async (options = {}) => {
    try {
      const response = await triggerGmailSync(headers, options)
      if (response?.gmail) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          gmail: response.gmail,
        }))
      }
      if (response?.status === 'already_running') {
        showToast('gmail sync is already running')
      } else if (options.requireAttachments && options.historicalDays) {
        showToast(`starting attachment-only Gmail import for the last ${options.historicalDays} days`)
      } else if (options.requireAttachments) {
        showToast('starting attachment-only Gmail sync')
      } else if (options.historicalDays) {
        showToast(`starting Gmail import for the last ${options.historicalDays} days`)
      } else if (options.fullSync) {
        showToast('starting full gmail sync')
      } else {
        showToast('starting gmail sync')
      }
      await Promise.allSettled([refreshStatus(), refreshHealth()])
    } catch (error) {
      showToast(error.message || 'unable to start gmail sync')
    }
  }, [headers, refreshHealth, refreshStatus, showToast])

  const handleGmailStop = useCallback(async () => {
    try {
      const response = await stopGmailSync(headers)
      if (response?.gmail) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          gmail: response.gmail,
        }))
      }
      showToast(response?.status === 'idle' ? 'gmail sync is not running' : 'stopping gmail sync')
      await Promise.allSettled([refreshStatus(), refreshHealth()])
    } catch (error) {
      showToast(error.message || 'unable to stop gmail sync')
    }
  }, [headers, refreshHealth, refreshStatus, showToast])

  const handleGmailCleanup = useCallback(async () => {
    try {
      const response = await cleanupGmail(headers)
      if (response?.gmail) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          gmail: response.gmail,
        }))
      }
      const retired = Number(response?.summary?.retired ?? 0) || 0
      showToast(retired > 0 ? `cleared ${retired} imported gmail memories` : 'no imported gmail memories to clear')
      await Promise.allSettled([refreshStatus(), refreshHealth(), refreshItems(), refreshContext()])
    } catch (error) {
      showToast(error.message || 'unable to clear imported gmail memories')
    }
  }, [headers, refreshContext, refreshHealth, refreshItems, refreshStatus, showToast])

  const handleGmailDisconnect = useCallback(async () => {
    try {
      const response = await disconnectGmail(headers)
      if (response?.gmail) {
        setStatus((current) => normalizeStatusSnapshot({
          ...current,
          gmail: response.gmail,
        }))
      }
      showToast('gmail disconnected')
      await Promise.allSettled([refreshStatus(), refreshHealth()])
    } catch (error) {
      showToast(error.message || 'unable to disconnect gmail')
    }
  }, [headers, refreshHealth, refreshStatus, showToast])

  const handleSaveExcludedDirs = useCallback(async (dirs) => {
    const result = await configureLlm({ excluded_dirs: dirs }, headers)
    if (result) {
      setStatus((current) => normalizeStatusSnapshot({ ...current, llm: result }))
    }
    showToast('excluded folders saved')
  }, [headers, showToast])

  useEffect(() => {
    if (!session?.token || session?.user?.name || resolvedName) {
      return
    }

    let cancelled = false

    const refreshIdentity = async () => {
      try {
        const backend = await getBackendHttpUrl()
        const response = await fetch(`${backend}/auth/status`, {
          headers: {
            Authorization: `Bearer ${session.token}`,
          },
        })

        if (!response.ok) {
          return
        }

        const payload = await response.json()
        if (cancelled || !payload?.user) {
          return
        }

        setResolvedName(payload.user.name || '')
      } catch (error) {
        console.warn('Identity refresh failed', error)
      }
    }

    const timeoutId = window.setTimeout(() => {
      void refreshIdentity()
    }, 1800)

    return () => {
      cancelled = true
      window.clearTimeout(timeoutId)
    }
  }, [resolvedName, session])

  if (showOnboarding) {
    return (
      <Onboarding
        status={status}
        items={items}
        watchedFolders={watchedFolders}
        recentIndexedItems={recentIndexedItems}
        onStartIndexing={handleOnboardingStartIndexing}
        onComplete={handleOnboardingComplete}
        authHeaders={headers}
        uiTestMode={FORCE_ONBOARDING}
      />
    )
  }

  return (
    <div className="main-shell">
      <div className="app-window">
        <aside className="app-rail">
          <div className="rail-brand">
            <BrandLogo className="brand-logo rail-brand-logo" alt="Custody" />
          </div>

          <div className="rail-nav">
            {PAGES.map((item) => {
              const active = page === item.id
                const Icon =
                item.id === 'overview'
                  ? GridIcon
                  : item.id === 'files'
                    ? FileIcon
                    : item.id === 'cases'
                      ? CaseIcon
                      : GearIcon

              return (
                <button
                  key={item.id}
                  type="button"
                  className={`rail-btn${active ? ' active' : ''}`}
                  onClick={() => setPage(item.id)}
                  onMouseEnter={(event) => handleTooltipEnter(item.label, event)}
                  onMouseLeave={handleTooltipLeave}
                >
                  <Icon />
                </button>
              )
            })}
          </div>

          <div className="rail-status">
            {daysRemaining <= 14 && daysRemaining > 0 ? (
              <div
                style={{
                  fontFamily: 'var(--mono)',
                  fontSize: 9,
                  color: daysRemaining <= 7 ? 'rgba(196,160,80,0.9)' : 'var(--text-3)',
                  textAlign: 'center',
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                }}
              >
                {daysRemaining}d left
              </div>
            ) : null}
            <div className={`status-dot${status.is_scanning ? ' scanning' : ''}`} />
          </div>
        </aside>

        <main className="app-content scroll">
          <div className="content-wrap">
          {healthBanner ? (
            <div className={`startup-health-banner${healthBannerExpanded ? ' expanded' : ''}`}>
              <div className="startup-health-banner-top">
                <div className="startup-health-banner-copy">
                  <span className="startup-health-banner-icon">⚡</span>
                  <span>{healthBanner.message}</span>
                </div>
                <div className="startup-health-banner-actions">
                  <button
                    type="button"
                    className="startup-health-link"
                    onClick={() => setHealthBannerExpanded((current) => !current)}
                  >
                    {healthBannerExpanded ? 'hide details' : 'details'}
                  </button>
                  <button
                    type="button"
                    className="startup-health-link"
                    onClick={dismissHealthBanner}
                  >
                    dismiss
                  </button>
                </div>
              </div>

              {healthBannerExpanded ? (
                <div className="startup-health-banner-body">
                  {healthBanner.issues.length ? (
                    <div className="startup-health-block">
                      <div className="startup-health-label">what happened</div>
                      {healthBanner.issues.map((issue) => (
                        <div key={issue} className="startup-health-detail">
                          {issue}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {healthBanner.actions.length ? (
                    <div className="startup-health-block">
                      <div className="startup-health-label">what Recurdo did</div>
                      {healthBanner.actions.map((action) => (
                        <div key={action} className="startup-health-detail">
                          {action.replace(/_/g, ' ')}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {page === 'overview' ? (
            <div className="overview-wrap">
              <div className="overview-bg-number">{totalItemsLabel}</div>

                <div className="page-header">
                  <Greeting session={greetingSession} />
                  <div className="label page-subtitle">
                    <span>
                      {scanProgressLabel
                        ? `${totalItemsLabel} memories · ${scanProgressLabel}`
                        : watchedFilesContextLabel
                          ? `${totalItemsLabel} memories · ${watchedFilesContextLabel}`
                          : `${totalItemsLabel} memories · searching`}
                    </span>
                    <span
                      title={wsFallbackPolling ? 'Live updates are reconnecting. Polling every 5 seconds until the socket recovers.' : wsMeta.title}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 6,
                        marginLeft: 10,
                        color: wsMeta.dotColor,
                      }}
                    >
                      <span
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: '999px',
                          background: wsMeta.dotColor,
                          boxShadow: wsMeta.pulse ? `0 0 0 0 ${wsMeta.dotColor}` : 'none',
                          animation: wsMeta.pulse ? 'pulse3 1.1s ease infinite' : 'none',
                        }}
                      />
                      {wsMeta.label}
                      {wsFallbackPolling ? ' · polling' : ''}
                    </span>
                    {showHealthReconnect ? (
                      <span
                        title="Recurdo is keeping the backend warm and reconnecting health checks in the background."
                        style={{
                          marginLeft: 10,
                          color: 'rgba(214, 178, 98, 0.92)',
                        }}
                      >
                        reconnecting...
                      </span>
                    ) : null}
                  </div>
                </div>

              <div className="overview-grid">
                <div className="surface-card hero-card">
                  <div className="status-pill">
                    <span className="pulse" />
                    {statusPillLabel}
                  </div>

                  <p className="hero-copy">
                    Recurdo keeps a local memory layer for the files you touch most, so you can
                    recall them by intent instead of hunting through folders.
                  </p>

                  <IndexStatusBar status={status} />

                  <div className="hero-metric" title={memoryTooltip}>
                    <div className="hero-metric-value">{totalItemsLabel}</div>
                    <div className="hero-metric-label">
                      {status.is_scanning || status.background?.state === 'running' ? 'memories stored' : 'memories'}
                    </div>
                  </div>

                  {scanProgressLabel ? (
                    <div className="label" style={{ marginTop: 14 }}>
                      {scanProgressLabel}
                    </div>
                  ) : watchedFilesContextLabel ? (
                    <div className="label" style={{ marginTop: 14 }}>
                      {watchedFilesContextLabel}
                    </div>
                  ) : null}

                  <div className="shortcut-block">
                    <div className="sc-key">⌘</div>
                    <div className="sc-key">⇧</div>
                    <div className="sc-key">Space</div>
                    <span className="sc-label">search from anywhere</span>
                  </div>
                </div>

                <div className="overview-side-column">
                  <NowPanel authHeaders={headers} />
                  <div className="surface-card meta-card">
                    <div className="info-grid">
                      <div className="info-cell">
                        <div className="info-key">privacy</div>
                        <div className="info-val">everything stays on this mac</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">storage</div>
                        <div className="info-val">local · ~/.recurdo</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">memories stored</div>
                        <div className="info-val">{totalItemsLabel} memories</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">files watched</div>
                        <div className="info-val">{filesWatchedLabel} files watched</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">duplicates skipped</div>
                        <div className="info-val">{duplicatesSkippedLabel} duplicates skipped</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">watching</div>
                        <div className="info-val">{watchedFolderNamesLabel}</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">access</div>
                        <div className="info-val">{accessState}</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">cognitive layer</div>
                        <div className="info-val">{cognitiveStateLabel}</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">cognitive status</div>
                        <div className="info-val">{cognitiveSummaryLabel}</div>
                      </div>
                      <div className="info-cell">
                        <div className="info-key">search</div>
                        <div className="info-val">instant overlay with keyboard shortcut</div>
                      </div>
                    </div>
                  </div>

                  <ProactiveContextCard
                    payload={proactiveContext}
                    loading={contextLoading}
                    onRefresh={refreshContext}
                  />
                </div>
              </div>
            </div>
          ) : null}

          {page === 'files' ? (
            <div style={{ animation: 'fadeUp 0.45s ease both' }}>
              <div className="page-header">
                <div className="label page-subtitle">indexed archive</div>
                <div
                  style={{
                    marginTop: 14,
                    fontFamily: 'var(--serif)',
                    fontSize: 'clamp(28px, 4vw, 40px)',
                    fontWeight: 300,
                    color: 'var(--text-1)',
                    letterSpacing: '-0.02em',
                  }}
                >
                  recently remembered files
                </div>
              </div>

              <div className="files-list">
                {items.map((item) => (
                  <div key={item.id} className="files-row">
                    <TinyBadge fileType={item.file_type} />
                    <div className="files-name">{item.title || item.filename}</div>
                    <div className="files-type">{item.file_type || 'file'}</div>
                    <div className="files-date">
                      {memoryDateLabel(item) || '—'}
                    </div>
                  </div>
                ))}
                {!items.length ? <div className="empty-copy">no memories stored yet</div> : null}
              </div>
            </div>
          ) : null}

          {page === 'cases' ? (
            <CasesPage authHeaders={headers} onShowToast={showToast} />
          ) : null}

          {page === 'settings' ? (
            <div className="settings-shell" style={{ animation: 'fadeUp 0.45s ease both' }}>
              <div className="page-header">
                <div className="label page-subtitle">index settings</div>
                <div
                  style={{
                    marginTop: 14,
                    fontFamily: 'var(--serif)',
                    fontSize: 'clamp(28px, 4vw, 40px)',
                    fontWeight: 300,
                    color: 'var(--text-1)',
                    letterSpacing: '-0.02em',
                  }}
                >
                  choose what recurdo watches
                </div>
              </div>

              <FolderSettingsPage
                authHeaders={headers}
                session={session}
                watchedFolders={watchedFolders}
                discovery={discovery}
                discoveryLoading={discoveryLoading}
                status={status}
                addingPaths={addingPaths}
                onBootstrapCognitive={handleCognitiveBootstrap}
                onCleanupGmail={handleGmailCleanup}
                onConnectGmail={handleGmailConnect}
                onDisconnectGmail={handleGmailDisconnect}
                onStopGmailSync={handleGmailStop}
                onSyncGmail={handleGmailSync}
                onOpenPicker={handleOpenPicker}
                onRemoveFolder={handleRemoveFolder}
                onAddFolder={addFolderWithChecks}
                customPath={customPath}
                customPathError={customPathError}
                onCustomPathChange={(value) => {
                  setCustomPath(value)
                  setCustomPathError('')
                }}
                onCustomPathSubmit={handleCustomSubmit}
                onSaveExcludedDirs={handleSaveExcludedDirs}
                onSignOut={onSignOut}
                onShowToast={showToast}
              />
              <MemoryBrowserSection authHeaders={headers} />
              <TimelineSection authHeaders={headers} />
              <AliasesSection authHeaders={headers} />
            </div>
          ) : null}
          </div>
        </main>
      </div>

      {tooltip ? (
        <div
          className="rail-tooltip"
          style={{
            left: tooltip.left,
            top: tooltip.top,
          }}
        >
          {tooltip.label}
        </div>
      ) : null}

      {toast ? <div className="main-toast">{toast}</div> : null}
    </div>
  )
}

export default MainApp
