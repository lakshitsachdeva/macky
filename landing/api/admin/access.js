import {
  denyWaitlistRequest,
  grantAccessForEmail,
  listAdminOverview,
  requireAdmin,
  restoreAccess,
  revokeAccess,
} from '../../lib/server/admin.js'
import {
  enforceRateLimit,
  normalizeBody,
  rejectUnexpectedFields,
  safeString,
  setSecurityHeaders,
} from '../../lib/server/security.js'

export default async function handler(request, response) {
  setSecurityHeaders(response, 'POST,OPTIONS')

  if (request.method === 'OPTIONS') {
    response.status(200).end()
    return
  }

  if (request.method !== 'POST') {
    response.setHeader('Allow', 'POST,OPTIONS')
    response.status(405).json({ error: 'Method not allowed.' })
    return
  }

  if (!enforceRateLimit(request, response, { name: 'admin:access', ipLimit: 120, userLimit: 60 })) {
    return
  }

  try {
    const { supabase, adminEmail } = await requireAdmin(request)
    const payload = normalizeBody(request.body)
    rejectUnexpectedFields(payload, [
      'action',
      'email',
      'userId',
      'user_id',
      'requestId',
      'request_id',
      'tier',
      'reason',
    ])
    const action = safeString(payload.action, {
      field: 'action',
      required: true,
      max: 20,
      pattern: /^(approve|deny|revoke|restore)$/,
    })
    const email = safeString(payload.email, { field: 'email', max: 254 })
    const userId = safeString(payload.userId || payload.user_id, { field: 'userId', max: 160 })
    const requestId = safeString(payload.requestId || payload.request_id, { field: 'requestId', max: 160 })
    const tier = safeString(payload.tier || 'beta', {
      field: 'tier',
      max: 40,
      pattern: /^[A-Za-z0-9_-]+$/,
    }) || 'beta'
    const reason = safeString(payload.reason, { field: 'reason', max: 500 })

    let result = null

    if (action === 'approve') {
      result = await grantAccessForEmail({
        supabase,
        email,
        tier,
        reason: reason || 'approved from dashboard',
        adminEmail,
      })
    } else if (action === 'deny') {
      result = await denyWaitlistRequest({
        supabase,
        requestId,
        email,
        adminEmail,
        reason,
      })
    } else if (action === 'revoke') {
      result = await revokeAccess({
        supabase,
        userId,
        email,
        adminEmail,
        reason,
      })
    } else if (action === 'restore') {
      result = await restoreAccess({
        supabase,
        userId,
        email,
        adminEmail,
        reason,
        tier,
      })
    } else {
      response.status(400).json({ error: 'Unsupported admin action.' })
      return
    }

    const overview = await listAdminOverview({ supabase })
    response.status(200).json({
      ok: true,
      result,
      admin: { email: adminEmail },
      ...overview,
    })
  } catch (error) {
    const message = error instanceof Error ? error.message : ''
    const validationError = /Unexpected field|Request body|invalid characters|required|too long|invalid/i.test(message)
    response.status(validationError ? 400 : error?.statusCode || 500).json({
      error: message || 'Could not update access.',
    })
  }
}
