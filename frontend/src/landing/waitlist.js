const WAITLIST_API_URL = import.meta.env.VITE_WAITLIST_API_URL?.trim()

async function readJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

function getWaitlistApiUrl() {
  if (WAITLIST_API_URL) {
    return WAITLIST_API_URL
  }

  if (typeof window !== 'undefined' && /^https?:$/.test(window.location.protocol)) {
    return new URL('/api/waitlist', window.location.origin).toString()
  }

  throw new Error('Waitlist API is not configured')
}

export async function createWaitlistSignup(
  email,
  { source = 'frontend-landing', honeypot = '', elapsedMs = 0, turnstileToken = '' } = {},
) {
  const response = await fetch(getWaitlistApiUrl(), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({
      email,
      source,
      honeypot,
      elapsedMs,
      turnstileToken,
      path: window.location.pathname,
      referrer: document.referrer,
    }),
  })

  const payload = await readJson(response)
  if (response.ok) {
    return { alreadyExists: Boolean(payload?.alreadyExists) }
  }

  throw new Error(payload?.error || 'Could not join waitlist right now')
}
