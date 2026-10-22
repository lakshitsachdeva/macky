import { listAdminOverview, requireAdmin } from '../../lib/server/admin.js'
import {
  enforceRateLimit,
  rejectUnexpectedFields,
  setSecurityHeaders,
} from '../../lib/server/security.js'

export default async function handler(request, response) {
  setSecurityHeaders(response, 'GET,OPTIONS')

  if (request.method === 'OPTIONS') {
    response.status(200).end()
    return
  }

  if (request.method !== 'GET') {
    response.setHeader('Allow', 'GET,OPTIONS')
    response.status(405).json({ error: 'Method not allowed.' })
    return
  }

  try {
    const url = new URL(request.url || '/api/admin/requests', 'http://localhost')
    rejectUnexpectedFields(Object.fromEntries(url.searchParams.entries()), [])
  } catch (error) {
    response.status(400).json({ error: error instanceof Error ? error.message : 'Invalid admin request.' })
    return
  }

  if (!enforceRateLimit(request, response, { name: 'admin:requests', ipLimit: 120, userLimit: 60 })) {
    return
  }

  try {
    const { supabase, adminEmail } = await requireAdmin(request)
    const overview = await listAdminOverview({ supabase })
    response.status(200).json({
      admin: { email: adminEmail },
      ...overview,
    })
  } catch (error) {
    response.status(error?.statusCode || 500).json({
      error: error instanceof Error ? error.message : 'Could not load admin requests.',
    })
  }
}
