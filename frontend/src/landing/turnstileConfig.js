export const TURNSTILE_SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY?.trim() || ''
export const TURNSTILE_ENABLED = Boolean(TURNSTILE_SITE_KEY)
