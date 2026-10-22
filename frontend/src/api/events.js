/**
 * Client-side interaction event batcher.
 *
 * The app emits an event for every search, click, open, dwell, copy, and
 * dismissal. Sending each one as a POST would saturate the backend (and
 * burn through the event-loop budget we just bought back with the Supabase-
 * off-event-loop fix). This module batches events in memory, flushes every
 * FLUSH_INTERVAL_MS or when the batch hits BATCH_SIZE_LIMIT, and also
 * flushes on window 'pagehide' / 'visibilitychange' so we don't lose trailing
 * events when the user closes the app.
 *
 * Failure mode: if the backend is unreachable, events queue in-memory up to
 * QUEUE_OVERFLOW_LIMIT and then drop oldest-first. We deliberately do NOT
 * persist to localStorage for this v1 because (a) events are low-value once
 * more than a few minutes stale, and (b) localStorage cross-tab sync has
 * bitten us before in this stack. A durable queue is a Phase 5 concern, not
 * a Phase 1 concern.
 */

const FLUSH_INTERVAL_MS = 5000
const BATCH_SIZE_LIMIT = 50
const QUEUE_OVERFLOW_LIMIT = 500
const MIN_BACKOFF_MS = 1000
const MAX_BACKOFF_MS = 60000

// Session ID ties together events within one app-open. Regenerated on page
// load — good enough for retrieval personalization; we don't need a stable
// cross-session ID.
let _sessionId = null

function getSessionId() {
  if (_sessionId) return _sessionId
  // crypto.randomUUID exists in every browser the Tauri webview supports.
  _sessionId = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID()
    : `sess-${Math.random().toString(36).slice(2)}-${Date.now()}`
  return _sessionId
}

/**
 * A standalone batcher. Kept as a closure so we can have one shared instance
 * (exported below) but also construct isolated instances in tests.
 */
export function createEventBatcher({ sendBatch, now = () => Date.now() }) {
  let queue = []
  let timerId = null
  let flushing = false
  let backoffMs = MIN_BACKOFF_MS
  let listenersAttached = false

  function scheduleFlush() {
    if (timerId !== null) return
    timerId = setTimeout(() => {
      timerId = null
      void flush()
    }, FLUSH_INTERVAL_MS)
  }

  function cancelFlush() {
    if (timerId !== null) {
      clearTimeout(timerId)
      timerId = null
    }
  }

  function enqueue(event) {
    if (!event || !event.event_type) return
    const enriched = {
      session_id: event.session_id || getSessionId(),
      client_timestamp: event.client_timestamp || new Date(now()).toISOString(),
      ...event,
    }
    queue.push(enriched)

    // Drop oldest first when we blow past the overflow cap.
    if (queue.length > QUEUE_OVERFLOW_LIMIT) {
      queue.splice(0, queue.length - QUEUE_OVERFLOW_LIMIT)
    }

    if (queue.length >= BATCH_SIZE_LIMIT) {
      void flush()
    } else {
      scheduleFlush()
    }
  }

  async function flush() {
    if (flushing || queue.length === 0) return
    flushing = true
    cancelFlush()

    // Swap the queue so events emitted during network I/O go into the next
    // batch instead of getting dropped if this batch fails.
    const batch = queue
    queue = []

    try {
      await sendBatch({
        events: batch,
        session_id: getSessionId(),
      })
      backoffMs = MIN_BACKOFF_MS
    } catch {
      // Put the batch back at the head of the queue; apply exponential
      // backoff so a flapping backend doesn't get hammered.
      queue = batch.concat(queue).slice(-QUEUE_OVERFLOW_LIMIT)
      setTimeout(() => {
        if (queue.length > 0) void flush()
      }, backoffMs)
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS)
    } finally {
      flushing = false
      if (queue.length > 0) scheduleFlush()
    }
  }

  function attachLifecycleListeners() {
    if (listenersAttached || typeof window === 'undefined') return
    listenersAttached = true
    // Flush on tab hide so we don't lose the tail. 'pagehide' is the
    // reliable signal across Safari/Chrome; 'visibilitychange' catches
    // backgrounding on macOS.
    window.addEventListener('pagehide', () => { void flush() })
    window.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') void flush()
    })
  }

  attachLifecycleListeners()

  return {
    log: enqueue,
    flush,
    _queueLength: () => queue.length,
    _testResetBackoff: () => { backoffMs = MIN_BACKOFF_MS },
  }
}

// ── Event builders (prefer over raw objects — type-safe-ish and less typo-prone)

export function searchEvent(query) {
  return { event_type: 'search', query }
}

export function clickEvent({ itemId, query, rankPosition }) {
  return {
    event_type: 'click',
    item_id: itemId,
    query,
    rank_position: rankPosition,
  }
}

export function openEvent({ itemId, query = null }) {
  return { event_type: 'open', item_id: itemId, query }
}

export function dwellEvent({ itemId, dwellMs, query = null }) {
  return {
    event_type: 'dwell',
    item_id: itemId,
    dwell_ms: dwellMs,
    query,
  }
}

export function copyEvent({ itemId, query = null }) {
  return { event_type: 'copy', item_id: itemId, query }
}

export function dismissEvent({ itemId, query = null, rankPosition = null }) {
  return {
    event_type: 'dismiss',
    item_id: itemId,
    query,
    rank_position: rankPosition,
  }
}
