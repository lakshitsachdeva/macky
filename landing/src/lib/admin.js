import { createClient } from '@supabase/supabase-js'

const ADMIN_API_BASE = (import.meta.env.VITE_ADMIN_API_BASE || '/api/admin').trim()

let adminSupabase = null

function readJsonSafe(response) {
  return response
    .json()
    .catch(() => null)
}

function createHttpError(payload, status, fallback) {
  const error = new Error(payload?.error || fallback)
  error.statusCode = status
  error.payload = payload
  return error
}

export function getAdminSupabaseClient() {
  if (adminSupabase) {
    return adminSupabase
  }

  const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
  const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY
  if (!supabaseUrl || !anonKey) {
    throw new Error('Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY')
  }

  adminSupabase = createClient(supabaseUrl, anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  })

  return adminSupabase
}

export async function restoreAdminSessionFromUrl() {
  const supabase = getAdminSupabaseClient()

  if (typeof window !== 'undefined') {
    const hasCode = window.location.search.includes('code=')
    if (hasCode) {
      await supabase.auth.exchangeCodeForSession(window.location.href)
      window.history.replaceState({}, '', window.location.pathname)
    }
  }

  const { data, error } = await supabase.auth.getSession()
  if (error) {
    throw error
  }

  return data.session || null
}

export async function signInAdminWithGoogle() {
  const supabase = getAdminSupabaseClient()
  const redirectTo = typeof window !== 'undefined'
    ? `${window.location.origin}/admin.html`
    : undefined

  const { error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo,
      queryParams: {
        prompt: 'select_account',
      },
    },
  })

  if (error) {
    throw error
  }
}

export async function signOutAdmin() {
  const supabase = getAdminSupabaseClient()
  const { error } = await supabase.auth.signOut()
  if (error) {
    throw error
  }
}

export async function fetchAdminOverview(accessToken) {
  const response = await fetch(`${ADMIN_API_BASE}/requests`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
    },
  })

  const payload = await readJsonSafe(response)
  if (!response.ok) {
    throw createHttpError(payload, response.status, 'Could not load admin overview')
  }

  return payload
}

export async function runAdminAction(accessToken, actionPayload) {
  const response = await fetch(`${ADMIN_API_BASE}/access`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(actionPayload),
  })

  const payload = await readJsonSafe(response)
  if (!response.ok) {
    throw createHttpError(payload, response.status, 'Could not update access')
  }

  return payload
}
