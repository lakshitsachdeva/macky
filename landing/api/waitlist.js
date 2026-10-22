import { createHmac } from 'node:crypto'
import { createClient } from '@supabase/supabase-js'
import {
  enforceRateLimit,
  normalizeBody,
  rejectUnexpectedFields,
  setSecurityHeaders,
} from '../lib/server/security.js'

const WAITLIST_OFFSET = 100
const RATE_LIMIT_WINDOW_MS = 60 * 60 * 1000
const RATE_LIMIT_MAX_REQUESTS = 5
const MIN_FORM_FILL_MS = 500
const MAX_FORM_FILL_MS = 60 * 60 * 1000
const EMAIL_PATTERN = /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$/i
const ALLOWED_SOURCES = new Set(['landing-page', 'frontend-landing', 'beta_expired'])
const BLOCKED_USER_AGENTS = [/curl\//i, /wget/i, /python-requests/i, /httpie/i, /go-http-client/i]
const DEFAULT_ALLOWED_ORIGIN_HOSTS = new Set(['localhost', '127.0.0.1', 'tauri.localhost'])
const DEFAULT_BLOCKED_EMAIL_DOMAINS = new Set([
  '10minutemail.com',
  'guerrillamail.com',
  'mailinator.com',
  'sharklasers.com',
  'spamgourmet.com',
  'tempmail.com',
  'yopmail.com',
])

function getAllowedOriginHosts(request) {
  const configuredHosts = (process.env.WAITLIST_ALLOWED_ORIGIN_HOSTS || '')
    .split(',')
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean)

  const hosts = new Set(DEFAULT_ALLOWED_ORIGIN_HOSTS)
  configuredHosts.forEach((host) => hosts.add(host))

  const requestHost = typeof request.headers.host === 'string' ? request.headers.host.trim() : ''
  if (requestHost) {
    hosts.add(requestHost.toLowerCase())
  }

  return hosts
}

function getBlockedEmailDomains() {
  const configuredDomains = (process.env.WAITLIST_BLOCKED_EMAIL_DOMAINS || '')
    .split(',')
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean)

  const domains = new Set(DEFAULT_BLOCKED_EMAIL_DOMAINS)
  configuredDomains.forEach((domain) => domains.add(domain))
  return domains
}

function getSupabaseAdmin() {
  const supabaseUrl = process.env.SUPABASE_URL || process.env.VITE_SUPABASE_URL
  const serviceRoleKey =
    process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SERVICE_KEY

  if (!supabaseUrl || !serviceRoleKey) {
    throw new Error('Missing server-side Supabase credentials for waitlist API')
  }

  return {
    serviceRoleKey,
    client: createClient(supabaseUrl, serviceRoleKey, {
      auth: {
        persistSession: false,
        autoRefreshToken: false,
      },
    }),
  }
}

function getClientIp(request) {
  const forwardedFor = request.headers['x-forwarded-for']
  if (typeof forwardedFor === 'string' && forwardedFor.trim()) {
    return forwardedFor.split(',')[0].trim()
  }

  const realIp = request.headers['x-real-ip']
  if (typeof realIp === 'string' && realIp.trim()) {
    return realIp.trim()
  }

  return request.socket?.remoteAddress || ''
}

function hashIp(ipAddress, secret) {
  return createHmac('sha256', secret).update(ipAddress).digest('hex')
}

function getRequestOriginHost(request) {
  const origin = request.headers.origin
  if (typeof origin === 'string' && origin.trim()) {
    try {
      return new URL(origin).host.toLowerCase()
    } catch {
      return ''
    }
  }

  return ''
}

function stripPort(host) {
  return host.replace(/:\d+$/, '')
}

function hostMatchesAllowed(host, allowedHosts) {
  if (!host) {
    return false
  }

  if (allowedHosts.has(host)) {
    return true
  }

  const bareHost = stripPort(host)
  for (const allowedHost of allowedHosts) {
    if (stripPort(allowedHost) === bareHost) {
      return true
    }
  }

  return false
}

function getRequestRefererHost(request, payload) {
  const refererHeader = request.headers.referer
  const referer = typeof refererHeader === 'string' && refererHeader.trim()
    ? refererHeader
    : typeof payload.referrer === 'string'
      ? payload.referrer
      : ''

  if (!referer) {
    return ''
  }

  try {
    return new URL(referer).host.toLowerCase()
  } catch {
    return ''
  }
}

function hasBlockedUserAgent(userAgent) {
  return BLOCKED_USER_AGENTS.some((pattern) => pattern.test(userAgent))
}

function getEmailDomain(email) {
  return email.split('@')[1]?.toLowerCase() || ''
}

async function verifyTurnstileToken(token, ipAddress) {
  const secret = process.env.TURNSTILE_SECRET_KEY?.trim()
  if (!secret) {
    return { enabled: false, success: true, hostname: '' }
  }

  if (!token) {
    return { enabled: true, success: false, hostname: '' }
  }

  const body = new URLSearchParams({
    secret,
    response: token,
  })

  if (ipAddress) {
    body.set('remoteip', ipAddress)
  }

  const response = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body,
  })

  const payload = await response.json()
  return {
    enabled: true,
    success: Boolean(response.ok && payload?.success),
    hostname: typeof payload?.hostname === 'string' ? payload.hostname.toLowerCase() : '',
  }
}

async function countRecentRequests(supabase, ipHash) {
  const windowStart = new Date(Date.now() - RATE_LIMIT_WINDOW_MS).toISOString()
  const { count, error } = await supabase
    .from('waitlist_signups')
    .select('id', { count: 'exact', head: true })
    .eq('ip_hash', ipHash)
    .gte('created_at', windowStart)

  if (error) {
    throw error
  }

  return count ?? 0
}

async function handleGet(response) {
  const { client: supabase } = getSupabaseAdmin()
  const { count, error } = await supabase
    .from('waitlist_signups')
    .select('id', { count: 'exact', head: true })

  if (error) {
    throw error
  }

  response.status(200).json({ count: (count ?? 0) + WAITLIST_OFFSET })
}

async function handlePost(request, response) {
  const { client: supabase, serviceRoleKey } = getSupabaseAdmin()
  const payload = normalizeBody(request.body)
  rejectUnexpectedFields(payload, [
    'email',
    'source',
    'honeypot',
    'turnstileToken',
    'elapsedMs',
    'path',
    'referrer',
  ])
  const email = typeof payload.email === 'string' ? payload.email.trim() : ''
  const source = typeof payload.source === 'string' ? payload.source.trim().toLowerCase() : ''
  const honeypot = typeof payload.honeypot === 'string' ? payload.honeypot.trim() : ''
  const turnstileToken =
    typeof payload.turnstileToken === 'string' ? payload.turnstileToken.trim() : ''
  const elapsedMs = Number.parseInt(payload.elapsedMs, 10)
  const userAgent = (request.headers['user-agent'] || '').slice(0, 300)
  const originHost = getRequestOriginHost(request)
  const refererHost = getRequestRefererHost(request, payload)
  const requestHost =
    typeof request.headers.host === 'string' ? request.headers.host.trim().toLowerCase() : ''
  const allowedOriginHosts = getAllowedOriginHosts(request)
  const blockedEmailDomains = getBlockedEmailDomains()
  const emailDomain = getEmailDomain(email)

  if (!email || email.length > 254 || !EMAIL_PATTERN.test(email)) {
    response.status(400).json({ error: 'Please enter a valid email address.' })
    return
  }

  if (emailDomain && blockedEmailDomains.has(emailDomain)) {
    response.status(400).json({ error: 'Please use a permanent email address.' })
    return
  }

  if (!ALLOWED_SOURCES.has(source)) {
    response.status(400).json({ error: 'Invalid signup source.' })
    return
  }

  if (honeypot) {
    response.status(400).json({ error: 'Could not join waitlist right now.' })
    return
  }

  if (!Number.isFinite(elapsedMs) || elapsedMs < MIN_FORM_FILL_MS || elapsedMs > MAX_FORM_FILL_MS) {
    response.status(400).json({ error: 'Please wait a moment and try again.' })
    return
  }

  if (hasBlockedUserAgent(userAgent)) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  if (originHost && !hostMatchesAllowed(originHost, allowedOriginHosts)) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  if (refererHost && !hostMatchesAllowed(refererHost, allowedOriginHosts)) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  if (
    originHost &&
    refererHost &&
    stripPort(originHost) !== stripPort(refererHost)
  ) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  const trustedHost =
    originHost ||
    refererHost ||
    (hostMatchesAllowed(requestHost, allowedOriginHosts) ? requestHost : '')

  if (!trustedHost) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  const ipAddress = getClientIp(request)
  if (!ipAddress) {
    response.status(400).json({ error: 'Could not verify this signup.' })
    return
  }

  const turnstile = await verifyTurnstileToken(turnstileToken, ipAddress)
  if (
    turnstile.enabled &&
    (
      !turnstile.success ||
      (turnstile.hostname && stripPort(turnstile.hostname) !== stripPort(trustedHost))
    )
  ) {
    response.status(400).json({ error: 'Complete the security check and try again.' })
    return
  }

  const ipHash = hashIp(ipAddress, serviceRoleKey)
  const recentRequestCount = await countRecentRequests(supabase, ipHash)
  if (recentRequestCount >= RATE_LIMIT_MAX_REQUESTS) {
    response.status(429).json({
      error: 'Too many signup attempts from this network. Please try again later.',
    })
    return
  }

  const metadata = {
    path: typeof payload.path === 'string' ? payload.path.slice(0, 200) : null,
    referrer: typeof payload.referrer === 'string' ? payload.referrer.slice(0, 300) : null,
    origin_host: originHost || null,
    referer_host: refererHost,
    request_host: requestHost || null,
    trusted_host: trustedHost,
    turnstile_hostname: turnstile.hostname || null,
    user_agent: userAgent,
  }

  const { error } = await supabase.from('waitlist_signups').insert({
    email,
    source,
    ip_hash: ipHash,
    metadata,
  })

  if (error?.code === '23505') {
    response.status(200).json({ alreadyExists: true })
    return
  }

  if (error) {
    throw error
  }

  response.status(201).json({ alreadyExists: false })
}

export default async function handler(request, response) {
  setSecurityHeaders(response, 'GET,POST,OPTIONS')

  if (request.method === 'OPTIONS') {
    response.status(204).end()
    return
  }

  try {
    if (request.method === 'GET') {
      if (!enforceRateLimit(request, response, { name: 'waitlist:get', ipLimit: 120, userLimit: 120 })) {
        return
      }
      await handleGet(response)
      return
    }

    if (request.method === 'POST') {
      if (!enforceRateLimit(request, response, { name: 'waitlist:post', ipLimit: 20, userLimit: 10 })) {
        return
      }
      await handlePost(request, response)
      return
    }

    response.setHeader('Allow', 'GET,POST,OPTIONS')
    response.status(405).json({ error: 'Method not allowed.' })
  } catch (error) {
    console.error('Waitlist API error', error)
    const message = error instanceof Error ? error.message : ''
    const validationError = /Unexpected field|Request body|invalid characters|required|too long/i.test(message)
    response.status(validationError ? 400 : 500).json({
      error: validationError ? message : 'Could not process waitlist request right now.',
    })
  }
}
