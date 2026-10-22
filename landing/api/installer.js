import crypto from 'node:crypto'
import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3'
import { getSignedUrl } from '@aws-sdk/s3-request-presigner'
import { requireApprovedAccess } from '../lib/server/admin.js'
import {
  enforceRateLimit,
  normalizeBody,
  rejectUnexpectedFields,
  safeString,
  setSecurityHeaders,
} from '../lib/server/security.js'

const INSTALLER_URL = (
  process.env.RECURDO_DMG_URL
  || process.env.DESKTOP_DMG_URL
  || process.env.VITE_RECURDO_DMG_URL
  || ''
).trim()
const INSTALLER_BUILD_LABEL = (
  process.env.RECURDO_DMG_BUILD
  || process.env.VITE_RECURDO_DMG_BUILD
  || 'latest macOS beta'
).trim()
const INSTALLER_FILENAME = (
  process.env.RECURDO_DMG_FILENAME
  || 'Recurdo.dmg'
).trim()
const INSTALLER_TICKET_TTL_SECONDS = Math.max(
  60,
  Number.parseInt(process.env.RECURDO_INSTALLER_TICKET_TTL_SECONDS || '300', 10) || 300,
)
const INSTALLER_TICKET_SECRET = (
  process.env.RECURDO_INSTALLER_TOKEN_SECRET
  || process.env.SUPABASE_SERVICE_ROLE_KEY
  || process.env.SUPABASE_SERVICE_KEY
  || ''
).trim()
const R2_ACCOUNT_ID = (process.env.R2_ACCOUNT_ID || '').trim()
const R2_ACCESS_KEY_ID = (process.env.R2_ACCESS_KEY_ID || '').trim()
const R2_SECRET_ACCESS_KEY = (process.env.R2_SECRET_ACCESS_KEY || '').trim()
const R2_BUCKET_NAME = (process.env.R2_BUCKET_NAME || '').trim()
const R2_OBJECT_KEY = (process.env.RECURDO_DMG_KEY || '').trim()
const USE_R2 = Boolean(
  R2_ACCOUNT_ID && R2_ACCESS_KEY_ID && R2_SECRET_ACCESS_KEY && R2_BUCKET_NAME && R2_OBJECT_KEY,
)

let r2Client = null

function base64urlEncode(value) {
  return Buffer.from(value, 'utf8').toString('base64url')
}

function base64urlDecode(value) {
  return Buffer.from(value, 'base64url').toString('utf8')
}

function signTicket(payload) {
  const body = base64urlEncode(JSON.stringify(payload))
  const signature = crypto
    .createHmac('sha256', INSTALLER_TICKET_SECRET)
    .update(body)
    .digest('base64url')
  return `${body}.${signature}`
}

function verifyTicket(ticket) {
  const [body, signature] = String(ticket || '').split('.')
  if (!body || !signature) {
    return null
  }

  const expected = crypto
    .createHmac('sha256', INSTALLER_TICKET_SECRET)
    .update(body)
    .digest('base64url')

  const signatureBuffer = Buffer.from(signature)
  const expectedBuffer = Buffer.from(expected)
  if (
    signatureBuffer.length !== expectedBuffer.length
    || !crypto.timingSafeEqual(signatureBuffer, expectedBuffer)
  ) {
    return null
  }

  try {
    const payload = JSON.parse(base64urlDecode(body))
    if (!payload?.exp || Date.now() >= Number(payload.exp)) {
      return null
    }
    return payload
  } catch {
    return null
  }
}

function installerMetadata() {
  return {
    file_name: INSTALLER_FILENAME,
    build_label: INSTALLER_BUILD_LABEL,
    delivery: USE_R2 ? 'r2-signed-url' : 'static-url',
  }
}

function isInstallerConfigured() {
  return Boolean((USE_R2 || INSTALLER_URL) && INSTALLER_TICKET_SECRET)
}

function getR2Client() {
  if (r2Client) {
    return r2Client
  }

  r2Client = new S3Client({
    region: 'auto',
    endpoint: `https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId: R2_ACCESS_KEY_ID,
      secretAccessKey: R2_SECRET_ACCESS_KEY,
    },
  })

  return r2Client
}

async function resolveInstallerRedirect() {
  if (USE_R2) {
    const signedUrl = await getSignedUrl(
      getR2Client(),
      new GetObjectCommand({
        Bucket: R2_BUCKET_NAME,
        Key: R2_OBJECT_KEY,
        ResponseContentDisposition: `attachment; filename="${INSTALLER_FILENAME}"`,
      }),
      {
        expiresIn: INSTALLER_TICKET_TTL_SECONDS,
      },
    )

    return signedUrl
  }

  return INSTALLER_URL
}

export default async function handler(request, response) {
  setSecurityHeaders(response, 'GET,POST,OPTIONS')

  if (request.method === 'OPTIONS') {
    response.status(200).end()
    return
  }

  if (!isInstallerConfigured()) {
    response.status(503).json({
      error: 'Installer download is not configured.',
    })
    return
  }

  if (request.method === 'POST') {
    if (!enforceRateLimit(request, response, { name: 'installer:ticket', ipLimit: 60, userLimit: 20 })) {
      return
    }
    try {
      rejectUnexpectedFields(normalizeBody(request.body), [])
      const { user, access } = await requireApprovedAccess(request)
      const ticket = signTicket({
        sub: user.id,
        email: user.email || '',
        exp: Date.now() + INSTALLER_TICKET_TTL_SECONDS * 1000,
      })

      response.status(200).json({
        ok: true,
        ...installerMetadata(),
        expires_in_seconds: INSTALLER_TICKET_TTL_SECONDS,
        download_path: `/api/installer?ticket=${encodeURIComponent(ticket)}`,
        access: {
          tier: access.tier,
          expires_at: access.expires_at,
          days_remaining: access.days_remaining,
        },
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : ''
      const validationError = /Unexpected field|Request body|invalid characters|required|too long/i.test(message)
      response.status(validationError ? 400 : error?.statusCode || 500).json({
        error: message || 'Could not prepare installer download.',
        access: error?.access || null,
      })
    }
    return
  }

  if (request.method !== 'GET') {
    response.setHeader('Allow', 'GET,POST,OPTIONS')
    response.status(405).json({ error: 'Method not allowed.' })
    return
  }

  const url = new URL(request.url || '/api/installer', 'http://localhost')
  const query = Object.fromEntries(url.searchParams.entries())
  try {
    rejectUnexpectedFields(query, ['ticket'])
  } catch (error) {
    response.status(400).json({ error: error instanceof Error ? error.message : 'Invalid installer request.' })
    return
  }
  if (!enforceRateLimit(request, response, { name: 'installer:download', ipLimit: 120, userLimit: 120 })) {
    return
  }
  let ticket = ''
  try {
    ticket = safeString(url.searchParams.get('ticket') || '', {
      field: 'ticket',
      max: 4096,
      required: true,
    })
  } catch (error) {
    response.status(400).json({ error: error instanceof Error ? error.message : 'Invalid download ticket.' })
    return
  }
  const payload = verifyTicket(ticket)
  if (!payload) {
    response.status(401).json({ error: 'Download ticket is missing or expired.' })
    return
  }

  try {
    const redirectUrl = await resolveInstallerRedirect()
    response.statusCode = 302
    response.setHeader('Location', redirectUrl)
    response.end()
  } catch (error) {
    response.status(500).json({
      error: error instanceof Error ? error.message : 'Could not create installer download link.',
    })
  }
}
