import { verifyAccessSession } from '../lib/server/admin.js'
import {
  enforceRateLimit,
  rejectUnexpectedFields,
  setSecurityHeaders,
} from '../lib/server/security.js'

const AUTH_USER_CACHE_TTL_MS = 45_000
const authUserCache = new Map()

function getBearerToken(request) {
  const authorization = request.headers.authorization
  if (typeof authorization !== 'string') {
    return ''
  }

  const [scheme, token] = authorization.split(' ')
  if (scheme?.toLowerCase() !== 'bearer') {
    return ''
  }

  return token?.trim() || ''
}

function normalizeAccess(payload) {
  return {
    has_access: Boolean(payload?.has_access),
    tier: payload?.tier || 'unknown',
    days_remaining: Number.isFinite(payload?.days_remaining) ? payload.days_remaining : 0,
    expires_at: payload?.expires_at || null,
    is_blocked: Boolean(payload?.is_blocked),
  }
}

function decodeJwtPayload(token) {
  const payload = token.split('.')[1]
  if (!payload) {
    return null
  }

  try {
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padding = '='.repeat((4 - (normalized.length % 4)) % 4)
    return JSON.parse(Buffer.from(`${normalized}${padding}`, 'base64').toString('utf8'))
  } catch {
    return null
  }
}

function getUserCacheExpiry(token) {
  const defaultExpiry = Date.now() + AUTH_USER_CACHE_TTL_MS
  const claims = decodeJwtPayload(token)
  const tokenExpMs = Number(claims?.exp) * 1000
  if (!Number.isFinite(tokenExpMs)) {
    return defaultExpiry
  }

  return Math.min(defaultExpiry, tokenExpMs)
}

function getCachedUser(token) {
  const cached = authUserCache.get(token)
  if (!cached) {
    return null
  }

  if (Date.now() >= cached.expiresAt) {
    authUserCache.delete(token)
    return null
  }

  return cached.user
}

function setCachedUser(token, user) {
  const expiresAt = getUserCacheExpiry(token)
  if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    return
  }

  authUserCache.set(token, {
    user,
    expiresAt,
  })
}

export default async function handler(request, response) {
  setSecurityHeaders(response, 'GET,OPTIONS')

  if (request.method === 'OPTIONS') {
    response.status(200).end()
    return
  }

  if (request.method !== 'GET') {
    response.status(405).json({ error: 'Method not allowed.' })
    return
  }

  try {
    const url = new URL(request.url || '/api/access-status', 'http://localhost')
    rejectUnexpectedFields(Object.fromEntries(url.searchParams.entries()), [])
  } catch (error) {
    response.status(400).json({ error: error instanceof Error ? error.message : 'Invalid access request.' })
    return
  }

  if (!enforceRateLimit(request, response, { name: 'access-status', ipLimit: 120, userLimit: 60 })) {
    return
  }

  const token = getBearerToken(request)
  if (!token) {
    response.status(401).json({ error: 'Missing bearer token.' })
    return
  }

  try {
    let user = getCachedUser(token)
    let access = null

    if (!user) {
      const verified = await verifyAccessSession(request)
      access = verified.access
      const { user: verifiedUser } = verified
      user = verifiedUser
      setCachedUser(token, user)
    }

    if (!access) {
      const verified = await verifyAccessSession(request)
      access = verified.access
    }

    response.status(200).json({
      authenticated: true,
      user: {
        id: user.id,
        email: user.email || '',
        name: user.user_metadata?.full_name || '',
      },
      access: normalizeAccess(access),
    })
  } catch (error) {
    const status = error?.statusCode || 500
    response.status(status).json({
      error: status === 401
        ? 'Invalid session.'
        : 'Unable to verify access right now.',
      detail: error instanceof Error ? error.message : String(error),
      access: normalizeAccess(error?.access),
    })
  }
}
