import { isTauri } from '@tauri-apps/api/core'
import { invokeIfTauri } from '../utils/tauri'

const DEFAULT_BACKEND_HOST = '127.0.0.1'
const DEFAULT_BACKEND_PORT = String(import.meta.env.VITE_RECURDO_BACKEND_PORT || '8000')
const BACKEND_ORIGIN_RESOLVE_TIMEOUT_MS = 3000
const BACKEND_HEALTHCHECK_TIMEOUT_MS = 4000

let backendOriginPromise = null

function withTimeout(promise, timeoutMs, message) {
  return new Promise((resolve, reject) => {
    const timerId = window.setTimeout(() => {
      reject(new Error(message))
    }, timeoutMs)

    Promise.resolve(promise)
      .then((value) => {
        window.clearTimeout(timerId)
        resolve(value)
      })
      .catch((error) => {
        window.clearTimeout(timerId)
        reject(error)
      })
  })
}

function defaultOrigin() {
  return {
    host: DEFAULT_BACKEND_HOST,
    port: DEFAULT_BACKEND_PORT,
    httpUrl: `http://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`,
    wsUrl: `ws://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`,
  }
}

function normalizeOrigin(payload) {
  if (!payload || !payload.host || !payload.port) {
    return defaultOrigin()
  }

  const host = String(payload.host)
  const port = String(payload.port)

  return {
    host,
    port,
    httpUrl: String(payload.http_url || `http://${host}:${port}`),
    wsUrl: String(payload.ws_url || `ws://${host}:${port}`),
  }
}

export function resetBackendOriginCache() {
  backendOriginPromise = null
}

export async function getBackendOrigin(options = {}) {
  if (options.forceRefresh) {
    resetBackendOriginCache()
  }

  if (!backendOriginPromise) {
    backendOriginPromise = (async () => {
      if (!isTauri()) {
        return defaultOrigin()
      }

      try {
        const origin = await withTimeout(
          invokeIfTauri('get_backend_origin'),
          BACKEND_ORIGIN_RESOLVE_TIMEOUT_MS,
          'Backend origin resolve timed out',
        )
        return normalizeOrigin(origin)
      } catch (error) {
        // Do not pin the app to the fallback port if the Tauri bridge
        // isn't ready yet. Let the next attempt ask Tauri again.
        backendOriginPromise = null
        console.warn('Unable to resolve backend origin from Tauri, using default port', error)
        return defaultOrigin()
      }
    })()
  }

  return backendOriginPromise
}

export async function getBackendHttpUrl(options = {}) {
  return (await getBackendOrigin(options)).httpUrl
}

export async function getBackendWsUrl(options = {}) {
  return (await getBackendOrigin(options)).wsUrl
}

async function pingHealth(httpUrl) {
  // Prefer the lightweight readiness probe. Older builds might not have it,
  // so fall back to /health if we see a 404.
  const ready = await withTimeout(
    fetch(`${httpUrl}/ready`),
    BACKEND_HEALTHCHECK_TIMEOUT_MS,
    'Backend readiness check timed out',
  )
  if (ready.status !== 404) {
    if (!ready.ok) {
      throw new Error(`Backend readiness check failed with status ${ready.status}`)
    }
    return ready
  }

  const response = await withTimeout(
    fetch(`${httpUrl}/health`),
    BACKEND_HEALTHCHECK_TIMEOUT_MS,
    'Backend health check timed out',
  )
  if (!response.ok) {
    throw new Error(`Backend health check failed with status ${response.status}`)
  }
  return response
}

export async function ensureBackendResponsive(options = {}) {
  try {
    const origin = await getBackendOrigin(options)
    await pingHealth(origin.httpUrl)
    return origin
  } catch {
    resetBackendOriginCache()
    const refreshed = await getBackendOrigin({ forceRefresh: true })
    await pingHealth(refreshed.httpUrl)
    return refreshed
  }
}

export const BACKEND_HOST = DEFAULT_BACKEND_HOST
export const BACKEND_PORT = DEFAULT_BACKEND_PORT
