const WINDOW_MS_DEFAULT = 60_000
const buckets = new Map()

function now() {
  return Date.now()
}

function pruneBucket(entries, cutoff) {
  while (entries.length && entries[0] <= cutoff) {
    entries.shift()
  }
}

export function setSecurityHeaders(response, methods = 'GET,POST,OPTIONS') {
  response.setHeader('Access-Control-Allow-Origin', '*')
  response.setHeader('Access-Control-Allow-Headers', 'Authorization,Content-Type')
  response.setHeader('Access-Control-Allow-Methods', methods)
  response.setHeader('Cache-Control', 'no-store')
  response.setHeader('X-Content-Type-Options', 'nosniff')
  response.setHeader('Referrer-Policy', 'no-referrer')
}

export function getClientIp(request) {
  const forwardedFor = request.headers['x-forwarded-for']
  if (typeof forwardedFor === 'string' && forwardedFor.trim()) {
    return forwardedFor.split(',')[0].trim().slice(0, 80)
  }

  const realIp = request.headers['x-real-ip']
  if (typeof realIp === 'string' && realIp.trim()) {
    return realIp.trim().slice(0, 80)
  }

  return String(request.socket?.remoteAddress || 'unknown').slice(0, 80)
}

export function bearerSubject(request) {
  const authorization = request.headers.authorization
  if (typeof authorization !== 'string') {
    return ''
  }

  const [scheme, token] = authorization.split(' ')
  if (scheme?.toLowerCase() !== 'bearer' || !token) {
    return ''
  }

  try {
    const payload = token.split('.')[1] || ''
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padding = '='.repeat((4 - (normalized.length % 4)) % 4)
    const claims = JSON.parse(Buffer.from(`${normalized}${padding}`, 'base64').toString('utf8'))
    return String(claims.sub || claims.user_id || claims.email || '').slice(0, 160)
  } catch {
    return ''
  }
}

function hitLimit(key, limit, windowMs) {
  const current = now()
  const cutoff = current - windowMs
  const entries = buckets.get(key) || []
  pruneBucket(entries, cutoff)
  if (entries.length >= limit) {
    const retryAfterSeconds = Math.max(1, Math.ceil((windowMs - (current - entries[0])) / 1000))
    buckets.set(key, entries)
    return retryAfterSeconds
  }
  entries.push(current)
  buckets.set(key, entries)

  if (buckets.size > 5000) {
    for (const [bucketKey, bucketEntries] of buckets.entries()) {
      pruneBucket(bucketEntries, cutoff)
      if (!bucketEntries.length) {
        buckets.delete(bucketKey)
      }
    }
  }

  return 0
}

export function enforceRateLimit(request, response, {
  name,
  ipLimit = 120,
  userLimit = 60,
  windowMs = WINDOW_MS_DEFAULT,
} = {}) {
  const route = name || request.url || 'unknown'
  const ipRetry = hitLimit(`ip:${getClientIp(request)}:${route}`, ipLimit, windowMs)
  if (ipRetry) {
    response.setHeader('Retry-After', String(ipRetry))
    response.status(429).json({
      error: 'Too many requests. Please wait a moment and try again.',
      retry_after_seconds: ipRetry,
    })
    return false
  }

  const subject = bearerSubject(request)
  if (subject) {
    const userRetry = hitLimit(`user:${subject}:${route}`, userLimit, windowMs)
    if (userRetry) {
      response.setHeader('Retry-After', String(userRetry))
      response.status(429).json({
        error: 'Too many requests. Please wait a moment and try again.',
        retry_after_seconds: userRetry,
      })
      return false
    }
  }

  return true
}

export function normalizeBody(body) {
  if (!body) {
    return {}
  }
  if (typeof body === 'string') {
    return JSON.parse(body)
  }
  if (typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('Request body must be a JSON object.')
  }
  return body
}

export function rejectUnexpectedFields(payload, allowedFields) {
  const allowed = new Set(allowedFields)
  const unexpected = Object.keys(payload).filter((key) => !allowed.has(key))
  if (unexpected.length) {
    throw new Error(`Unexpected field: ${unexpected[0]}`)
  }
}

export function safeString(value, {
  field,
  max = 500,
  required = false,
  pattern = null,
} = {}) {
  const text = typeof value === 'string' ? value.trim() : ''
  if (required && !text) {
    throw new Error(`${field || 'field'} is required.`)
  }
  if (text.length > max) {
    throw new Error(`${field || 'field'} is too long.`)
  }
  if (/[\0\r\n]/.test(text)) {
    throw new Error(`${field || 'field'} contains invalid characters.`)
  }
  if (text && pattern && !pattern.test(text)) {
    throw new Error(`${field || 'field'} is invalid.`)
  }
  return text
}
