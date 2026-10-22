import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { open as openNativeDialog } from '@tauri-apps/plugin-dialog'
import { estimateFolder, openFolderPicker } from '../api/client'
import BrandLogo from '../components/BrandLogo'
import {
  ONBOARDING_STEP_FOLDER_SELECT,
  ONBOARDING_STEP_SCANNING,
  isRestoringExistingLibraryStatus,
  isSearchReadyWhileFinishingStatus,
  parseStoredOnboardingStep,
  serializeOnboardingStep,
} from './onboardingState'

const ONBOARDING_STATE_KEY = 'recurdo_onboarding_v2'
const SEARCH_STOPWORDS = new Set([
  'and',
  'copy',
  'draft',
  'final',
  'for',
  'from',
  'new',
  'notes',
  'page',
  'the',
  'untitled',
  'version',
])

const DEFAULT_FOLDER_OPTIONS = [
  {
    id: 'downloads',
    label: 'Downloads',
    subtitle: 'downloads, attachments, installers, exports',
    paths: ['~/Downloads'],
    matches: ['/Downloads'],
  },
  {
    id: 'desktop',
    label: 'Desktop',
    subtitle: 'working files, screenshots, things in progress',
    paths: ['~/Desktop'],
    matches: ['/Desktop'],
  },
  {
    id: 'documents',
    label: 'Documents',
    subtitle: 'docs, spreadsheets, PDFs, and notes',
    paths: ['~/Documents'],
    matches: ['/Documents'],
  },
  {
    id: 'screenshots',
    label: 'Screenshots',
    subtitle: 'saved captures and image-heavy folders',
    paths: ['~/Pictures/Screenshots', '~/Desktop/Screenshots'],
    matches: ['/Pictures/Screenshots', '/Desktop/Screenshots'],
  },
]

function readStoredStep() {
  return parseStoredOnboardingStep(window.localStorage.getItem(ONBOARDING_STATE_KEY))
}

function writeStoredStep(step) {
  const serialized = serializeOnboardingStep(step)
  if (serialized) {
    window.localStorage.setItem(ONBOARDING_STATE_KEY, serialized)
    return
  }
  window.localStorage.removeItem(ONBOARDING_STATE_KEY)
}

function normalizePath(path) {
  return String(path || '').replace(/\/+$/, '')
}

function relativeHomePath(path) {
  if (!path) {
    return ''
  }
  if (String(path).startsWith('~/')) {
    return String(path)
  }
  const match = String(path).match(/^\/Users\/[^/]+(\/.*)?$/)
  if (match) {
    return `~${match[1] || ''}`
  }
  return String(path)
}

function formatCount(count) {
  return Number(count || 0).toLocaleString()
}

function formatTime(total, minutes) {
  if (!total) {
    return '0 min'
  }
  if (total < 500) {
    return '<1 min'
  }
  if (minutes >= 10) {
    return `~${minutes} min`
  }
  return `~${minutes} min`
}

function estimateMinutesForSelection(option) {
  const total = Number(option?.estimate?.total || 0)
  if (!total) {
    return 0
  }
  return Number(option?.estimate?.estimated_minutes || 0)
}

const EMPTY_SCAN_BUCKETS = {
  already_in_library: 0,
  will_index: 0,
  deferred_large: 0,
  skipped_old: 0,
  unsupported_hidden: 0,
  skipped_too_large: 0,
}

function getEstimateBuckets(estimate) {
  const buckets = estimate?.buckets || {}
  const skippedBreakdown = estimate?.skipped_breakdown || {}
  const candidateTotal = Number(estimate?.candidate_total || 0)
  const visibleTotal = Number(estimate?.visible_total || 0)
  const fallbackUnsupportedHidden = Math.max(visibleTotal - candidateTotal, 0)

  return {
    already_in_library: Number(buckets.already_in_library ?? estimate?.already_accounted_for ?? 0),
    will_index: Number(buckets.will_index ?? estimate?.total ?? 0),
    deferred_large: Number(buckets.deferred_large ?? estimate?.deferred_background ?? 0),
    skipped_old: Number(buckets.skipped_old ?? estimate?.skipped_too_old ?? 0),
    unsupported_hidden: Number(
      buckets.unsupported_hidden
      ?? skippedBreakdown.unsupported_hidden
      ?? fallbackUnsupportedHidden,
    ),
    skipped_too_large: Number(skippedBreakdown.too_large ?? estimate?.skipped_too_large ?? 0),
  }
}

function addEstimateBuckets(current, estimate) {
  const next = getEstimateBuckets(estimate)
  return {
    already_in_library: current.already_in_library + next.already_in_library,
    will_index: current.will_index + next.will_index,
    deferred_large: current.deferred_large + next.deferred_large,
    skipped_old: current.skipped_old + next.skipped_old,
    unsupported_hidden: current.unsupported_hidden + next.unsupported_hidden,
    skipped_too_large: current.skipped_too_large + next.skipped_too_large,
  }
}

function summarizeEstimate(estimate) {
  const buckets = getEstimateBuckets(estimate)
  const parts = []

  if (buckets.already_in_library > 0) {
    parts.push(`${formatCount(buckets.already_in_library)} already in your memory`)
  }
  if (buckets.deferred_large > 0) {
    parts.push(`+${formatCount(buckets.deferred_large)} later in background`)
  }
  if (buckets.skipped_old > 0) {
    parts.push(`${formatCount(buckets.skipped_old)} older files skipped`)
  }
  if (buckets.unsupported_hidden > 0) {
    parts.push(`${formatCount(buckets.unsupported_hidden)} system/unsupported`)
  }
  if (buckets.skipped_too_large > 0) {
    parts.push(`${formatCount(buckets.skipped_too_large)} too large right now`)
  }

  return parts.slice(0, 3)
}

function describeCounts(byType = {}) {
  const ordered = ['documents', 'images', 'videos', 'audio', 'other']
  const labels = {
    documents: 'documents',
    images: 'images',
    videos: 'videos',
    audio: 'audio',
    other: 'other',
  }

  return ordered
    .filter((key) => Number(byType[key] || 0) > 0)
    .map((key) => `${labels[key]}: ${formatCount(byType[key])}`)
}

function matchesDefaultOption(option, actualPath) {
  const normalized = normalizePath(actualPath)
  return option.matches.some((suffix) => normalized.endsWith(suffix))
}

function toSearchPhrase(source) {
  const cleaned = String(source || '')
    .replace(/\.[a-z0-9]+$/i, ' ')
    .replace(/[_-]+/g, ' ')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  if (!cleaned) {
    return ''
  }

  const tokens = cleaned
    .split(' ')
    .map((token) => token.toLowerCase())
    .filter((token) => token.length >= 3 && !SEARCH_STOPWORDS.has(token))
    .slice(0, 3)

  return tokens.join(' ')
}

function fileTypeBucket(fileType, handlerType) {
  const normalizedType = String(fileType || '').toLowerCase()
  const normalizedHandler = String(handlerType || '').toLowerCase()

  if (normalizedType === 'image' || normalizedHandler === 'image') {
    return 'images'
  }
  if (normalizedType === 'video' || normalizedHandler === 'video') {
    return 'videos'
  }
  if (normalizedType === 'audio' || normalizedHandler === 'audio') {
    return 'audio'
  }
  return 'documents'
}

function buildSearchExamples(items = [], recentIndexedItems = []) {
  const searchableRecentItems = recentIndexedItems.filter((item) => item?.kind !== 'duplicate')
  const suggestions = []
  const seen = new Set()

  for (const item of [...searchableRecentItems, ...items]) {
    const query = toSearchPhrase(item?.title || item?.filename || '')
    if (!query || seen.has(query)) {
      continue
    }
    seen.add(query)
    suggestions.push(query)
    if (suggestions.length === 3) {
      return suggestions
    }
  }

  const fallbackBuckets = new Set(
    [...searchableRecentItems, ...items].map((item) => fileTypeBucket(item?.file_type, item?.handler_type)),
  )

  if (fallbackBuckets.has('documents')) {
    suggestions.push('project brief')
  }
  if (fallbackBuckets.has('images')) {
    suggestions.push('screenshot')
  }
  if (fallbackBuckets.has('audio')) {
    suggestions.push('voice note')
  }

  return suggestions.filter(Boolean).slice(0, 3)
}

async function estimateOption(option, authHeaders) {
  const attempts = await Promise.allSettled(
    option.paths.map(async (path) => {
      const response = await estimateFolder(path, authHeaders)
      return {
        ...response,
        id: option.id,
        label: option.label,
        subtitle: option.subtitle,
        path: response?.path || path,
        displayPath: relativeHomePath(response?.path || path),
        isCustom: Boolean(option.isCustom),
      }
    }),
  )

  const successful = attempts
    .filter((result) => result.status === 'fulfilled')
    .map((result) => result.value)

  const existing = successful
    .filter((result) => result.exists)
    .sort((left, right) => Number(right.total || 0) - Number(left.total || 0))

  if (existing.length) {
    return existing[0]
  }

  if (successful.length) {
    return successful[0]
  }

  return {
    id: option.id,
    label: option.label,
    subtitle: option.subtitle,
    path: option.paths[0],
    displayPath: relativeHomePath(option.paths[0]),
    exists: false,
    total: 0,
    by_type: {},
    capped: false,
    estimated_minutes: 0,
    buckets: { ...EMPTY_SCAN_BUCKETS },
    skipped_breakdown: {
      old: 0,
      unsupported_hidden: 0,
      too_large: 0,
    },
    searchable_after_scan: 0,
    skipped_total: 0,
    accessible: false,
    permission_required: false,
    error_message: '',
    isCustom: Boolean(option.isCustom),
  }
}

function ProgressDots({ step }) {
  return (
    <div className="step-dots">
      {[0, 1, 2].map((index) => {
        let state = 'pending'
        if (index < step) {
          state = 'done'
        }
        if (index === step) {
          state = 'active'
        }
        return <div key={index} className={`step-dot ${state}`} />
      })}
    </div>
  )
}

function WelcomeStep({ hasExistingLibrary, totalItems, watchedFolderCount, onContinue, onSkipExisting }) {
  return (
    <div className="main-onboarding-shell">
      <div className="ob-welcome onboarding-panel">
        <ProgressDots step={0} />

        <div className="ob-bg-word">
          <BrandLogo className="brand-logo onboarding-watermark-logo" alt="Custody" />
        </div>

        <p className="ob-section-label">welcome to recurdo</p>
        <h1 className="onboarding-title">your files,<br /><span style={{ fontStyle: 'italic', opacity: 0.7 }}>remembered</span></h1>
        <p className="onboarding-copy">
          Search your files the way your brain works — by what they contained, who sent them, or when you last saw them. Not by filename.
        </p>

        <div className="ob-bullets">
          <div className="ob-bullet">
            <div className="ob-bullet-dot" />
            <span className="ob-bullet-text">runs privately on your Mac — no cloud uploads</span>
          </div>
          <div className="ob-bullet">
            <div className="ob-bullet-dot" />
            <span className="ob-bullet-text">lives in your menu bar, one keystroke away</span>
          </div>
          <div className="ob-bullet">
            <div className="ob-bullet-dot" />
            <span className="ob-bullet-text">searchable while it indexes — no waiting</span>
          </div>
          <div className="ob-bullet">
            <div className="ob-bullet-dot" />
            <span className="ob-bullet-text">ask questions, get AI answers from your own files</span>
          </div>
        </div>

        {hasExistingLibrary ? (
          <div className="onboarding-restore-callout">
            <div className="onboarding-restore-title">looks like you&apos;ve used Recurdo before</div>
            <div className="onboarding-restore-body">
              We found {formatCount(totalItems)} stored memories across {formatCount(watchedFolderCount)} watched folder
              {watchedFolderCount === 1 ? '' : 's'}.
            </div>
            <div className="onboarding-inline-actions">
              <button type="button" className="ob-btn ghost" onClick={onSkipExisting}>
                open my library
              </button>
              <button type="button" className="ob-btn" onClick={onContinue}>
                choose folders again →
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className="ob-btn" onClick={onContinue}>
            get started →
          </button>
        )}
      </div>
    </div>
  )
}

function FolderSelectionStep({
  options,
  selectedIds,
  loadingIds,
  onToggle,
  onAddCustomFolder,
  onStart,
  onSkipForNow,
  selectedTotalFiles,
  selectedTotalMinutes,
  selectedBuckets,
  hasExistingLibrary,
  isSubmitting,
  error,
}) {
  const hasSelection = selectedIds.size > 0
  const showHeavyWarning = selectedTotalFiles > 5000
  const selectedBlockedCount = options.filter(
    (option) => selectedIds.has(option.id) && option.estimate?.accessible === false,
  ).length
  const searchableAfterScan = selectedBuckets.already_in_library + selectedBuckets.will_index
  const skippedTotal =
    selectedBuckets.skipped_old + selectedBuckets.unsupported_hidden + selectedBuckets.skipped_too_large

  return (
    <div className="main-onboarding-shell">
      <div className="ob-folders onboarding-panel onboarding-wide-panel">
        <ProgressDots step={1} />

        <p className="ob-section-label">folder selection</p>
        <h1 className="onboarding-title">choose what to remember</h1>
        <p className="onboarding-copy onboarding-copy-left">
          Select the folders you want Recurdo to watch. You can change this anytime in settings.
        </p>

        <div className="folder-choice-list">
          {options.map((option) => {
            const selected = selectedIds.has(option.id)
            const loading = loadingIds.has(option.id)
            const counts = describeCounts(option.estimate?.by_type || {})
            const unavailable = !loading && option.estimate && option.estimate.exists === false
            const permissionRequired = !loading && Boolean(option.estimate?.permission_required)
            const inaccessible = !loading && option.estimate?.exists !== false && option.estimate?.accessible === false

            return (
              <button
                key={option.id}
                type="button"
                className={`folder-choice-card${selected ? ' selected' : ''}${unavailable || inaccessible ? ' unavailable' : ''}`}
                onClick={() => onToggle(option.id)}
                disabled={loading || unavailable || isSubmitting}
              >
                <div className="folder-choice-header">
                  <div className="folder-choice-left">
                    <span className={`folder-choice-checkbox${selected ? ' checked' : ''}`} />
                    <div>
                      <div className="folder-choice-name">{option.label}</div>
                      <div className="folder-choice-path">{option.displayPath}</div>
                    </div>
                  </div>

                  <div className="folder-choice-meta">
                    {loading ? (
                      <span>checking…</span>
                    ) : option.estimate?.exists ? (
                      <>
                        {inaccessible ? (
                          <span>permission needed</span>
                        ) : (
                          <>
                            <span>{formatCount(option.estimate.total)} first-scan files</span>
                            <span>
                              {Number(option.estimate.deferred_background || 0) > 0
                                ? `+${formatCount(option.estimate.deferred_background)} background`
                                : formatTime(option.estimate.total, option.estimate.estimated_minutes)}
                            </span>
                          </>
                        )}
                      </>
                    ) : (
                      <span>not found</span>
                    )}
                  </div>
                </div>

                <div className="folder-choice-details">
                  {loading ? (
                    <span>estimating files without indexing anything yet…</span>
                  ) : unavailable ? (
                    <span>This folder was not found on this Mac.</span>
                  ) : permissionRequired ? (
                    <span>macOS permission is required before Recurdo can scan this folder.</span>
                  ) : inaccessible ? (
                    <span>{option.estimate?.error_message || 'This folder is not accessible right now.'}</span>
                  ) : counts.length ? (
                    [
                      ...counts,
                      ...summarizeEstimate(option.estimate),
                    ].map((count) => <span key={`${option.id}-${count}`}>{count}</span>)
                  ) : (
                    <span>no indexable files found yet</span>
                  )}
                </div>

                {!loading && option.estimate?.capped ? (
                  <div className="folder-choice-hint">Estimate capped at 20,000 files for speed.</div>
                ) : option.subtitle ? (
                  <div className="folder-choice-hint">{option.subtitle}</div>
                ) : null}
              </button>
            )
          })}

          <button
            type="button"
            className="folder-choice-card folder-choice-add"
            onClick={onAddCustomFolder}
            disabled={isSubmitting}
          >
            <span className="folder-choice-add-icon">+</span>
            <span>Add custom folder</span>
          </button>
        </div>

        <div className="folder-selection-summary">
          {hasExistingLibrary ? 'Indexing next' : 'First scan'} · {formatCount(selectedTotalFiles)} files · estimated{' '}
          {formatTime(selectedTotalFiles, selectedTotalMinutes)}
        </div>

        {hasSelection ? (
          <div className="folder-scan-preview">
            <div className="folder-scan-bucket folder-scan-bucket-primary">
              <div className="folder-scan-bucket-label">files in your memory</div>
              <div className="folder-scan-bucket-count">{formatCount(searchableAfterScan)}</div>
              <div className="folder-scan-bucket-note">
                {formatCount(selectedBuckets.already_in_library)} already searchable
                {selectedBuckets.will_index > 0
                  ? ` · ${formatCount(selectedBuckets.will_index)} indexing now`
                  : ' · everything here is already remembered'}
              </div>
            </div>

            {selectedBuckets.deferred_large > 0 ? (
              <div className="folder-scan-bucket folder-scan-bucket-secondary">
                <div className="folder-scan-bucket-label">background follow-up</div>
                <div className="folder-scan-bucket-count">{formatCount(selectedBuckets.deferred_large)}</div>
                <div className="folder-scan-bucket-note">
                  larger visual, spreadsheet, and oversized files will finish later
                </div>
              </div>
            ) : null}

            {skippedTotal > 0 ? (
              <details className="folder-scan-skipped">
                <summary>{formatCount(skippedTotal)} files skipped by policy</summary>
                <div className="folder-scan-skipped-grid">
                  {selectedBuckets.skipped_old > 0 ? (
                    <span>{formatCount(selectedBuckets.skipped_old)} older than the default window</span>
                  ) : null}
                  {selectedBuckets.unsupported_hidden > 0 ? (
                    <span>{formatCount(selectedBuckets.unsupported_hidden)} system, hidden, or unsupported</span>
                  ) : null}
                  {selectedBuckets.skipped_too_large > 0 ? (
                    <span>{formatCount(selectedBuckets.skipped_too_large)} too large right now</span>
                  ) : null}
                </div>
                <div className="folder-scan-skipped-note">
                  These files are not lost. Recurdo is skipping them on purpose so the first pass stays fast and useful.
                </div>
              </details>
            ) : null}
          </div>
        ) : null}

        {showHeavyWarning ? (
          <div className="folder-selection-warning">
            That&apos;s a lot of files. The first scan will take 10+ minutes and run in the
            background. Recurdo will be ready faster if you start with fewer folders.
          </div>
        ) : null}

        {selectedBlockedCount ? (
          <div className="folder-selection-warning">
            Grant macOS access to the selected folder{selectedBlockedCount > 1 ? 's' : ''} first. Recurdo will ask again when you start indexing.
          </div>
        ) : null}

        {error ? <div className="folder-selection-error">{error}</div> : null}

        <div className="onboarding-footer-actions">
          <button
            type="button"
            className="ob-btn full"
            onClick={onStart}
            disabled={!hasSelection || isSubmitting}
          >
            {isSubmitting ? 'starting…' : 'start indexing →'}
          </button>

          <button type="button" className="ob-text-link" onClick={onSkipForNow} disabled={isSubmitting}>
            skip for now
          </button>
        </div>
      </div>
    </div>
  )
}

function ScanningStep({
  status,
  uniqueMemories,
  filesWatched,
  selectedTotalFiles,
  selectedTotalMinutes,
  hasExistingLibrary,
  recentIndexedItems,
  items,
  scanStartedAt,
  now,
  onOpenApp,
  uiTestMode = false,
}) {
  const phase = uiTestMode
    ? (status.scan_phase?.phase || 'checking')
    : (status.scan_phase?.phase || (status.is_scanning ? 'indexing' : 'complete'))
  const phaseFound = Number(status.scan_phase?.found || 0)
  const phaseChecked = Number(status.scan_phase?.checked || 0)
  const phaseTotal = Number(status.scan_phase?.total || 0)
  const phaseNewFound = Number(status.scan_phase?.new_found || 0)
  const phaseMessage = String(status.scan_phase?.message || '').trim()
  const pendingFiles = Number(status.pending_files ?? 0) || 0
  const queueSize = Number(status.queue_size ?? 0) || 0
  const backendProcessed = Number(status.scan_progress?.processed ?? status.scan_progress?.indexed ?? 0) || 0
  const backendTotal = Number(status.scan_progress?.total || 0) || 0
  const restoringExistingLibrary = isRestoringExistingLibraryStatus({
    uiTestMode,
    isScanning: status.is_scanning,
    pendingFiles,
    queueSize,
    phaseFound,
    phaseChecked,
    phaseTotal,
    phaseNewFound,
    backendProcessed,
    backendTotal,
    uniqueMemories,
    filesWatched,
  })
  const hasBackendActivity = uiTestMode
    ? true
    : Boolean(
      status.is_scanning
      || phaseFound > 0
      || phaseChecked > 0
      || phaseTotal > 0
      || phaseNewFound > 0
      || backendProcessed > 0
      || backendTotal > 0
      || uniqueMemories > 0
      || filesWatched > 0,
    )
  const statusLooksReset = !uiTestMode
    && !status.is_scanning
    && pendingFiles === 0
    && queueSize === 0
    && uniqueMemories === 0
    && filesWatched === 0
    && (
      phaseFound > 0
      || phaseChecked > 0
      || phaseTotal > 0
      || phaseNewFound > 0
      || backendProcessed > 0
      || backendTotal > 0
    )
  const hasMeaningfulBackendActivity = hasBackendActivity && !statusLooksReset
  const hasPendingBacklog = pendingFiles > 0 || queueSize > 0
  const hasCompletionSignal = uiTestMode
    ? true
    : Boolean(
      uniqueMemories > 0
      || backendProcessed > 0
      || phaseChecked > 0,
    )
  const indexed = uiTestMode
    ? Math.min(36, Number(status.scan_progress?.indexed || 0) || 18)
    : Number(status.scan_progress?.indexed || backendProcessed)
  const total = uiTestMode
    ? Math.max(60, Number(status.scan_progress?.total || 0) || selectedTotalFiles || 60)
    : backendTotal || Number(selectedTotalFiles || 0)
  const indexingTotal = uiTestMode
    ? total
    : backendTotal || (phase === 'indexing' && phaseNewFound > 0 ? phaseNewFound : total)
  const percent = indexingTotal
    ? Math.min(100, Math.max(0, Math.round((indexed / indexingTotal) * 100)))
    : 0
  const remaining = Math.max(indexingTotal - indexed, 0)
  const effectiveRecent = recentIndexedItems.length
    ? recentIndexedItems
    : uiTestMode
      ? [
          { kind: 'indexed', title: 'Invoice_March.pdf', file_type: 'pdf' },
          { kind: 'indexed', title: 'Screenshot 2025-01-14.png', file_type: 'image' },
          { kind: 'indexed', title: 'Project_Brief.docx', file_type: 'docx' },
          { kind: 'duplicate', title: 'Duplicate screenshot copy', file_type: 'image' },
        ]
      : []

  const elapsedMinutes =
    scanStartedAt && now > scanStartedAt ? (now - scanStartedAt) / 60000 : 0

  let estimatedMinutesLeft = selectedTotalMinutes
  if (indexed >= 25 && elapsedMinutes >= 0.75) {
    const rate = indexed / elapsedMinutes
    if (rate > 0) {
      estimatedMinutesLeft = Math.max(1, Math.round(remaining / rate))
    }
  }

  const liveExamples = buildSearchExamples(items, effectiveRecent)
  const searchableNow = !statusLooksReset && (uniqueMemories >= 10 || indexed >= 10)
  const hasFinishedWork = uniqueMemories > 0 || filesWatched > 0 || backendProcessed > 0
  const searchReadyWhileFinishing = isSearchReadyWhileFinishingStatus({
    uiTestMode,
    isScanning: status.is_scanning,
    pendingFiles,
    queueSize,
    searchableNow,
  })
  const scanDone = uiTestMode
    ? indexed >= total
    : searchReadyWhileFinishing
      || (
        !status.is_scanning
        && !hasPendingBacklog
        && !statusLooksReset
        && hasFinishedWork
        && hasCompletionSignal
        && (phase === 'complete' || (backendTotal > 0 && remaining === 0))
      )

  let progressPercent = percent
  let sectionLabel = 'indexing'
  const storedSoFar = Math.max(indexed, uniqueMemories)
  const displayTotal = phase === 'indexing' ? indexingTotal : total
  const hasBackgroundDeferralHint = phaseMessage.toLowerCase().includes('background')
  let lineOne = `indexing ${formatCount(phaseNewFound || displayTotal)} new memories...`
  let lineTwo = `${formatCount(storedSoFar)} memories stored · ${formatCount(remaining)} remaining`
  let lineThree = indexed >= 25 && elapsedMinutes >= 0.75
    ? `${formatTime(remaining, estimatedMinutesLeft)} left`
    : hasBackgroundDeferralHint
      ? 'core docs first — heavier files will continue in the background'
      : 'estimating time left...'

  if (!hasMeaningfulBackendActivity && !uiTestMode) {
    sectionLabel = 'starting'
    progressPercent = 2
    lineOne = hasExistingLibrary ? 'starting your next scan...' : 'starting your first scan...'
    lineTwo = `connecting ${formatCount(selectedTotalFiles)} selected files to the indexer`
    lineThree = 'this usually takes a few seconds'
  }

  if (restoringExistingLibrary) {
    sectionLabel = 'restoring'
    progressPercent = 100
    lineOne = 'restoring your existing library...'
    lineTwo = `${formatCount(uniqueMemories)} memories already stored across ${formatCount(filesWatched)} watched files`
    lineThree = 'search is ready now while Recurdo syncs the latest indexing status'
  } else if (hasMeaningfulBackendActivity && phase === 'discovering') {
    progressPercent = selectedTotalFiles
      ? Math.min(18, Math.max(4, Math.round((phaseFound / selectedTotalFiles) * 18)))
      : 8
    sectionLabel = 'discovering'
    lineOne = 'finding files in your folders...'
    lineTwo = `${formatCount(phaseFound)} files discovered so far`
    lineThree = 'estimating what needs indexing next'
  } else if (hasMeaningfulBackendActivity && phase === 'checking') {
    progressPercent = phaseTotal
      ? Math.min(100, Math.max(0, Math.round((phaseChecked / phaseTotal) * 100)))
      : 0
    sectionLabel = 'checking'
    lineOne = `checking ${formatCount(phaseChecked)} / ${formatCount(phaseTotal)} files...`
    lineTwo = `${formatCount(phaseNewFound)} look new or changed · ${formatCount(Math.max(phaseTotal - phaseChecked, 0))} left to check`
    lineThree = 'indexing starts as soon as this pass finishes'
  } else if (hasMeaningfulBackendActivity && phase === 'indexing') {
    sectionLabel = 'indexing'
    progressPercent = indexed > 0 ? Math.max(1, percent) : percent
    lineOne = `indexing ${formatCount(phaseNewFound || indexingTotal)} new memories...`
    lineTwo = `${formatCount(storedSoFar)} memories stored · ${formatCount(remaining)} remaining`
    lineThree = indexed >= 25 && elapsedMinutes >= 0.75
      ? `${formatTime(remaining, estimatedMinutesLeft)} left`
      : hasBackgroundDeferralHint
        ? 'core docs first — heavier files will continue in the background'
        : 'estimating time left...'
  } else if (!status.is_scanning && hasPendingBacklog) {
    sectionLabel = 'finishing'
    progressPercent = total
      ? Math.min(99, Math.max(percent, Math.round(((total - pendingFiles) / total) * 100)))
      : Math.min(99, Math.max(percent, 92))
    lineOne = 'search is ready while Recurdo finishes the library...'
    lineTwo = `${formatCount(uniqueMemories)} memories stored · ${formatCount(pendingFiles)} files still pending`
    lineThree = 'the remaining files are retrying in the background'
  } else if (statusLooksReset) {
    sectionLabel = 'starting'
    progressPercent = 2
    lineOne = 'syncing your scan state...'
    lineTwo = 'the backend restarted and is refreshing the real library status'
    lineThree = 'this should settle in a few seconds'
  }

  if (scanDone) {
    const finishingCount = Number(pendingFiles || queueSize || 0)
    const completionLabel = searchReadyWhileFinishing ? 'search ready' : 'scan complete'
    const completionCopy = searchReadyWhileFinishing
      ? `${formatCount(uniqueMemories)} memories stored so far. Recurdo will finish ${formatCount(finishingCount)} remaining file${finishingCount === 1 ? '' : 's'} in the background.`
      : `${formatCount(uniqueMemories)} memories stored across ${formatCount(filesWatched || total || selectedTotalFiles)} files watched.`

    return (
      <div className="main-onboarding-shell">
        <div className="ob-done onboarding-panel onboarding-wide-panel">
          <ProgressDots step={2} />

          <p className="ob-section-label">{completionLabel}</p>
          <h1 className="onboarding-title">building your memory</h1>
          <p className="onboarding-copy onboarding-copy-left">
            {completionCopy}
          </p>

          {liveExamples.length ? (
            <div className="onboarding-example-list">
              {liveExamples.map((query) => (
                <div key={query} className="onboarding-example-chip">
                  try “{query}”
                </div>
              ))}
            </div>
          ) : null}

          <button type="button" className="ob-btn full" onClick={onOpenApp}>
            start searching →
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="main-onboarding-shell">
      <div className="ob-scanning onboarding-panel onboarding-wide-panel">
        <ProgressDots step={2} />

        <p className="ob-section-label">{sectionLabel}</p>
        <h1 className="onboarding-title">building your memory</h1>

        <div className="onboarding-progress-shell">
          <div className="onboarding-progress-bar">
            <div
              className={`onboarding-progress-fill${phase === 'discovering' ? ' discovering' : ''}`}
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          <div className="onboarding-progress-percent">
            {phase === 'discovering' ? `${formatCount(phaseFound)} found` : `${progressPercent}%`}
          </div>
        </div>

        <div className="onboarding-scan-lines">
          <div>{lineOne}</div>
          <div>{lineTwo}</div>
          <div>{lineThree}</div>
        </div>

        <div className="onboarding-ticker">
          {phase === 'indexing' && effectiveRecent.length ? (
            effectiveRecent.slice(0, 5).map((item, index) => (
              <div
                key={`${item.kind || 'indexed'}-${item.title}-${index}`}
                className="onboarding-ticker-row"
                style={{ animationDelay: `${index * 80}ms` }}
              >
                <span className="onboarding-ticker-check">✓</span>
                <span>
                  {item.kind === 'duplicate'
                    ? 'skipping duplicate — same content already indexed'
                    : item.title || item.filename || 'Indexed file'}
                </span>
              </div>
            ))
          ) : phase === 'checking' ? (
            <div className="onboarding-ticker-empty">
              Comparing quick file stats so Recurdo only hashes files that actually need reindexing.
            </div>
          ) : phase === 'discovering' ? (
            <div className="onboarding-ticker-empty">
              Walking the folders you chose and counting supported files first.
            </div>
          ) : (
            <div className="onboarding-ticker-empty">
              Preparing your first documents and PDFs now…
            </div>
          )}
        </div>

        {searchableNow ? (
          <button type="button" className="ob-text-link prominent" onClick={onOpenApp}>
            Recurdo is already searchable. Try it now →
          </button>
        ) : null}
      </div>
    </div>
  )
}

function Onboarding({
  status,
  items,
  watchedFolders,
  recentIndexedItems,
  onStartIndexing,
  onComplete,
  authHeaders,
  uiTestMode = false,
}) {
  const [step, setStep] = useState(() => readStoredStep())
  const [optionResults, setOptionResults] = useState({})
  const [customOptions, setCustomOptions] = useState([])
  const [selectedIds, setSelectedIds] = useState(() => new Set())
  const [loadingIds, setLoadingIds] = useState(new Set())
  const [selectionDirty, setSelectionDirty] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [scanStartedAt, setScanStartedAt] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  const hydratedSelectionRef = useRef(false)

  const hasExistingLibrary = Number((status?.unique_memories ?? status?.total_items) || 0) > 0

  const loadEstimate = useCallback(
    async (option) => {
      setLoadingIds((current) => new Set([...current, option.id]))
      try {
        const result = await estimateOption(option, authHeaders)
        setOptionResults((current) => ({ ...current, [option.id]: result }))
        return result
      } finally {
        setLoadingIds((current) => {
          const next = new Set(current)
          next.delete(option.id)
          return next
        })
      }
    },
    [authHeaders],
  )

  useEffect(() => {
    void Promise.all(
      DEFAULT_FOLDER_OPTIONS.map((option) => {
        if (optionResults[option.id]) {
          return optionResults[option.id]
        }
        return loadEstimate(option).catch((loadError) => {
          console.warn(`Failed to estimate ${option.label}`, loadError)
          return null
        })
      }),
    )
  }, [loadEstimate, optionResults])

  useEffect(() => {
    if (!customOptions.length) {
      return
    }

    void Promise.all(
      customOptions.map((option) => {
        if (optionResults[option.id]) {
          return optionResults[option.id]
        }
        return loadEstimate(option).catch((loadError) => {
          console.warn(`Failed to estimate ${option.label}`, loadError)
          return null
        })
      }),
    )
  }, [customOptions, loadEstimate, optionResults])

  useEffect(() => {
    if (selectionDirty || hydratedSelectionRef.current) {
      return
    }

    const nextSelected = new Set()
    const nextCustom = []

    for (const folder of watchedFolders || []) {
      const matchingDefault = DEFAULT_FOLDER_OPTIONS.find((option) =>
        matchesDefaultOption(option, folder.path),
      )

      if (matchingDefault) {
        nextSelected.add(matchingDefault.id)
        continue
      }

      const optionId = `custom:${normalizePath(folder.path)}`
      nextCustom.push({
        id: optionId,
        label: folder.label || folder.path.split('/').filter(Boolean).pop() || folder.path,
        subtitle: 'custom folder',
        paths: [folder.path],
        matches: [normalizePath(folder.path)],
        isCustom: true,
      })
      nextSelected.add(optionId)
    }

    if (nextCustom.length) {
      setCustomOptions((current) => {
        const existingIds = new Set(current.map((option) => option.id))
        return [...current, ...nextCustom.filter((option) => !existingIds.has(option.id))]
      })
    }

    setSelectedIds(nextSelected)
    hydratedSelectionRef.current = true
  }, [selectionDirty, watchedFolders])

  useEffect(() => {
    writeStoredStep(step)
  }, [step])

  useEffect(() => {
    if (step !== 2) {
      return undefined
    }

    const intervalId = window.setInterval(() => {
      setNow(Date.now())
    }, 1000)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [step])

  const mergedOptions = useMemo(() => {
    const defaults = DEFAULT_FOLDER_OPTIONS.map((option) => ({
      ...option,
      ...optionResults[option.id],
      path: optionResults[option.id]?.path || option.paths[0],
      displayPath: optionResults[option.id]?.displayPath || relativeHomePath(option.paths[0]),
      estimate: optionResults[option.id],
    }))

    const custom = customOptions.map((option) => ({
      ...option,
      ...optionResults[option.id],
      path: optionResults[option.id]?.path || option.paths[0],
      displayPath: optionResults[option.id]?.displayPath || relativeHomePath(option.paths[0]),
      estimate: optionResults[option.id],
    }))

    return [...defaults, ...custom]
  }, [customOptions, optionResults])

  const selectedOptions = useMemo(
    () => mergedOptions.filter((option) => selectedIds.has(option.id)),
    [mergedOptions, selectedIds],
  )

  const selectedTotalFiles = useMemo(
    () => selectedOptions.reduce((sum, option) => sum + Number(option?.estimate?.total || 0), 0),
    [selectedOptions],
  )

  const selectedTotalMinutes = useMemo(
    () => selectedOptions.reduce((sum, option) => sum + estimateMinutesForSelection(option), 0),
    [selectedOptions],
  )
  const selectedAlreadyAccounted = useMemo(
    () => selectedOptions.reduce((sum, option) => sum + Number(option?.estimate?.already_accounted_for || 0), 0),
    [selectedOptions],
  )
  const selectedBuckets = useMemo(
    () => selectedOptions.reduce((sum, option) => addEstimateBuckets(sum, option?.estimate), { ...EMPTY_SCAN_BUCKETS }),
    [selectedOptions],
  )

  const handleComplete = useCallback(async () => {
    await onComplete?.()
  }, [onComplete])

  const handleToggle = useCallback((optionId) => {
    setSelectionDirty(true)
    setError('')
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(optionId)) {
        next.delete(optionId)
      } else {
        next.add(optionId)
      }
      return next
    })
  }, [])

  const handleAddCustomFolder = useCallback(async () => {
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
        } catch (nativeError) {
          console.warn('Native picker failed, falling back to backend picker', nativeError)
        }
      }

      if (!picked?.path) {
        picked = await openFolderPicker(authHeaders)
      }

      if (!picked?.path) {
        return
      }

      const optionId = `custom:${normalizePath(picked.path)}`
      const defaultMatch = mergedOptions.find(
        (option) => normalizePath(option.path) === normalizePath(picked.path),
      )

      setSelectionDirty(true)
      setError('')

      if (defaultMatch) {
        setSelectedIds((current) => new Set([...current, defaultMatch.id]))
        return
      }

      const nextOption = {
        id: optionId,
        label: picked.label || picked.path.split('/').filter(Boolean).pop() || picked.path,
        subtitle: 'custom folder',
        paths: [picked.path],
        matches: [normalizePath(picked.path)],
        isCustom: true,
      }

      setCustomOptions((current) => {
        if (current.some((option) => option.id === optionId)) {
          return current
        }
        return [...current, nextOption]
      })
      const result = await loadEstimate(nextOption)
      if (!result?.exists) {
        setError('That folder could not be found or estimated on this Mac.')
        return
      }
      setSelectedIds((current) => new Set([...current, optionId]))
    } catch (customError) {
      console.warn('Unable to add custom folder', customError)
      setError(customError.message || 'Unable to add that folder right now.')
    }
  }, [authHeaders, loadEstimate, mergedOptions])

  const handleStart = useCallback(async () => {
    setIsSubmitting(true)
    setError('')

    try {
      await onStartIndexing(
        selectedOptions.map((option) => ({
          id: option.id,
          label: option.label,
          path: option.path,
        })),
      )
      setScanStartedAt(Date.now())
      setNow(Date.now())
      setStep(ONBOARDING_STEP_SCANNING)
    } catch (submitError) {
      console.warn('Unable to start onboarding indexing', submitError)
      setError(submitError.message || 'Unable to start indexing with those folders.')
    } finally {
      setIsSubmitting(false)
    }
  }, [onStartIndexing, selectedOptions])

  if (step === 0) {
    return (
      <WelcomeStep
        hasExistingLibrary={hasExistingLibrary}
        totalItems={status.unique_memories ?? status.total_items}
        watchedFolderCount={watchedFolders.length}
        onContinue={() => {
          setStep(ONBOARDING_STEP_FOLDER_SELECT)
        }}
        onSkipExisting={() => {
          void handleComplete()
        }}
      />
    )
  }

  if (step === 1) {
    return (
      <FolderSelectionStep
        options={mergedOptions}
        selectedIds={selectedIds}
        loadingIds={loadingIds}
        onToggle={handleToggle}
        onAddCustomFolder={() => void handleAddCustomFolder()}
        onStart={() => void handleStart()}
        onSkipForNow={() => {
          void handleComplete()
        }}
          selectedTotalFiles={selectedTotalFiles}
          selectedTotalMinutes={selectedTotalMinutes}
          selectedBuckets={{
            ...selectedBuckets,
            already_in_library: hasExistingLibrary ? Math.max(selectedBuckets.already_in_library, selectedAlreadyAccounted) : selectedBuckets.already_in_library,
          }}
          hasExistingLibrary={hasExistingLibrary}
          isSubmitting={isSubmitting}
          error={error}
        />
    )
  }

  return (
    <ScanningStep
      status={status}
      uniqueMemories={status.unique_memories ?? status.total_items}
      filesWatched={status.files_watched}
      selectedTotalFiles={selectedTotalFiles}
      selectedTotalMinutes={selectedTotalMinutes}
      hasExistingLibrary={hasExistingLibrary}
      recentIndexedItems={recentIndexedItems}
      items={items}
      scanStartedAt={scanStartedAt}
      now={now}
      onOpenApp={() => {
        void handleComplete()
      }}
      uiTestMode={uiTestMode}
    />
  )
}

export default Onboarding
