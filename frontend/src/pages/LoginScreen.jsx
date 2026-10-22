import { useCallback, useEffect, useRef, useState } from 'react'
import { listen } from '@tauri-apps/api/event'
import { isTauri } from '@tauri-apps/api/core'
import { getCurrent } from '@tauri-apps/plugin-deep-link'
import BrandLogo from '../components/BrandLogo'
import StarField from '../components/StarField'
import { SUPABASE_ANON_KEY, SUPABASE_URL } from '../lib/supabaseConfig'
import {
  AUTH_CONFIGURED,
  beginPendingAuth,
  clearPendingAuth,
  consumePendingAuth,
  loadSession,
  saveSession,
} from '../utils/session'
import { invokeIfTauri } from '../utils/tauri'

const WEBSITE_URL = 'https://recurdo.lakshitsachdeva.tech'
const DESKTOP_AUTH_ORIGIN = WEBSITE_URL
const DESKTOP_AUTH_CALLBACK_URL = `${DESKTOP_AUTH_ORIGIN}/auth-callback.html`
const OAUTH_PENDING_TIMEOUT_MS = 5 * 60 * 1000
const OAUTH_HELP_DELAY_MS = 3000

function AuthButton({
  children,
  disabled = false,
  onClick,
  variant = 'neutral',
}) {
  const [hovered, setHovered] = useState(false)

  const styles =
    variant === 'gold'
      ? {
          borderColor: hovered ? 'rgba(196,160,80,0.6)' : 'var(--gold-dim)',
          background: hovered ? 'rgba(196,160,80,0.14)' : 'var(--gold-glow)',
          color: 'var(--gold)',
        }
      : variant === 'ghost'
        ? {
            borderColor: hovered ? 'rgba(255,255,255,0.16)' : 'var(--border)',
            background: hovered ? 'rgba(255,255,255,0.03)' : 'transparent',
            color: hovered ? 'var(--text-2)' : 'var(--text-3)',
          }
        : {
            borderColor: hovered ? 'rgba(255,255,255,0.16)' : 'var(--border)',
            background: hovered ? 'rgba(255,255,255,0.05)' : 'var(--surface)',
            color: hovered ? 'var(--text-1)' : 'var(--text-2)',
          }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        width: '100%',
        padding: '12px 24px',
        borderRadius: 8,
        border: `1px solid ${styles.borderColor}`,
        background: styles.background,
        color: styles.color,
        fontSize: 14,
        fontFamily: 'var(--sans)',
        fontWeight: 400,
        letterSpacing: '0.01em',
        transition: 'all 200ms ease',
        opacity: disabled ? 0.6 : 1,
        cursor: disabled ? 'default' : 'pointer',
      }}
    >
      {children}
    </button>
  )
}

function AuthInput({ ...props }) {
  return (
    <input
      {...props}
      style={{
        width: '100%',
        padding: '12px 16px',
        borderRadius: 8,
        border: '1px solid rgba(196,160,80,0.16)',
        background: 'rgba(255,255,255,0.03)',
        color: 'var(--text-1)',
        fontSize: 14,
        fontFamily: 'var(--sans)',
        outline: 'none',
        caretColor: 'var(--gold)',
      }}
    />
  )
}

export default function LoginScreen({ onAuthenticated }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [checking, setChecking] = useState(true)
  const [emailMode, setEmailMode] = useState(false)
  const [isSignUp, setIsSignUp] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [oauthRetryUrl, setOauthRetryUrl] = useState('')
  const [oauthHelpVisible, setOauthHelpVisible] = useState(false)
  const oauthTimeoutRef = useRef(null)
  const oauthHelpTimeoutRef = useRef(null)

  const resetOauthUi = useCallback(() => {
    if (oauthTimeoutRef.current) {
      window.clearTimeout(oauthTimeoutRef.current)
      oauthTimeoutRef.current = null
    }
    if (oauthHelpTimeoutRef.current) {
      window.clearTimeout(oauthHelpTimeoutRef.current)
      oauthHelpTimeoutRef.current = null
    }
    setOauthRetryUrl('')
    setOauthHelpVisible(false)
  }, [])

  const completeAuth = useCallback(async (token, userEmail = '', refreshToken = '') => {
    if (!token) {
      return
    }

    resetOauthUi()
    setLoading(false)
    if (!isTauri()) {
      clearPendingAuth()
    }

    await saveSession({
      token,
      email: userEmail,
      refreshToken,
    })

    onAuthenticated({ token, email: userEmail, refreshToken })
  }, [onAuthenticated, resetOauthUi])

  const handleCallbackSession = useCallback(async (callbackSession) => {
    if (!callbackSession?.token) {
      return false
    }

    if (!isTauri()) {
      if (!callbackSession.state || !consumePendingAuth(callbackSession.state || '')) {
        return false
      }
    }

    await completeAuth(
      callbackSession.token,
      callbackSession.email || '',
      callbackSession.refreshToken || '',
    )
    return true
  }, [completeAuth])

  useEffect(() => {
    if (!AUTH_CONFIGURED) {
      setError('Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY in frontend env.')
      setChecking(false)
      return undefined
    }

    loadSession()
      .then((saved) => {
        if (saved?.token) {
          onAuthenticated(saved)
        } else {
          setChecking(false)
        }
      })
      .catch(() => setChecking(false))

    const unlistenPromises = []

    if (isTauri()) {
      unlistenPromises.push(
        listen('auth-callback', async ({ payload }) => {
          if (payload?.token) {
            const accepted = await handleCallbackSession({
              token: payload.token,
              email: payload.email || '',
              refreshToken: payload.refresh_token || payload.refreshToken || '',
              state: payload.state || '',
            })
            if (!accepted) {
              console.warn('Ignoring auth callback without an active login attempt')
            }
            return
          }

          if (payload?.error || payload?.message) {
            resetOauthUi()
            setLoading(false)
            setError(payload.error_description || payload.error || payload.message)
          }
        }),
      )

      // On desktop, Rust validates the OAuth callback state and emits
      // a trusted auth-callback event. Reading raw deep-link URLs here
      // can resurrect stale callbacks after a wipe/relaunch.
      void getCurrent().catch(() => {})
    }

    return () => {
      resetOauthUi()
      Promise.all(unlistenPromises).then((unlisteners) => {
        unlisteners.forEach((unlisten) => unlisten())
      })
    }
  }, [handleCallbackSession, onAuthenticated, resetOauthUi])

  const loginWithOAuth = async (provider) => {
    setLoading(true)
    setError(null)

    try {
      const state = isTauri()
        ? await invokeIfTauri('begin_auth')
        : beginPendingAuth()
      const desktopRedirect = new URL(isTauri() ? DESKTOP_AUTH_CALLBACK_URL : 'recurdo://auth/callback')
      if (isTauri()) {
        desktopRedirect.searchParams.set('desktop_state', state)
      }
      const params = new URLSearchParams({
        provider,
        redirect_to: desktopRedirect.toString(),
        prompt: 'select_account',
      })
      const url = `${SUPABASE_URL}/auth/v1/authorize?${params}`
      setOauthRetryUrl(url)
      setOauthHelpVisible(false)
      if (isTauri()) {
        await invokeIfTauri('open_url', { url })
      } else {
        window.location.href = url
      }

      resetOauthUi()
      setOauthRetryUrl(url)
      oauthTimeoutRef.current = window.setTimeout(() => {
        if (isTauri()) {
          void invokeIfTauri('clear_pending_auth')
        } else {
          clearPendingAuth()
        }
        resetOauthUi()
        setLoading(false)
      }, OAUTH_PENDING_TIMEOUT_MS)
      oauthHelpTimeoutRef.current = window.setTimeout(() => {
        setOauthHelpVisible(true)
      }, OAUTH_HELP_DELAY_MS)
    } catch (e) {
      if (isTauri()) {
        await invokeIfTauri('clear_pending_auth')
      } else {
        clearPendingAuth()
      }
      resetOauthUi()
      setError(e?.message || 'Unable to open browser')
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!loading || !isTauri()) {
      return undefined
    }

    let cancelled = false

    const pollSession = async () => {
      try {
        const saved = await loadSession()
        if (!cancelled && saved?.token) {
          await completeAuth(saved.token, saved.email || '', saved.refreshToken || '')
        }
      } catch {
        // Ignore transient read errors while waiting for the browser callback.
      }
    }

    void pollSession()
    const intervalId = window.setInterval(() => {
      void pollSession()
    }, 1000)

    return () => {
      cancelled = true
      window.clearInterval(intervalId)
    }
  }, [completeAuth, loading])

  const reopenBrowser = useCallback(async () => {
    if (!oauthRetryUrl) {
      return
    }

    try {
      await invokeIfTauri('open_url', { url: oauthRetryUrl })
    } catch (e) {
      resetOauthUi()
      setError(e?.message || 'Unable to reopen browser')
      setLoading(false)
    }
  }, [oauthRetryUrl, resetOauthUi])

  const cancelOauth = useCallback(async () => {
    if (isTauri()) {
      await invokeIfTauri('clear_pending_auth')
    } else {
      clearPendingAuth()
    }
    resetOauthUi()
    setLoading(false)
  }, [resetOauthUi])

  const loginWithEmail = async () => {
    if (!email || !password) return

    setLoading(true)
    setError(null)

    try {
      const endpoint = isSignUp
        ? `${SUPABASE_URL}/auth/v1/signup`
        : `${SUPABASE_URL}/auth/v1/token?grant_type=password`
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          apikey: SUPABASE_ANON_KEY,
        },
        body: JSON.stringify({ email, password }),
      })
      const data = await res.json()

      if (!res.ok) {
        throw new Error(data.error_description || data.msg || 'Login failed')
      }

      await completeAuth(data.access_token, email, data.refresh_token || '')
    } catch (e) {
      setError(e.message)
      setLoading(false)
    }
  }

  if (checking) {
    return <LoadingScreen />
  }

  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: 'var(--bg)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <StarField />

      <div
        style={{
          position: 'absolute',
          width: 600,
          height: 600,
          borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(196,160,80,0.04) 0%, transparent 70%)',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          pointerEvents: 'none',
        }}
      />

      <div
        style={{
          position: 'absolute',
          top: 28,
          left: 32,
        }}
      >
        <BrandLogo
          className="brand-logo login-brand-logo"
          alt="Recurdo"
        />
      </div>

      <div
        style={{
          position: 'relative',
          zIndex: 10,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          textAlign: 'center',
          padding: '0 40px',
          maxWidth: 460,
          width: '100%',
          animation: 'fadeUp 0.6s ease both',
        }}
      >
        <div className="label" style={{ marginBottom: 20, color: 'var(--gold-dim)' }}>
          local ai memory layer
        </div>

        <h1
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 'clamp(42px, 6vw, 64px)',
            fontWeight: 300,
            letterSpacing: '-0.01em',
            lineHeight: 1.05,
            color: 'var(--text-1)',
            marginBottom: 8,
          }}
        >
          find anything
        </h1>
        <h1
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 'clamp(42px, 6vw, 64px)',
            fontWeight: 300,
            fontStyle: 'italic',
            letterSpacing: '-0.01em',
            lineHeight: 1.05,
            color: 'var(--text-3)',
            marginBottom: 26,
          }}
        >
          you&apos;ve ever seen
        </h1>

        {error ? (
          <div
            style={{
              width: '100%',
              marginBottom: 16,
              padding: '12px 14px',
              borderRadius: 10,
              border: '1px solid rgba(229,86,86,0.22)',
              background: 'rgba(229,86,86,0.08)',
              color: 'rgba(255,172,172,0.9)',
              fontSize: 13,
              lineHeight: 1.5,
            }}
          >
            {error}
          </div>
        ) : null}

        {!emailMode ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: '100%' }}>
            <AuthButton
              onClick={() => loginWithOAuth('google')}
              disabled={loading}
              variant="gold"
            >
              <GoogleIcon />
              <span>{loading ? 'Opening browser...' : 'Continue with Google'}</span>
            </AuthButton>

            <AuthButton
              onClick={() => loginWithOAuth('github')}
              disabled={loading}
            >
              <GitHubIcon />
              <span>Continue with GitHub</span>
            </AuthButton>

            {loading ? (
              <div
                style={{
                  width: '100%',
                  padding: '12px 14px',
                  borderRadius: 10,
                  border: '1px solid rgba(196,160,80,0.16)',
                  background: 'rgba(255,255,255,0.03)',
                  color: 'var(--text-3)',
                  fontSize: 13,
                  lineHeight: 1.6,
                  textAlign: 'left',
                }}
              >
                <div>Finish sign-in in your browser.</div>
                <div style={{ color: 'var(--text-4)', marginTop: 4 }}>
                  Recurdo will continue automatically as soon as the browser hands the session back.
                </div>
                {oauthHelpVisible ? (
                  <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                    <button
                      type="button"
                      onClick={() => void reopenBrowser()}
                      style={{
                        borderRadius: 999,
                        border: '1px solid rgba(196,160,80,0.2)',
                        background: 'rgba(196,160,80,0.08)',
                        color: 'var(--gold)',
                        padding: '8px 14px',
                        fontSize: 12,
                        cursor: 'pointer',
                      }}
                    >
                      open browser again
                    </button>
                    <button
                      type="button"
                      onClick={() => void cancelOauth()}
                      style={{
                        borderRadius: 999,
                        border: '1px solid var(--border)',
                        background: 'transparent',
                        color: 'var(--text-3)',
                        padding: '8px 14px',
                        fontSize: 12,
                        cursor: 'pointer',
                      }}
                    >
                      cancel
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}

            <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '4px 0' }}>
              <div style={{ flex: 1, height: 1, background: 'var(--border-dim)' }} />
              <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
                or
              </span>
              <div style={{ flex: 1, height: 1, background: 'var(--border-dim)' }} />
            </div>

            <AuthButton onClick={() => setEmailMode(true)} variant="ghost">
              <span>Continue with email</span>
            </AuthButton>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: '100%' }}>
            <AuthInput
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
            <AuthInput
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="password"
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  void loginWithEmail()
                }
              }}
            />

            <AuthButton
              onClick={() => void loginWithEmail()}
              disabled={loading || !email || !password}
              variant="gold"
            >
              <span>{loading ? 'Signing in...' : isSignUp ? 'Create account' : 'Sign in'}</span>
            </AuthButton>

            <AuthButton onClick={() => setIsSignUp((value) => !value)} variant="ghost">
              <span>{isSignUp ? 'Already have an account?' : 'No account? Create one'}</span>
            </AuthButton>

            <button
              type="button"
              onClick={() => {
                setEmailMode(false)
                setError(null)
              }}
              style={{
                marginTop: 4,
                background: 'none',
                border: 'none',
                color: 'var(--text-3)',
                fontSize: 12,
              }}
            >
              back
            </button>
          </div>
        )}

        <p
          className="mono"
          style={{
            marginTop: 28,
            fontSize: 9,
            color: 'var(--text-4)',
            letterSpacing: '0.08em',
            lineHeight: 1.9,
          }}
        >
          your files never leave your machine
          <br />
          we only store your email + access status
        </p>
      </div>
    </div>
  )
}

function LoadingScreen() {
  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: 'var(--bg)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <StarField />
      <div
        style={{
          width: 20,
          height: 20,
          border: '1.5px solid rgba(255,255,255,0.06)',
          borderTop: '1.5px solid rgba(196,160,80,0.5)',
          borderRadius: '50%',
          animation: 'spin 0.9s linear infinite',
          position: 'relative',
          zIndex: 1,
        }}
      />
    </div>
  )
}

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24">
      <path
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
        fill="#4285F4"
      />
      <path
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
        fill="#34A853"
      />
      <path
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
        fill="#FBBC05"
      />
      <path
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
        fill="#EA4335"
      />
    </svg>
  )
}

function GitHubIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0 1 12 6.836c.85.004 1.705.114 2.504.336 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.202 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z" />
    </svg>
  )
}
