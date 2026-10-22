import { createClient } from '@supabase/supabase-js'

const ACCESS_STATUS_API = (import.meta.env.VITE_ACCESS_STATUS_API || '/api/access-status').trim()
const INSTALLER_API = (import.meta.env.VITE_INSTALLER_API || '/api/installer').trim()

let accessSupabase = null

function readJsonSafe(response) {
  return response.json().catch(() => null)
}

function createHttpError(payload, status, fallback) {
  const error = new Error(payload?.error || fallback)
  error.statusCode = status
  error.payload = payload
  return error
}

export function getAccessSupabaseClient() {
  if (accessSupabase) {
    return accessSupabase
  }

  const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
  const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY
  if (!supabaseUrl || !anonKey) {
    throw new Error('Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY')
  }

  accessSupabase = createClient(supabaseUrl, anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  })

  return accessSupabase
}

export async function restoreAccessSessionFromUrl(pathname = '/download.html') {
  const supabase = getAccessSupabaseClient()

  if (typeof window !== 'undefined' && window.location.search.includes('code=')) {
    await supabase.auth.exchangeCodeForSession(window.location.href)
    window.history.replaceState({}, '', pathname)
  }

  const { data, error } = await supabase.auth.getSession()
  if (error) {
    throw error
  }

  return data.session || null
}

export async function signInForAccess(pathname = '/download.html') {
  const supabase = getAccessSupabaseClient()
  const redirectTo = typeof window !== 'undefined'
    ? `${window.location.origin}${pathname}`
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

export async function signOutAccess() {
  const supabase = getAccessSupabaseClient()
  const { error } = await supabase.auth.signOut()
  if (error) {
    throw error
  }
}

export async function fetchAccessStatus(accessToken) {
  const response = await fetch(ACCESS_STATUS_API, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
    },
  })

  const payload = await readJsonSafe(response)
  if (!response.ok) {
    throw createHttpError(payload, response.status, 'Could not verify beta access')
  }

  return payload
}

export async function requestInstallerDownload(accessToken) {
  const response = await fetch(INSTALLER_API, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: 'application/json',
    },
  })

  const payload = await readJsonSafe(response)
  if (!response.ok) {
    throw createHttpError(payload, response.status, 'Could not prepare installer download')
  }

  return payload
}
