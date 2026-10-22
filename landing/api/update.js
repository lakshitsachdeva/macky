import {
  enforceRateLimit,
  rejectUnexpectedFields,
  safeString,
  setSecurityHeaders,
} from '../lib/server/security.js'

const DEFAULT_PLATFORM_KEY = 'darwin-aarch64'
const PLATFORM_PART_PATTERN = /^[a-z0-9_-]{1,40}$/
const VERSION_PATTERN = /^v?[0-9][0-9A-Za-z.+-]{0,31}$/

function envValue(...keys) {
  for (const key of keys) {
    const value = String(process.env[key] || '').trim()
    if (value) {
      return value
    }
  }
  return ''
}

function normalizeVersion(version = '') {
  return String(version || '').trim().replace(/^v/i, '')
}

function parseVersion(version = '') {
  return normalizeVersion(version)
    .split(/[.+-]/)
    .slice(0, 3)
    .map((part) => Number.parseInt(part, 10))
    .map((part) => (Number.isFinite(part) ? part : 0))
}

function compareVersions(a, b) {
  const left = parseVersion(a)
  const right = parseVersion(b)
  for (let index = 0; index < 3; index += 1) {
    if ((left[index] || 0) > (right[index] || 0)) {
      return 1
    }
    if ((left[index] || 0) < (right[index] || 0)) {
      return -1
    }
  }
  return 0
}

function platformKeyFromRequest(request) {
  rejectUnexpectedFields(request.query || {}, ['target', 'arch', 'current'])
  const target = safeString(request.query?.target, {
    field: 'target',
    max: 40,
    pattern: PLATFORM_PART_PATTERN,
  }).toLowerCase()
  const arch = safeString(request.query?.arch, {
    field: 'arch',
    max: 40,
    pattern: PLATFORM_PART_PATTERN,
  }).toLowerCase()
  if (!target || !arch) {
    return DEFAULT_PLATFORM_KEY
  }
  return `${target}-${arch}`
}

function platformEnvSuffix(platformKey) {
  return platformKey.replace(/[^a-z0-9]+/gi, '_').toUpperCase()
}

function releaseForPlatform(platformKey) {
  const suffix = platformEnvSuffix(platformKey)
  const version = envValue('RECURDO_LATEST_VERSION', 'RECURDO_UPDATE_VERSION')
  const url = envValue(
    `RECURDO_UPDATE_URL_${suffix}`,
    `RECURDO_${suffix}_UPDATE_URL`,
    `RECURDO_${suffix}_URL`,
  )
  const signature = envValue(
    `RECURDO_UPDATE_SIGNATURE_${suffix}`,
    `RECURDO_${suffix}_UPDATE_SIGNATURE`,
    `RECURDO_${suffix}_SIGNATURE`,
  )

  return {
    version,
    url,
    signature,
    pub_date: envValue('RECURDO_LATEST_PUB_DATE', 'RECURDO_UPDATE_PUB_DATE') || new Date().toISOString(),
    notes: envValue('RECURDO_LATEST_NOTES', 'RECURDO_UPDATE_NOTES'),
  }
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

  if (!enforceRateLimit(request, response, { name: 'update', ipLimit: 240, userLimit: 240 })) {
    return
  }

  let platformKey
  let current
  try {
    platformKey = platformKeyFromRequest(request)
    current = normalizeVersion(safeString(request.query?.current, {
      field: 'current',
      max: 32,
      pattern: VERSION_PATTERN,
    }))
  } catch (error) {
    response.status(400).json({ error: error instanceof Error ? error.message : 'Invalid update request.' })
    return
  }

  const release = releaseForPlatform(platformKey)
  if (!release.version || !release.url || !release.signature) {
    response.status(204).end()
    return
  }

  if (current && compareVersions(release.version, current) <= 0) {
    response.status(204).end()
    return
  }

  response.status(200).json({
    version: release.version,
    pub_date: release.pub_date,
    url: release.url,
    signature: release.signature,
    notes: release.notes,
  })
}
