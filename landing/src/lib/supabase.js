const WAITLIST_API_URL = (import.meta.env.VITE_WAITLIST_API_URL || '/api/waitlist').trim()

async function readJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

export async function fetchWaitlistCount() {
  const response = await fetch(WAITLIST_API_URL, {
    headers: {
      Accept: 'application/json',
    },
  })

  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(payload?.error || 'Could not load waitlist count')
  }

  return payload?.count ?? 100
}

export async function createWaitlistSignup(
  email,
  { source = 'landing-page', honeypot = '', elapsedMs = 0, turnstileToken = '' } = {},
) {
  const response = await fetch(WAITLIST_API_URL, {
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
