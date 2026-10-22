import { isTauri } from '@tauri-apps/api/core'
import { AUTH_CONFIGURED, SUPABASE_ANON_KEY, SUPABASE_URL } from '../lib/supabaseConfig'
import { invokeIfTauri } from './tauri'
const SESSION_KEY = 'recurdo_session'
const PENDING_AUTH_KEY = 'recurdo_pending_auth'
const PENDING_AUTH_MAX_AGE_MS = 15 * 60 * 1000

export { AUTH_CONFIGURED }

let refreshInFlight = null

function nowIso() {
  return new Date().toISOString()
}

function randomState() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `auth-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`
}

function readPendingAuthState() {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(PENDING_AUTH_KEY) || 'null')
    if (!parsed || typeof parsed !== 'object') {
      return null
    }

    const state = String(parsed.state || '').trim()
    const createdAt = String(parsed.created_at || parsed.createdAt || '').trim()
    if (!state || !createdAt) {
      return null
    }

    const createdAtMs = Date.parse(createdAt)
    if (!Number.isFinite(createdAtMs)) {
      return null
    }

    return {
      state,
      createdAt,
      createdAtMs,
    }
  } catch {
    return null
  }
}

export function beginPendingAuth() {
  if (typeof window === 'undefined') {
    return ''
  }

  const state = randomState()
  window.localStorage.setItem(
    PENDING_AUTH_KEY,
    JSON.stringify({
      state,
      created_at: nowIso(),
    }),
  )
  return state
}

export function clearPendingAuth() {
  if (typeof window === 'undefined') {
    return
  }
  window.localStorage.removeItem(PENDING_AUTH_KEY)
}

export function consumePendingAuth(expectedState = '') {
  const pending = readPendingAuthState()
  if (!pending) {
    return false
  }

  const expired = Date.now() - pending.createdAtMs > PENDING_AUTH_MAX_AGE_MS
  if (expired) {
    clearPendingAuth()
    return false
  }

  if (expectedState && pending.state !== String(expectedState).trim()) {
    return false
  }

  if (!expectedState) {
    return false
  }

  clearPendingAuth()
  return true
}

function normalizeSession(value) {
  if (!value || typeof value !== 'object') {
    return null
  }

  const token = String(value.token || value.access_token || '').trim()
  if (!token) {
    return null
  }

  return {
    token,
    refreshToken: String(value.refresh_token || value.refreshToken || '').trim(),
    email: String(value.email || value.userEmail || '').trim(),
    savedAt: String(value.saved_at || value.savedAt || '').trim(),
  }
}

export async function loadSession() {
  if (isTauri()) {
    const saved = await invokeIfTauri('load_session')
    return normalizeSession(saved)
  }

  try {
    return normalizeSession(JSON.parse(window.localStorage.getItem(SESSION_KEY) || 'null'))
  } catch {
    return null
  }
}

export async function saveSession({ token, refreshToken = '', email = '' }) {
  if (!token) {
    return
  }

  if (isTauri()) {
    await invokeIfTauri('save_session', {
      token,
      userEmail: email,
      refreshToken: refreshToken || null,
    })
    return
  }

  window.localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      token,
      refresh_token: refreshToken,
      email,
      saved_at: new Date().toISOString(),
    }),
  )
}

export async function clearSession() {
  clearPendingAuth()

  if (isTauri()) {
    await invokeIfTauri('clear_session')
    return
  }

  window.localStorage.removeItem(SESSION_KEY)
}

async function requestRefresh(session) {
  if (!AUTH_CONFIGURED || !session?.refreshToken) {
    return null
  }

  const response = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      apikey: SUPABASE_ANON_KEY,
    },
    body: JSON.stringify({
      refresh_token: session.refreshToken,
    }),
  })

  if (!response.ok) {
    return null
  }

  const payload = await response.json()
  return normalizeSession({
    token: payload.access_token,
    refresh_token: payload.refresh_token || session.refreshToken,
    email: payload.user?.email || session.email,
    saved_at: new Date().toISOString(),
  })
}

export async function refreshSession(existingSession = null) {
  if (refreshInFlight) {
    return refreshInFlight
  }

  refreshInFlight = (async () => {
    const baseSession = existingSession || (await loadSession())
    const refreshed = await requestRefresh(baseSession)
    if (!refreshed?.token) {
      return null
    }

    await saveSession(refreshed)
    return refreshed
  })()

  try {
    return await refreshInFlight
  } finally {
    refreshInFlight = null
  }
}
