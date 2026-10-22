import { createClient } from '@supabase/supabase-js'

const DEFAULT_TIER = 'beta'
const DEFAULT_BETA_DURATION_DAYS = Math.max(
  1,
  Number.parseInt(process.env.RECURDO_BETA_DURATION_DAYS || '90', 10) || 90,
)
const ADMIN_EMAILS = new Set(
  (process.env.ADMIN_EMAILS || process.env.RECURDO_ADMIN_EMAILS || '')
    .split(',')
    .map((value) => normalizeEmail(value))
    .filter(Boolean),
)

function normalizeEmail(email = '') {
  return String(email || '').trim().toLowerCase()
}

function getSupabaseAdmin() {
  const supabaseUrl = process.env.SUPABASE_URL || process.env.VITE_SUPABASE_URL
  const serviceRoleKey =
    process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SERVICE_KEY

  if (!supabaseUrl || !serviceRoleKey) {
    throw new Error('Missing server-side Supabase credentials')
  }

  return createClient(supabaseUrl, serviceRoleKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  })
}

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

function mergeMetadata(existing, next) {
  return {
    ...(existing && typeof existing === 'object' && !Array.isArray(existing) ? existing : {}),
    ...(next && typeof next === 'object' && !Array.isArray(next) ? next : {}),
  }
}

function formatGrantNote({ reason = '', adminEmail = '', requestId = '' } = {}) {
  const pieces = []
  if (reason) {
    pieces.push(reason.trim())
  }
  if (requestId) {
    pieces.push(`request ${requestId}`)
  }
  if (adminEmail) {
    pieces.push(`approved by ${adminEmail}`)
  }
  return pieces.filter(Boolean).join(' · ')
}

function computeGrantWindow() {
  const start = new Date()
  const expires = new Date(start.getTime() + DEFAULT_BETA_DURATION_DAYS * 24 * 60 * 60 * 1000)
  return {
    betaStartedAt: start.toISOString(),
    expiresAt: expires.toISOString(),
    durationDays: DEFAULT_BETA_DURATION_DAYS,
  }
}

function normalizeAccessPayload(payload) {
  const safe = payload && typeof payload === 'object' ? payload : {}
  return {
    has_access: Boolean(safe.has_access),
    tier: safe.tier || 'unknown',
    days_remaining: Number.isFinite(safe.days_remaining) ? safe.days_remaining : 0,
    expires_at: safe.expires_at || null,
    is_blocked: Boolean(safe.is_blocked),
  }
}

async function ensureProfileRecord(supabase, user) {
  if (!user?.id || !user?.email) {
    return
  }

  const payload = {
    id: user.id,
    email: user.email,
    name: user.user_metadata?.full_name || user.user_metadata?.name || user.email.split('@')[0],
    avatar_url: user.user_metadata?.avatar_url || user.user_metadata?.picture || null,
  }

  const { error } = await supabase.from('profiles').upsert(payload, {
    onConflict: 'id',
  })

  if (error) {
    throw error
  }
}

async function getAccessRowForUserId(supabase, userId) {
  const { data, error } = await supabase
    .from('access')
    .select('id, user_id, tier, beta_started_at, expires_at, is_blocked, notes, created_at, updated_at')
    .eq('user_id', userId)
    .limit(1)
    .maybeSingle()

  if (error) {
    throw error
  }

  return data || null
}

async function getWaitlistRowByEmail(supabase, email) {
  const normalizedEmail = normalizeEmail(email)
  if (!normalizedEmail) {
    return null
  }

  const { data, error } = await supabase
    .from('waitlist_signups')
    .select('id, email, email_normalized, source, status, metadata, created_at')
    .eq('email_normalized', normalizedEmail)
    .limit(1)
    .maybeSingle()

  if (error) {
    throw error
  }

  return data || null
}

async function upsertAccessGrant(supabase, { userId, tier = DEFAULT_TIER, reason = '', adminEmail = '', requestId = '' }) {
  const existing = await getAccessRowForUserId(supabase, userId)
  const { betaStartedAt, expiresAt } = computeGrantWindow()
  const notes = formatGrantNote({ reason, adminEmail, requestId })

  if (existing?.id) {
    const { data, error } = await supabase
      .from('access')
      .update({
        tier,
        is_blocked: false,
        beta_started_at: betaStartedAt,
        expires_at: expiresAt,
        notes: notes || existing.notes || null,
        updated_at: betaStartedAt,
      })
      .eq('id', existing.id)
      .select('id, user_id, tier, beta_started_at, expires_at, is_blocked, notes, created_at, updated_at')
      .single()

    if (error) {
      throw error
    }

    return data
  }

  const { data, error } = await supabase
    .from('access')
    .insert({
      user_id: userId,
      tier,
      beta_started_at: betaStartedAt,
      expires_at: expiresAt,
      is_blocked: false,
      notes: notes || null,
      updated_at: betaStartedAt,
    })
    .select('id, user_id, tier, beta_started_at, expires_at, is_blocked, notes, created_at, updated_at')
    .single()

  if (error) {
    throw error
  }

  return data
}

async function updateWaitlistRow(supabase, rowId, { status, metadataPatch = {} }) {
  const { data: current, error: currentError } = await supabase
    .from('waitlist_signups')
    .select('id, metadata')
    .eq('id', rowId)
    .single()

  if (currentError) {
    throw currentError
  }

  const { data, error } = await supabase
    .from('waitlist_signups')
    .update({
      status,
      metadata: mergeMetadata(current?.metadata, metadataPatch),
    })
    .eq('id', rowId)
    .select('id, email, email_normalized, source, status, metadata, created_at')
    .single()

  if (error) {
    throw error
  }

  return data
}

async function resolveProfilesByEmails(supabase, emails) {
  const normalized = Array.from(new Set(emails.map((value) => normalizeEmail(value)).filter(Boolean)))
  if (!normalized.length) {
    return []
  }

  const { data, error } = await supabase
    .from('profiles')
    .select('id, email, name, avatar_url, created_at')
    .in('email', normalized)

  if (error) {
    throw error
  }

  return data || []
}

async function resolveProfilesByIds(supabase, ids) {
  const uniqueIds = Array.from(new Set(ids.map((value) => String(value || '').trim()).filter(Boolean)))
  if (!uniqueIds.length) {
    return []
  }

  const { data, error } = await supabase
    .from('profiles')
    .select('id, email, name, avatar_url, created_at')
    .in('id', uniqueIds)

  if (error) {
    throw error
  }

  return data || []
}

export async function requireAdmin(request) {
  if (!ADMIN_EMAILS.size) {
    const error = new Error('Missing ADMIN_EMAILS / RECURDO_ADMIN_EMAILS')
    error.statusCode = 503
    throw error
  }

  const token = getBearerToken(request)
  if (!token) {
    const error = new Error('Missing bearer token')
    error.statusCode = 401
    throw error
  }

  const supabase = getSupabaseAdmin()
  const { data, error } = await supabase.auth.getUser(token)
  if (error || !data?.user) {
    const authError = new Error('Invalid session')
    authError.statusCode = 401
    throw authError
  }

  const email = normalizeEmail(data.user.email)
  if (!ADMIN_EMAILS.has(email)) {
    const adminError = new Error('Admin access required')
    adminError.statusCode = 403
    throw adminError
  }

  return {
    supabase,
    adminEmail: email,
    user: data.user,
  }
}

export async function maybeProvisionAccessFromApprovedRequest({ supabase, user }) {
  if (!supabase || !user?.id || !user?.email) {
    return { granted: false, reason: 'missing-user' }
  }

  await ensureProfileRecord(supabase, user)

  const existingAccess = await getAccessRowForUserId(supabase, user.id)
  if (existingAccess?.id) {
    return { granted: false, reason: 'existing-access', access: existingAccess }
  }

  const matchingRow = await getWaitlistRowByEmail(supabase, user.email)
  if (!matchingRow || !['approved', 'invited'].includes(String(matchingRow.status || ''))) {
    return { granted: false, reason: 'no-approved-request' }
  }

  const access = await upsertAccessGrant(supabase, {
    userId: user.id,
    tier: DEFAULT_TIER,
    reason: 'approved from waitlist',
    requestId: matchingRow.id,
  })

  const now = new Date().toISOString()
  await updateWaitlistRow(supabase, matchingRow.id, {
    status: 'invited',
    metadataPatch: {
      granted_user_id: user.id,
      granted_at: now,
      provisioned_from_waitlist: true,
    },
  })

  return { granted: true, reason: 'waitlist-approved', access }
}

export async function verifyAccessSession(request, { allowProvision = true } = {}) {
  const token = getBearerToken(request)
  if (!token) {
    const error = new Error('Missing bearer token')
    error.statusCode = 401
    throw error
  }

  const supabase = getSupabaseAdmin()
  const { data, error } = await supabase.auth.getUser(token)
  if (error || !data?.user) {
    const authError = new Error('Invalid session')
    authError.statusCode = 401
    throw authError
  }

  const user = data.user
  if (allowProvision) {
    await maybeProvisionAccessFromApprovedRequest({ supabase, user })
  }

  const { data: accessData, error: accessError } = await supabase.rpc('check_access', {
    p_user_id: user.id,
  })

  if (accessError) {
    throw accessError
  }

  const access = normalizeAccessPayload(accessData)
  return {
    supabase,
    token,
    user,
    access,
  }
}

export async function requireApprovedAccess(request, { allowProvision = true } = {}) {
  const result = await verifyAccessSession(request, { allowProvision })
  const { access } = result
  if (!access.has_access) {
    const deniedError = new Error('Approved beta access required')
    deniedError.statusCode = 403
    deniedError.access = access
    throw deniedError
  }
  return result
}

export async function listAdminOverview({ supabase, limit = 200 }) {
  const { data: requests, error: requestsError } = await supabase
    .from('waitlist_signups')
    .select('id, email, email_normalized, source, status, metadata, created_at')
    .order('created_at', { ascending: false })
    .limit(limit)

  if (requestsError) {
    throw requestsError
  }

  const { data: grants, error: grantsError } = await supabase
    .from('access')
    .select('id, user_id, tier, beta_started_at, expires_at, is_blocked, notes, created_at, updated_at')
    .order('updated_at', { ascending: false })
    .limit(limit)

  if (grantsError) {
    throw grantsError
  }

  const requestProfiles = await resolveProfilesByEmails(supabase, (requests || []).map((row) => row.email))
  const grantProfiles = await resolveProfilesByIds(supabase, (grants || []).map((row) => row.user_id))

  const profileByEmail = new Map(requestProfiles.map((row) => [normalizeEmail(row.email), row]))
  const profileById = new Map(grantProfiles.map((row) => [row.id, row]))
  const accessByUserId = new Map((grants || []).map((row) => [row.user_id, row]))

  const hydratedRequests = (requests || []).map((row) => {
    const profile = profileByEmail.get(normalizeEmail(row.email)) || null
    const access = profile ? accessByUserId.get(profile.id) || null : null
    return {
      ...row,
      matched_profile: profile,
      access,
    }
  })

  const hydratedGrants = (grants || []).map((row) => ({
    ...row,
    profile: profileById.get(row.user_id) || null,
  }))

  return {
    requests: hydratedRequests,
    grants: hydratedGrants,
  }
}

export async function grantAccessForEmail({
  supabase,
  email,
  tier = DEFAULT_TIER,
  reason = '',
  adminEmail = '',
}) {
  const normalizedEmail = normalizeEmail(email)
  if (!normalizedEmail) {
    throw new Error('Missing email')
  }

  const now = new Date().toISOString()
  const existingRequest = await getWaitlistRowByEmail(supabase, normalizedEmail)
  let requestRow = existingRequest

  if (existingRequest?.id) {
    requestRow = await updateWaitlistRow(supabase, existingRequest.id, {
      status: 'approved',
      metadataPatch: {
        approved_at: now,
        approved_by: adminEmail,
        approval_reason: reason || null,
      },
    })
  } else {
    const { data, error } = await supabase
      .from('waitlist_signups')
      .insert({
        email: normalizedEmail,
        source: 'admin-dashboard',
        status: 'approved',
        metadata: {
          approved_at: now,
          approved_by: adminEmail,
          approval_reason: reason || null,
        },
      })
      .select('id, email, email_normalized, source, status, metadata, created_at')
      .single()

    if (error) {
      throw error
    }

    requestRow = data
  }

  const matchingProfiles = await resolveProfilesByEmails(supabase, [normalizedEmail])
  let grantedAccess = null
  if (matchingProfiles[0]?.id) {
    grantedAccess = await upsertAccessGrant(supabase, {
      userId: matchingProfiles[0].id,
      tier,
      reason: reason || 'approved from dashboard',
      adminEmail,
      requestId: requestRow.id,
    })

    await updateWaitlistRow(supabase, requestRow.id, {
      status: 'invited',
      metadataPatch: {
        granted_user_id: matchingProfiles[0].id,
        granted_at: now,
      },
    })
  }

  return {
    request: requestRow,
    access: grantedAccess,
  }
}

export async function denyWaitlistRequest({
  supabase,
  requestId = '',
  email = '',
  adminEmail = '',
  reason = '',
}) {
  let rowId = String(requestId || '').trim()
  if (!rowId) {
    const existing = await getWaitlistRowByEmail(supabase, email)
    rowId = existing?.id || ''
  }

  if (!rowId) {
    throw new Error('Waitlist request not found')
  }

  return updateWaitlistRow(supabase, rowId, {
    status: 'denied',
    metadataPatch: {
      denied_at: new Date().toISOString(),
      denied_by: adminEmail,
      denial_reason: reason || null,
    },
  })
}

export async function revokeAccess({
  supabase,
  userId = '',
  email = '',
  adminEmail = '',
  reason = '',
}) {
  let resolvedUserId = String(userId || '').trim()
  const normalizedEmail = normalizeEmail(email)

  if (!resolvedUserId && normalizedEmail) {
    const matchingProfiles = await resolveProfilesByEmails(supabase, [normalizedEmail])
    resolvedUserId = matchingProfiles[0]?.id || ''
  }

  if (normalizedEmail) {
    const existingRequest = await getWaitlistRowByEmail(supabase, normalizedEmail)
    if (existingRequest?.id) {
      await updateWaitlistRow(supabase, existingRequest.id, {
        status: 'denied',
        metadataPatch: {
          revoked_at: new Date().toISOString(),
          revoked_by: adminEmail,
          revoke_reason: reason || null,
        },
      })
    }
  }

  if (!resolvedUserId) {
    return { revoked: false, reason: 'no-matching-user' }
  }

  const existing = await getAccessRowForUserId(supabase, resolvedUserId)
  if (!existing?.id) {
    return { revoked: false, reason: 'no-access-row', user_id: resolvedUserId }
  }

  const { data, error } = await supabase
    .from('access')
    .update({
      is_blocked: true,
      updated_at: new Date().toISOString(),
      notes: formatGrantNote({
        reason: reason || existing.notes || 'revoked by admin',
        adminEmail,
      }),
    })
    .eq('id', existing.id)
    .select('id, user_id, tier, beta_started_at, expires_at, is_blocked, notes, created_at, updated_at')
    .single()

  if (error) {
    throw error
  }

  return { revoked: true, access: data }
}

export async function restoreAccess({
  supabase,
  userId = '',
  email = '',
  adminEmail = '',
  reason = '',
  tier = DEFAULT_TIER,
}) {
  if (email) {
    return grantAccessForEmail({
      supabase,
      email,
      tier,
      reason: reason || 'restored by admin',
      adminEmail,
    })
  }

  const resolvedUserId = String(userId || '').trim()
  if (!resolvedUserId) {
    throw new Error('Missing user id or email')
  }

  const access = await upsertAccessGrant(supabase, {
    userId: resolvedUserId,
    tier,
    reason: reason || 'restored by admin',
    adminEmail,
  })

  return { access }
}
