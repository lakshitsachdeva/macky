import { loadSession, refreshSession } from '../utils/session'
import { ensureBackendResponsive, getBackendHttpUrl, getBackendWsUrl, resetBackendOriginCache } from '../lib/backend'
import { createEventBatcher } from './events'
import { buildCaseReportPayload, buildForensicTimelineQuery } from './forensicPayload'

const DEFAULT_REQUEST_TIMEOUT_MS = 15000
const SEARCH_REQUEST_TIMEOUT_MS = 20000
const HEALTH_REQUEST_TIMEOUT_MS = 5000
const DEFAULT_TOTAL_REQUEST_TIMEOUT_MS = 20000
const SEARCH_TOTAL_REQUEST_TIMEOUT_MS = 26000
const HEALTH_TOTAL_REQUEST_TIMEOUT_MS = 7000
const FORENSIC_TIMELINE_REQUEST_TIMEOUT_MS = 15000
const FORENSIC_TIMELINE_TOTAL_REQUEST_TIMEOUT_MS = 20000
const FORENSIC_REPORT_REQUEST_TIMEOUT_MS = 60000
const FORENSIC_REPORT_TOTAL_REQUEST_TIMEOUT_MS = 75000

function withTotalTimeout(promise, timeoutMs, message = 'Request timed out') {
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => reject(new Error(message)), timeoutMs)

    Promise.resolve(promise)
      .then((value) => {
        window.clearTimeout(timeoutId)
        resolve(value)
      })
      .catch((error) => {
        window.clearTimeout(timeoutId)
        reject(error)
      })
  })
}

function withAuth(headers = {}, authHeaders = {}) {
  return {
    ...headers,
    ...authHeaders,
  }
}

function isRecoverableBackendError(error) {
  const message = String(error?.message || error || '').toLowerCase()
  return (
    error?.name === 'AbortError'
    || message.includes('request timed out')
    || message.includes('failed to fetch')
    || message.includes('networkerror')
    || message.includes('load failed')
    || message.includes('network request failed')
  )
}

function isRecoverableBackendStatus(status) {
  return [502, 503, 504].includes(Number(status))
}

async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs)

  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new Error('Request timed out')
    }
    throw error
  } finally {
    window.clearTimeout(timeoutId)
  }
}

async function resolveAuthHeaders(authHeaders = {}) {
  const saved = await loadSession()
  if (!saved?.token) {
    return authHeaders
  }

  const restAuthHeaders = { ...authHeaders }
  delete restAuthHeaders.Authorization
  return {
    ...restAuthHeaders,
    Authorization: `Bearer ${saved.token}`,
  }
}

async function request(path, options = {}, authHeaders = {}, requestOptions = {}) {
  const {
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    totalTimeoutMs = DEFAULT_TOTAL_REQUEST_TIMEOUT_MS,
    timeoutMessage = 'Request timed out',
  } = requestOptions

  return withTotalTimeout((async () => {
    let effectiveAuthHeaders = await resolveAuthHeaders(authHeaders)

    const send = async (forceRefresh = false) => {
      const base = await getBackendHttpUrl({ forceRefresh })
      return fetchWithTimeout(`${base}${path}`, {
        ...options,
        headers: withAuth(options.headers || {}, effectiveAuthHeaders),
      }, timeoutMs)
    }

    let response = null
    let recoveredOrigin = false

    try {
      response = await send(false)
    } catch (error) {
      if (!isRecoverableBackendError(error)) {
        throw error
      }

      resetBackendOriginCache()
      response = await send(true)
      recoveredOrigin = true
    }

    if (response && isRecoverableBackendStatus(response.status) && !recoveredOrigin) {
      resetBackendOriginCache()
      response = await send(true)
      recoveredOrigin = true
    }

    if (response.status === 401) {
      const refreshed = await refreshSession()
      if (refreshed?.token) {
        effectiveAuthHeaders = {
          ...effectiveAuthHeaders,
          Authorization: `Bearer ${refreshed.token}`,
        }

        try {
          response = await send(recoveredOrigin)
        } catch (error) {
          if (!isRecoverableBackendError(error)) {
            throw error
          }

          resetBackendOriginCache()
          response = await send(true)
        }
      }
    }

    return response
  })(), totalTimeoutMs, timeoutMessage)
}

export async function searchItems(query, limit = 10, authHeaders = {}, options = {}) {
  const explain = Boolean(options?.explain)
  const querySuffix = explain ? '?explain=true' : ''
  const body = { query, limit, explain }
  if (options?.caseId !== undefined && options?.caseId !== null && String(options.caseId).trim()) {
    body.case_id = options.caseId
  }
  const send = () => request(`/search${querySuffix}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }, authHeaders, {
    timeoutMs: SEARCH_REQUEST_TIMEOUT_MS,
    totalTimeoutMs: SEARCH_TOTAL_REQUEST_TIMEOUT_MS,
    timeoutMessage: 'Search request timed out',
  })

  let r
  try {
    r = await send()
  } catch (error) {
    if (!isRecoverableBackendError(error)) {
      throw error
    }
    await ensureBackendResponsive({ forceRefresh: true })
    r = await send()
  }

  if (isRecoverableBackendStatus(r?.status)) {
    await ensureBackendResponsive({ forceRefresh: true })
    r = await send()
  }

  if (!r.ok) {
    throw new Error(await getErrorMessage(r, "Unable to search memories"))
  }
  return r.json()
}

function casePath(caseId, suffix = '') {
  return `/cases/${encodeURIComponent(String(caseId))}${suffix}`
}

export async function getCases(authHeaders = {}) {
  const response = await request('/cases', {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load cases'))
  }
  return response.json()
}

export async function createCase(payload = {}, authHeaders = {}) {
  const response = await request('/cases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: payload.name,
      ...(payload.description ? { description: payload.description } : {}),
      ...(payload.notes ? { notes: payload.notes } : {}),
    }),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to create case'))
  }
  return response.json()
}

export async function getCase(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId), {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load case'))
  }
  return response.json()
}

export async function updateCase(caseId, fields = {}, authHeaders = {}) {
  const response = await request(casePath(caseId), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to update case'))
  }
  return response.json()
}

export async function deleteCase(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId), { method: 'DELETE' }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to delete case'))
  }
  return response.json()
}

export async function importCaseEvidence(caseId, paths = [], authHeaders = {}, options = {}) {
  const response = await request(casePath(caseId, '/evidence/import'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      paths,
      hash_algorithm: options.hashAlgorithm || 'sha256',
    }),
  }, authHeaders, {
    timeoutMs: 60000,
    totalTimeoutMs: 90000,
    timeoutMessage: 'Evidence import timed out',
  })
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to import evidence'))
  }
  return response.json()
}

export async function getCaseEvidence(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId, '/evidence'), {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load evidence manifest'))
  }
  return response.json()
}

export async function getCaseTimeline(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId, '/timeline'), {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load custody timeline'))
  }
  return response.json()
}

export async function getForensicTimeline(caseId, options = {}, authHeaders = {}) {
  const suffix = buildForensicTimelineQuery(options)
  const response = await request(
    casePath(caseId, `/forensic-timeline${suffix}`),
    {},
    authHeaders,
    {
      timeoutMs: FORENSIC_TIMELINE_REQUEST_TIMEOUT_MS,
      totalTimeoutMs: FORENSIC_TIMELINE_TOTAL_REQUEST_TIMEOUT_MS,
      timeoutMessage: 'Forensic timeline request timed out',
    },
  )
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load forensic timeline'))
  }
  return response.json()
}

export async function generateCaseReport(caseId, payload = {}, authHeaders = {}) {
  const body = buildCaseReportPayload(payload)
  const response = await request(casePath(caseId, '/report'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, authHeaders, {
    timeoutMs: FORENSIC_REPORT_REQUEST_TIMEOUT_MS,
    totalTimeoutMs: FORENSIC_REPORT_TOTAL_REQUEST_TIMEOUT_MS,
    timeoutMessage: 'Case report request timed out',
  })
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to generate case report'))
  }
  return response.json()
}

export async function exportCaseManifest(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId, '/export'), {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to export case manifest'))
  }
  return response.json()
}

export async function verifyCaseEvidence(caseId, authHeaders = {}) {
  const response = await request(casePath(caseId, '/verify'), {
    method: 'POST',
  }, authHeaders, {
    timeoutMs: 60000,
    totalTimeoutMs: 90000,
    timeoutMessage: 'Evidence verification timed out',
  })
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to verify evidence'))
  }
  return response.json()
}

/**
 * Conversational Q&A over the user's memory.
 *
 * Returns: { question, answer: { text, provider, confidence }, sources: [...] }
 *
 * The backend is guaranteed not to 500 even if the LLM provider is unavailable
 * — it falls back to a deterministic top-3 list. The caller should render
 * sources regardless; only `answer.text` and `answer.confidence` change shape.
 */
export async function askQuestion(question, options = {}, authHeaders = {}) {
  const body = {
    question,
    limit: options.limit ?? 6,
    session_id: options.sessionId ?? null,
    ...(options.caseId ? { case_id: options.caseId } : {}),
    allow_model: options.allowModel !== false,
    conversation_history: options.conversationHistory ?? null,
  }
  const send = () => request(
    '/ask',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    authHeaders,
    {
      // /ask is slower than /search because it calls the LLM. The backend
      // already has a 30s internal cap on generation; front the network with
      // a generous-but-bounded timeout so a stuck Ollama doesn't hang the UI.
      timeoutMs: 40000,
      totalTimeoutMs: 45000,
      timeoutMessage: 'Ask request timed out',
    },
  )

  let r
  try {
    r = await send()
  } catch (error) {
    if (!isRecoverableBackendError(error)) throw error
    await ensureBackendResponsive({ forceRefresh: true })
    r = await send()
  }

  if (isRecoverableBackendStatus(r?.status)) {
    await ensureBackendResponsive({ forceRefresh: true })
    r = await send()
  }

  if (!r.ok) {
    throw new Error(await getErrorMessage(r, 'Unable to answer question'))
  }
  return r.json()
}


/**
 * Proactive "Now" feed — items the user probably wants, no query required.
 *
 * Returns: { items: [...], count, generated_at, cached }
 *
 * The backend TTL-caches this at 20s per user. Pass `refresh: true` to bust
 * the cache (e.g. right after the user dismisses a card, so the dismissed
 * item doesn't blink back from cache).
 */
export async function getNowFeed(options = {}, authHeaders = {}) {
  const params = new URLSearchParams()
  if (options.limit) params.set('limit', String(options.limit))
  if (options.refresh) params.set('refresh', 'true')
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const response = await request(`/now${suffix}`, {}, authHeaders, {
    timeoutMs: 8000,
    totalTimeoutMs: 10000,
    timeoutMessage: 'Now feed request timed out',
  })
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load Now feed'))
  }
  return response.json()
}


export async function getStatus(authHeaders = {}) {
  const response = await request('/index/status', {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to load index status"))
  }
  return response.json()
}

export async function getHealth() {
  const response = await request('/health', {}, {}, {
    timeoutMs: HEALTH_REQUEST_TIMEOUT_MS,
    totalTimeoutMs: HEALTH_TOTAL_REQUEST_TIMEOUT_MS,
    timeoutMessage: 'Backend health check timed out',
  })
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to reach Recurdo backend"))
  }
  return response.json()
}

export async function getContext(authHeaders = {}, options = {}) {
  const params = new URLSearchParams()
  if (options.currentItemId) {
    params.set('current_item_id', options.currentItemId)
  }
  if (options.currentQuery) {
    params.set('current_query', options.currentQuery)
  }
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const response = await request(`/context${suffix}`, {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load proactive context'))
  }
  return response.json()
}

export async function getItems(page = 1, limit = 10, authHeaders = {}) {
  const response = await request(`/items?page=${page}&limit=${limit}`, {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to load indexed files"))
  }
  return response.json()
}

export async function triggerScan(authHeaders = {}) {
  const response = await request('/index/trigger', { method: "POST" }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to start indexing"))
  }
  return response.json()
}

export async function triggerCognitiveBootstrap(authHeaders = {}, options = {}) {
  const response = await request('/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reset: options.reset ?? false,
      dry_run: options.dryRun ?? false,
    }),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to start cognitive bootstrap'))
  }
  return response.json()
}

export async function configureLlm(updates = {}, authHeaders = {}) {
  const response = await request('/llm/configure', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to save settings'))
  }
  return response.json()
}

export async function getGmailStatus(authHeaders = {}) {
  const response = await request('/gmail/status', {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to load Gmail status'))
  }
  return response.json()
}

export async function connectGmail(authHeaders = {}) {
  const response = await request('/gmail/connect', {
    method: 'POST',
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to start Gmail authentication'))
  }
  return response.json()
}

export async function triggerGmailSync(authHeaders = {}, options = {}) {
  const response = await request('/gmail/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      full_sync: options.fullSync ?? false,
      historical_days: options.historicalDays ?? null,
      require_attachments: options.requireAttachments ?? false,
      max_messages: options.maxMessages ?? null,
    }),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to start Gmail sync'))
  }
  return response.json()
}

export async function stopGmailSync(authHeaders = {}) {
  const response = await request('/gmail/stop', {
    method: 'POST',
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to stop Gmail sync'))
  }
  return response.json()
}

export async function cleanupGmail(authHeaders = {}, options = {}) {
  const response = await request('/gmail/cleanup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reset_history_cursor: options.resetHistoryCursor ?? false,
    }),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to clear imported Gmail memories'))
  }
  return response.json()
}

export async function disconnectGmail(authHeaders = {}) {
  const response = await request('/gmail/connection', {
    method: 'DELETE',
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to disconnect Gmail'))
  }
  return response.json()
}

export async function discoverFolders(authHeaders = {}) {
  const response = await request('/folders/discover', {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to discover folders"))
  }
  return response.json()
}

export async function estimateFolder(path, authHeaders = {}) {
  const response = await request('/folders/estimate', {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path}),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to estimate folder"))
  }
  return response.json()
}

export async function getWatchedFolders(authHeaders = {}) {
  const response = await request('/folders/watched', {}, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to load watched folders"))
  }
  return response.json()
}

export async function addWatchedFolder(path, label, authHeaders = {}, options = {}) {
  const response = await request('/folders/add', {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      path,
      label,
      start_scan: options.startScan ?? true,
    }),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to add folder"))
  }
  return response.json()
}

export async function removeWatchedFolder(path, authHeaders = {}) {
  const response = await request('/folders/remove', {
    method: "DELETE",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path}),
  }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to remove folder"))
  }
  return response.json()
}

export async function openFolderPicker(authHeaders = {}) {
  const response = await request('/folders/open-picker', { method: "POST" }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to open folder picker"))
  }
  return response.json()
}

export async function logOpen(itemId, authHeaders = {}) {
  return request(`/items/${itemId}/log-open`, { method: "POST" }, authHeaders)
}

export async function openItem(itemId, authHeaders = {}) {
  const response = await request(`/items/${itemId}/open`, { method: "POST" }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to open file"))
  }
  return response.json()
}

export async function revealItem(itemId, authHeaders = {}) {
  const response = await request(`/items/${itemId}/reveal`, { method: "POST" }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to reveal file"))
  }
  return response.json()
}

/**
 * POST a batch of interaction events to /events/batch.
 *
 * Used by the shared batcher below. Not exported for direct use — call
 * `logInteractionEvent(event)` instead, which buffers + flushes on a timer.
 */
async function postInteractionEventBatch(payload, authHeaders = {}) {
  const response = await request(
    '/events/batch',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
    authHeaders,
    { timeoutMs: 8000, totalTimeoutMs: 10000 },
  )
  if (!response.ok) {
    // Don't throw a rich error — the batcher just needs to know it failed so
    // it can re-queue and back off.
    throw new Error(`events batch rejected (${response.status})`)
  }
  return response.json()
}

const _sharedEventBatcher = createEventBatcher({
  sendBatch: (payload) => postInteractionEventBatch(payload),
})

/**
 * Public API: fire-and-forget a single interaction event. Safe to call from
 * any render path — batching and retry are handled internally.
 *
 *   logInteractionEvent(searchEvent('my query'))
 *   logInteractionEvent(clickEvent({ itemId, query, rankPosition: 3 }))
 */
export function logInteractionEvent(event) {
  try {
    _sharedEventBatcher.log(event)
  } catch (err) {
    // Never let event logging break a render. Worst case we lose the event.
    if (typeof console !== 'undefined') console.debug('logInteractionEvent failed:', err)
  }
}

/** Force-flush pending events. Rarely needed — used by `/ask` and similar
 * surfaces that want click→ask context to reach the backend synchronously. */
export async function flushInteractionEvents() {
  try { await _sharedEventBatcher.flush() } catch (err) { void err }
}

// Re-export the event builders so callers have one import path.
export { searchEvent, clickEvent, openEvent, dwellEvent, copyEvent, dismissEvent } from './events'

export async function clearAllData(authHeaders = {}) {
  const response = await request('/items/all', { method: "DELETE" }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, "Unable to clear indexed data"))
  }
  return response.json()
}

export async function deleteItem(itemId, authHeaders = {}) {
  const response = await request(`/items/${itemId}`, { method: 'DELETE' }, authHeaders)
  if (!response.ok) {
    throw new Error(await getErrorMessage(response, 'Unable to remove memory'))
  }
  return response.json()
}

function normalizeSocketEvent(event) {
  if (event?.type === 'scan_phase') {
    return {
      found: 0,
      checked: 0,
      total: 0,
      new_found: 0,
      message: '',
      phase: '',
      ...event,
    }
  }

  return event
}

class ReconnectingWebSocket {
  constructor(getUrl, onMessage, onStatusChange) {
    this.getUrl = getUrl
    this.onMessage = onMessage
    this.onStatusChange = onStatusChange
    this.ws = null
    this.attempt = 0
    this.maxDelay = 30000
    this.destroyed = false
    this.reconnectTimer = null
    this.connectSerial = 0
    void this.connect()
  }

  async connect() {
    if (this.destroyed) {
      return
    }

    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    if (this.ws && (
      this.ws.readyState === WebSocket.CONNECTING
      || this.ws.readyState === WebSocket.OPEN
    )) {
      return
    }

    const connectId = this.connectSerial + 1
    this.connectSerial = connectId
    this.onStatusChange?.('connecting')

    let url = ''
    try {
      url = await this.getUrl({ forceRefresh: this.attempt > 0 })
    } catch (error) {
      if (!this.destroyed) {
        console.warn('WS origin resolve failed', error)
        this.onStatusChange?.('disconnected')
        this.scheduleReconnect()
      }
      return
    }

    if (this.destroyed || connectId !== this.connectSerial) {
      return
    }

    this.ws = new WebSocket(url)

    this.ws.onopen = () => {
      this.attempt = 0
      this.onStatusChange?.('connected')
    }

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        this.onMessage(normalizeSocketEvent(data))
      } catch (error) {
        console.warn('WS parse error', error)
      }
    }

    this.ws.onclose = () => {
      this.ws = null
      if (this.destroyed) {
        return
      }
      this.onStatusChange?.('disconnected')
      this.scheduleReconnect()
    }

    this.ws.onerror = (error) => {
      console.warn('WS error', error)
    }
  }

  scheduleReconnect() {
    if (this.destroyed || this.reconnectTimer) {
      return
    }

    const delay = Math.min(1000 * (2 ** this.attempt), this.maxDelay)
    this.attempt += 1
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }

  destroy() {
    this.destroyed = true

    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    if (this.ws && (
      this.ws.readyState === WebSocket.CONNECTING
      || this.ws.readyState === WebSocket.OPEN
    )) {
      this.ws.close()
    }

    this.ws = null
  }
}

export function connectWebSocket(onMessage, onStatusChange) {
  let websocket = null
  let destroyed = false

  void (async () => {
    if (destroyed) {
      return
    }
    websocket = new ReconnectingWebSocket(
      async ({ forceRefresh = false } = {}) => `${await getBackendWsUrl({ forceRefresh })}/ws/events`,
      onMessage,
      onStatusChange,
    )
  })()

  return {
    destroy() {
      destroyed = true
      websocket?.destroy()
    },
  }
}

async function getErrorMessage(response, fallback) {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string' && body.detail.trim()) {
      return body.detail
    }
    if (body?.detail && typeof body.detail === 'object' && typeof body.detail.message === 'string') {
      return body.detail.message
    }
    if (typeof body?.message === 'string' && body.message.trim()) {
      return body.message
    }
    return fallback
  } catch {
    return fallback
  }
}
