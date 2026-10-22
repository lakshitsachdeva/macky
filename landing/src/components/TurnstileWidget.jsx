import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'

const TURNSTILE_SCRIPT_ID = 'cf-turnstile-script'
const TURNSTILE_SCRIPT_SRC =
  'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'

export const TURNSTILE_SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY?.trim() || ''
export const TURNSTILE_ENABLED = Boolean(TURNSTILE_SITE_KEY)

let turnstileScriptPromise

function loadTurnstileScript() {
  if (typeof window === 'undefined') {
    return Promise.resolve(null)
  }

  if (window.turnstile) {
    return Promise.resolve(window.turnstile)
  }

  if (turnstileScriptPromise) {
    return turnstileScriptPromise
  }

  turnstileScriptPromise = new Promise((resolve, reject) => {
    const existingScript = document.getElementById(TURNSTILE_SCRIPT_ID)
    if (existingScript) {
      existingScript.addEventListener('load', () => resolve(window.turnstile), { once: true })
      existingScript.addEventListener(
        'error',
        () => reject(new Error('Could not load security check.')),
        { once: true },
      )
      return
    }

    const script = document.createElement('script')
    script.id = TURNSTILE_SCRIPT_ID
    script.src = TURNSTILE_SCRIPT_SRC
    script.async = true
    script.defer = true
    script.onload = () => resolve(window.turnstile)
    script.onerror = () => reject(new Error('Could not load security check.'))
    document.head.appendChild(script)
  })

  return turnstileScriptPromise
}

export const TurnstileWidget = forwardRef(function TurnstileWidget(
  { onTokenChange, className = '', appearance = 'interaction-only' },
  ref,
) {
  const containerRef = useRef(null)
  const widgetIdRef = useRef(null)
  const onTokenChangeRef = useRef(onTokenChange)
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    onTokenChangeRef.current = onTokenChange
  }, [onTokenChange])

  const emitTokenChange = (token) => {
    onTokenChangeRef.current(token)
  }

  useImperativeHandle(ref, () => ({
    reset() {
      emitTokenChange('')
      if (widgetIdRef.current !== null && window.turnstile) {
        window.turnstile.reset(widgetIdRef.current)
      }
    },
  }), [])

  useEffect(() => {
    if (!TURNSTILE_ENABLED || !containerRef.current || widgetIdRef.current !== null) {
      return undefined
    }

    let cancelled = false

    void loadTurnstileScript()
      .then((turnstile) => {
        if (cancelled || !turnstile || !containerRef.current || widgetIdRef.current !== null) {
          return
        }

        widgetIdRef.current = turnstile.render(containerRef.current, {
          sitekey: TURNSTILE_SITE_KEY,
          appearance,
          theme: 'dark',
          callback: (token) => {
            setLoadError('')
            emitTokenChange(token)
          },
          'expired-callback': () => {
            emitTokenChange('')
          },
          'error-callback': () => {
            emitTokenChange('')
            setLoadError('Complete the security check and try again.')
          },
        })
      })
      .catch((error) => {
        if (!cancelled) {
          emitTokenChange('')
          setLoadError(error instanceof Error ? error.message : 'Could not load security check.')
        }
      })

    return () => {
      cancelled = true
      if (widgetIdRef.current !== null && window.turnstile) {
        window.turnstile.remove(widgetIdRef.current)
        widgetIdRef.current = null
      }
    }
  }, [])

  if (!TURNSTILE_ENABLED) {
    return null
  }

  return (
    <div className={className}>
      <div ref={containerRef} />
      {loadError ? <div className="waitlist-error">{loadError}</div> : null}
    </div>
  )
})
