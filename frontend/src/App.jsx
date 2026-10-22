import { useCallback, useEffect, useState } from 'react'
import { listen } from '@tauri-apps/api/event'
import BetaExpiredScreen from './pages/BetaExpiredScreen'
import LoginScreen from './pages/LoginScreen'
import MainApp from './pages/MainApp'
import { ensureBackendResponsive, getBackendHttpUrl, resetBackendOriginCache } from './lib/backend'
import { clearSession, loadSession, refreshSession, saveSession } from './utils/session'

const BACKEND_TIMEOUT_MS = 4000
const AUTH_STATUS_TIMEOUT_MS = 8000
const UNAVAILABLE_RETRY_MS = 2500
const ONBOARDING_STATE_KEY = 'recurdo_onboarding_v2'
const LEGACY_ONBOARDING_KEYS = ['recurdo_onboarding_step', 'recurdo_onboarding_done']
const BACKEND_START_ATTEMPTS = 30
const BACKEND_START_RETRY_MS = 1500
const BACKEND_START_REQUEST_TIMEOUT_MS = 1500
const LOCAL_MODE = import.meta.env.VITE_RECURDO_LOCAL_MODE === '1'
const LOCAL_SESSION = {
  token: 'local-dev-token',
  email: 'local@custody.app',
  refreshToken: '',
}

function clearLegacyOnboardingKeys() {
  LEGACY_ONBOARDING_KEYS.forEach((key) => {
    window.localStorage.removeItem(key)
  })
}

function getLocalOnboardingState() {
  const state = window.localStorage.getItem(ONBOARDING_STATE_KEY)
  if (['folder_select', 'scanning', 'done'].includes(String(state || ''))) {
    return state
  }
  return null
}

function setLocalOnboardingState(state) {
  if (state) {
    window.localStorage.setItem(ONBOARDING_STATE_KEY, state)
  } else {
    window.localStorage.removeItem(ONBOARDING_STATE_KEY)
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = BACKEND_TIMEOUT_MS) {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs)

  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
    })
  } finally {
    window.clearTimeout(timeoutId)
  }
}

async function fetchBackendWithTimeout(path, options = {}, timeoutMs = BACKEND_TIMEOUT_MS) {
  const backend = await getBackendHttpUrl()
  return fetchWithTimeout(`${backend}${path}`, options, timeoutMs)
}

function buildSessionShell(saved) {
  return {
    token: saved?.token || '',
    user: {
      email: saved?.email || '',
      name: null,
    },
    access: null,
  }
}

function getBootSession() {
  return LOCAL_MODE ? { ...LOCAL_SESSION } : loadSession()
}

async function waitForBackend(onProgress) {
  for (let attempt = 0; attempt < BACKEND_START_ATTEMPTS; attempt += 1) {
    try {
      const backend = await getBackendHttpUrl({ forceRefresh: attempt > 0 })
      const response =
        typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
          ? await fetch(`${backend}/ready`, {
            signal: AbortSignal.timeout(BACKEND_START_REQUEST_TIMEOUT_MS),
          })
          : await fetchWithTimeout(
            `${backend}/ready`,
            {},
            BACKEND_START_REQUEST_TIMEOUT_MS,
          )
      if (response.ok) {
        return true
      }
    } catch {
      // Keep retrying while the local backend warms up.
    }

    onProgress?.({
      attempt: attempt + 1,
      max: BACKEND_START_ATTEMPTS,
      message:
        attempt < 3
          ? 'starting up...'
          : attempt < 8
            ? 'loading your index...'
            : attempt < 15
              ? 'this is taking longer than usual...'
              : 'almost ready...',
    })

    await new Promise((resolve) => {
      window.setTimeout(resolve, BACKEND_START_RETRY_MS)
    })
  }

  return false
}

async function readIndexStatus(token) {
  try {
    const statusResponse = await fetchBackendWithTimeout(
      '/index/status',
      {
        headers: { Authorization: `Bearer ${token}` },
      },
      AUTH_STATUS_TIMEOUT_MS,
    )

    if (!statusResponse.ok) {
      return { uniqueMemories: 0, filesWatched: 0, loaded: false }
    }

    const status = await statusResponse.json()
    return {
      uniqueMemories: Number(status?.unique_memories ?? status?.total_items ?? 0) || 0,
      filesWatched: Number(status?.files_watched ?? 0) || 0,
      loaded: true,
    }
  } catch {
    return { uniqueMemories: 0, filesWatched: 0, loaded: false }
  }
}

async function determineScreen(token) {
  clearLegacyOnboardingKeys()
  const localState = getLocalOnboardingState()
  const { uniqueMemories, filesWatched, loaded } = await readIndexStatus(token)

  // An empty install should always restart onboarding, even if stale
  // local/user settings claim onboarding finished previously.
  if (loaded && uniqueMemories === 0 && filesWatched === 0) {
    setLocalOnboardingState(null)
    return { screen: 'onboarding', uniqueMemories: 0 }
  }

  // A library with watched files but no indexed memories is still mid-setup
  // or failed to finish the first scan. Keep the user in onboarding until
  // real searchable content exists.
  if (loaded && uniqueMemories === 0 && filesWatched > 0) {
    if (localState === 'done') {
      setLocalOnboardingState('scanning')
    }
    return { screen: 'onboarding', uniqueMemories: 0 }
  }

  try {
    const settingsResponse = await fetchBackendWithTimeout(
      '/user/settings',
      {
        headers: { Authorization: `Bearer ${token}` },
      },
      AUTH_STATUS_TIMEOUT_MS,
    )
    if (settingsResponse.ok) {
      const settings = await settingsResponse.json()
      if (settings?.onboarding_completed) {
        setLocalOnboardingState('done')
        return { screen: 'app', uniqueMemories }
      }
    }
  } catch {
    // Fall back to local state when settings are unavailable.
  }

  if (localState === 'done') {
    return { screen: 'app', uniqueMemories }
  }
  if (localState === 'scanning' || localState === 'folder_select') {
    return { screen: 'onboarding', uniqueMemories: 0 }
  }

  if (uniqueMemories > 0) {
    return { screen: 'reinstall', uniqueMemories }
  }

  return { screen: 'onboarding', uniqueMemories: 0 }
}

export default function App() {
  const [screen, setScreen] = useState('loading')
  const [session, setSession] = useState(null)
  const [startupMessage, setStartupMessage] = useState('starting up...')
  const [reinstallMemories, setReinstallMemories] = useState(0)
  const [reinstallBusyAction, setReinstallBusyAction] = useState('')
  const [reinstallError, setReinstallError] = useState('')

  const validateAndRoute = useCallback(async (savedSession) => {
    let saved = savedSession
    let data = null

    setStartupMessage('verifying access...')

    try {
      const res = await fetchBackendWithTimeout('/auth/status', {
        headers: { Authorization: `Bearer ${saved.token}` },
      }, AUTH_STATUS_TIMEOUT_MS)

      if (res.status === 401) {
        const refreshed = await refreshSession(saved)
        if (!refreshed?.token) {
          await clearSession().catch(() => {})
          setSession(null)
          setScreen('login')
          return
        }

        saved = refreshed

        const retryRes = await fetchBackendWithTimeout('/auth/status', {
          headers: { Authorization: `Bearer ${saved.token}` },
        }, AUTH_STATUS_TIMEOUT_MS)
        if (retryRes.status === 401) {
          await clearSession().catch(() => {})
          setSession(null)
          setScreen('login')
          return
        }
        if (!retryRes.ok) {
          throw new Error(`auth_status_${retryRes.status}`)
        }
        data = await retryRes.json()
      } else {
        if (!res.ok) {
          throw new Error(`auth_status_${res.status}`)
        }

        data = await res.json()
      }
    } catch (fetchError) {
      console.warn(
        'Access verification failed after backend startup:',
        fetchError?.message || fetchError,
      )
      setSession(buildSessionShell(saved))
      setScreen('unavailable')
      return
    }

    if (data && !data.access?.has_access) {
      setSession({ token: saved.token, user: data.user, access: data.access })
      setScreen('expired')
      return
    }

    if (!data) {
      setSession(buildSessionShell(saved))
      setScreen('unavailable')
      return
    }

    setSession({ token: saved.token, user: data.user, access: data.access })

    setStartupMessage('checking your library...')
    const nextScreen = await determineScreen(saved.token)
    setReinstallMemories(nextScreen.uniqueMemories || 0)
    setScreen(nextScreen.screen)
  }, [])

  const boot = useCallback(async () => {
    setScreen('loading')
    setStartupMessage('starting up...')
    setReinstallError('')
    resetBackendOriginCache()

    try {
      const saved = await getBootSession()

      if (!saved?.token) {
        setSession(null)
        setScreen('login')
        return
      }

      setScreen('starting')
      const backendReady = await waitForBackend(({ message }) => {
        setStartupMessage(message)
      })

      if (!backendReady) {
        setSession(buildSessionShell(saved))
        setScreen('backend_error')
        return
      }

      await validateAndRoute(saved)
    } catch (error) {
      console.error('Boot error:', error)
      setSession(null)
      setScreen('login')
    }
  }, [validateAndRoute])

  useEffect(() => {
    void boot()
  }, [boot])

  useEffect(() => {
    const unlistenPromise = listen('auth-callback', async ({ payload }) => {
      if (!payload?.token) {
        return
      }

      await saveSession({
        token: payload.token,
        email: payload.email || '',
        refreshToken: payload.refresh_token || payload.refreshToken || '',
      }).catch(() => {})

      await boot()
    })

    return () => {
      unlistenPromise.then((unlisten) => unlisten())
    }
  }, [boot])

  useEffect(() => {
    const originalFetch = window.fetch
    window.fetch = async (...args) => {
      const response = await originalFetch(...args)
      if (response.status === 403) {
        const clone = response.clone()
        try {
          const body = await clone.json()
          const code = body?.detail?.code || body?.code
          if (['BETA_EXPIRED', 'ACCESS_BLOCKED'].includes(code)) {
            setScreen('expired')
          }
        } catch {
          // ignore invalid JSON
        }
      }
      return response
    }

    return () => {
      window.fetch = originalFetch
    }
  }, [])

  useEffect(() => {
    if (screen !== 'unavailable') {
      return undefined
    }

    const timeoutId = window.setTimeout(() => {
      void boot()
    }, UNAVAILABLE_RETRY_MS)

    return () => {
      window.clearTimeout(timeoutId)
    }
  }, [boot, screen])

  useEffect(() => {
    if (screen !== 'backend_error') {
      return undefined
    }

    const timeoutId = window.setTimeout(() => {
      void boot()
    }, UNAVAILABLE_RETRY_MS)

    return () => {
      window.clearTimeout(timeoutId)
    }
  }, [boot, screen])

  useEffect(() => {
    const handleWake = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      if (screen === 'unavailable' || screen === 'backend_error') {
        void boot()
        return
      }

      void ensureBackendResponsive({ forceRefresh: true }).catch((error) => {
        console.warn('Backend wake check failed', error)
      })
    }

    window.addEventListener('focus', handleWake)
    document.addEventListener('visibilitychange', handleWake)

    return () => {
      window.removeEventListener('focus', handleWake)
      document.removeEventListener('visibilitychange', handleWake)
    }
  }, [boot, screen])

  const handleAuthenticated = useCallback(
    async ({ token, refreshToken = '', email = '' }) => {
      await saveSession({ token, refreshToken, email }).catch(() => {})
      await boot()
    },
    [boot],
  )

  const handleSignOut = useCallback(async () => {
    if (LOCAL_MODE) {
      // Local-first mode has no remote identity to revoke. Restarting the
      // boot flow keeps the workspace usable after a sign-out click without
      // sending the user into the cloud-login screen.
      void boot()
      return
    }

    await clearSession().catch(() => {})
    setLocalOnboardingState(null)
    setSession(null)
    setStartupMessage('starting up...')
    setReinstallMemories(0)
    setReinstallError('')
    setReinstallBusyAction('')
    setScreen('login')
  }, [boot])

  const persistUserSettings = useCallback(async (updates) => {
    if (!session?.token) {
      throw new Error('Missing session token')
    }

    const response = await fetchBackendWithTimeout(
      '/user/settings',
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${session.token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(updates),
      },
      AUTH_STATUS_TIMEOUT_MS,
    )

    if (!response.ok) {
      throw new Error(`user_settings_${response.status}`)
    }

    return response.json()
  }, [session?.token])

  const handleOnboardingComplete = useCallback(async () => {
    const statusSnapshot = await readIndexStatus(session?.token)
    if (
      statusSnapshot.loaded
      && statusSnapshot.uniqueMemories === 0
      && statusSnapshot.filesWatched > 0
    ) {
      setLocalOnboardingState('scanning')
      setScreen('onboarding')
      return
    }

    const previousState = getLocalOnboardingState()
    setLocalOnboardingState('done')
    try {
      await persistUserSettings({ onboarding_completed: true })
    } catch (error) {
      console.warn('Unable to persist onboarding completion', error)
      setLocalOnboardingState(previousState || 'scanning')
      setScreen('onboarding')
      return
    }

    setReinstallMemories(0)
    setScreen('app')
  }, [persistUserSettings, session?.token])

  const handleContinueReinstall = useCallback(async () => {
    setReinstallBusyAction('continue')
    setReinstallError('')
    setLocalOnboardingState('done')
    try {
      await persistUserSettings({ onboarding_completed: true })
      setReinstallMemories(0)
      setScreen('app')
    } catch (error) {
      console.warn('Unable to continue previous install state', error)
      setLocalOnboardingState(null)
      setReinstallError('Could not restore your previous onboarding state right now.')
    } finally {
      setReinstallBusyAction('')
    }
  }, [persistUserSettings])

  const handleStartFresh = useCallback(async () => {
    if (!session?.token) {
      return
    }

    setReinstallBusyAction('fresh')
    setReinstallError('')
    try {
      await persistUserSettings({ onboarding_completed: false })
      const response = await fetchBackendWithTimeout(
        '/items/all',
        {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${session.token}` },
        },
        AUTH_STATUS_TIMEOUT_MS,
      )
      if (!response.ok) {
        throw new Error(`clear_items_${response.status}`)
      }
      setLocalOnboardingState(null)
      setReinstallMemories(0)
      setScreen('onboarding')
    } catch (error) {
      console.warn('Unable to start fresh onboarding flow', error)
      setReinstallError('Could not reset this library yet. Try again in a moment.')
    } finally {
      setReinstallBusyAction('')
    }
  }, [persistUserSettings, session?.token])

  if (screen === 'loading') return <LoadingScreen />
  if (screen === 'starting') return <StartingScreen message={startupMessage} />
  if (screen === 'backend_error') {
    return <BackendErrorScreen onRetry={boot} />
  }
  if (screen === 'login') return <LoginScreen onAuthenticated={handleAuthenticated} />
  if (screen === 'unavailable') {
    return <BackendUnavailableScreen onRetry={boot} onSignOut={handleSignOut} />
  }
  if (screen === 'expired') {
    return <BetaExpiredScreen user={session?.user} onSignOut={handleSignOut} />
  }
  if (screen === 'reinstall') {
    return (
      <ReinstallScreen
        memoryCount={reinstallMemories}
        isContinuing={reinstallBusyAction === 'continue'}
        isResetting={reinstallBusyAction === 'fresh'}
        error={reinstallError}
        onContinue={handleContinueReinstall}
        onStartFresh={handleStartFresh}
        onSignOut={handleSignOut}
      />
    )
  }
  if (screen === 'onboarding') {
    return (
      <MainApp
        session={session}
        onSignOut={handleSignOut}
        daysRemaining={session?.access?.days_remaining}
        forceOnboarding
        onOnboardingComplete={handleOnboardingComplete}
      />
    )
  }

  return (
    <MainApp
      session={session}
      onSignOut={handleSignOut}
      daysRemaining={session?.access?.days_remaining}
    />
  )
}

function StartingScreen({ message }) {
  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: '#080808',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 20,
      }}
    >
      <div
        style={{
          fontFamily: "'Cormorant Garamond', serif",
          fontSize: 18,
          fontWeight: 300,
          color: 'rgba(255,255,255,0.25)',
          letterSpacing: '0.04em',
        }}
      >
        recurdo
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div
          style={{
            width: 16,
            height: 16,
            border: '1.5px solid rgba(255,255,255,0.06)',
            borderTop: '1.5px solid rgba(196,160,80,0.5)',
            borderRadius: '50%',
            animation: 'spin 0.9s linear infinite',
          }}
        />
        <span
          style={{
            fontFamily: 'monospace',
            fontSize: 11,
            color: 'rgba(255,255,255,0.25)',
            letterSpacing: '0.08em',
          }}
        >
          {message || 'starting up...'}
        </span>
      </div>

      <style>{'@keyframes spin { to { transform: rotate(360deg) } }'}</style>
    </div>
  )
}

function BackendErrorScreen({ onRetry }) {
  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: '#080808',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 16,
        padding: 24,
      }}
    >
      <div
        style={{
          fontFamily: 'monospace',
          fontSize: 10,
          color: 'rgba(255,255,255,0.2)',
          letterSpacing: '0.1em',
        }}
      >
        STARTUP FAILED
      </div>
      <p
        style={{
          fontFamily: "'Geist', system-ui",
          fontSize: 13,
          color: 'rgba(255,255,255,0.3)',
          textAlign: 'center',
          maxWidth: 320,
          lineHeight: 1.8,
          margin: 0,
        }}
      >
        Recurdo&apos;s background service couldn&apos;t start.
        Retrying automatically in a moment. If this keeps happening, fully quit Recurdo and reopen it.
      </p>
      <button
        type="button"
        onClick={() => void onRetry()}
        style={{
          padding: '10px 24px',
          borderRadius: 9,
          border: '1px solid rgba(255,255,255,0.12)',
          background: 'rgba(255,255,255,0.05)',
          color: 'rgba(255,255,255,0.5)',
          fontSize: 13,
          fontFamily: "'Geist', system-ui",
          cursor: 'pointer',
        }}
      >
        try again
      </button>
    </div>
  )
}

function ReinstallScreen({
  memoryCount,
  isContinuing,
  isResetting,
  error,
  onContinue,
  onStartFresh,
  onSignOut,
}) {
  const isBusy = isContinuing || isResetting

  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: '#080808',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: 520,
          borderRadius: 20,
          border: '1px solid rgba(196,160,80,0.18)',
          background: 'rgba(255,255,255,0.02)',
          boxShadow: '0 24px 70px rgba(0,0,0,0.28)',
          padding: '40px 32px',
          textAlign: 'center',
        }}
      >
        <div
          style={{
            color: 'rgba(196,160,80,0.55)',
            fontSize: 12,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            marginBottom: 16,
          }}
        >
          Welcome Back
        </div>
        <div
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 'clamp(42px, 6vw, 60px)',
            lineHeight: 1.02,
            color: '#f3eee6',
            marginBottom: 16,
          }}
        >
          welcome back.
        </div>
        <div
          style={{
            color: 'rgba(255,255,255,0.82)',
            fontSize: 17,
            lineHeight: 1.7,
            maxWidth: 420,
            margin: '0 auto',
          }}
        >
          looks like you&apos;ve used Recurdo before.
          <br />
          we found {Number(memoryCount || 0).toLocaleString()} memories from your previous session.
        </div>

        {error ? (
          <div
            style={{
              marginTop: 18,
              color: 'rgba(255,124,124,0.9)',
              fontSize: 13,
            }}
          >
            {error}
          </div>
        ) : null}

        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
            marginTop: 28,
          }}
        >
          <button
            type="button"
            onClick={() => void onContinue()}
            disabled={isBusy}
            style={primaryActionButton}
          >
            {isContinuing ? 'continuing…' : 'continue where you left off'}
          </button>
          <button
            type="button"
            onClick={() => void onStartFresh()}
            disabled={isBusy}
            style={secondaryActionButton}
          >
            {isResetting ? 'starting fresh…' : 'start fresh'}
          </button>
          <button
            type="button"
            onClick={() => void onSignOut()}
            disabled={isBusy}
            style={textActionButton}
          >
            sign out
          </button>
        </div>
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
        background: '#080808',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        style={{
          width: 20,
          height: 20,
          border: '1.5px solid rgba(255,255,255,0.06)',
          borderTop: '1.5px solid rgba(196,160,80,0.5)',
          borderRadius: '50%',
          animation: 'spin 0.9s linear infinite',
        }}
      />
      <style>{'@keyframes spin { to { transform: rotate(360deg) } }'}</style>
    </div>
  )
}

const primaryActionButton = {
  width: '100%',
  borderRadius: 14,
  border: '1px solid rgba(196,160,80,0.32)',
  background: 'rgba(196,160,80,0.92)',
  color: '#120f0a',
  padding: '14px 18px',
  fontSize: 15,
  fontWeight: 600,
  cursor: 'pointer',
}

const secondaryActionButton = {
  width: '100%',
  borderRadius: 14,
  border: '1px solid rgba(255,255,255,0.12)',
  background: 'rgba(255,255,255,0.02)',
  color: 'rgba(255,255,255,0.88)',
  padding: '14px 18px',
  fontSize: 15,
  fontWeight: 500,
  cursor: 'pointer',
}

const textActionButton = {
  background: 'transparent',
  border: 'none',
  color: 'rgba(255,255,255,0.54)',
  fontSize: 13,
  cursor: 'pointer',
}

function BackendUnavailableScreen({ onRetry, onSignOut }) {
  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        background: '#080808',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: 460,
          borderRadius: 20,
          border: '1px solid rgba(196,160,80,0.18)',
          background: 'rgba(255,255,255,0.02)',
          boxShadow: '0 24px 70px rgba(0,0,0,0.28)',
          padding: '40px 32px',
          textAlign: 'center',
        }}
      >
        <div
          style={{
            color: 'rgba(196,160,80,0.55)',
            fontSize: 12,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            marginBottom: 16,
          }}
        >
          access verification required
        </div>
        <h1
          style={{
            margin: 0,
            color: 'rgba(255,255,255,0.92)',
            fontSize: 'clamp(28px, 4vw, 40px)',
            fontWeight: 400,
            lineHeight: 1.1,
          }}
        >
          could not verify access
        </h1>
        <p
          style={{
            margin: '16px auto 28px',
            maxWidth: 340,
            color: 'rgba(255,255,255,0.56)',
            fontSize: 14,
            lineHeight: 1.7,
          }}
        >
          Recurdo will not open until the local backend confirms your session and access status.
        </p>
        <p
          style={{
            margin: '-10px auto 24px',
            maxWidth: 320,
            color: 'rgba(196,160,80,0.52)',
            fontSize: 12,
            lineHeight: 1.7,
          }}
        >
          retrying automatically while access verification becomes available
        </p>
        <div
          style={{
            display: 'flex',
            justifyContent: 'center',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <button
            type="button"
            onClick={() => void onRetry()}
            style={{
              padding: '12px 22px',
              borderRadius: 10,
              border: 'none',
              background: 'var(--gold)',
              color: '#000',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            retry now
          </button>
          <button
            type="button"
            onClick={() => void onSignOut()}
            style={{
              padding: '12px 22px',
              borderRadius: 10,
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'transparent',
              color: 'rgba(255,255,255,0.74)',
              fontSize: 14,
              fontWeight: 500,
            }}
          >
            sign out
          </button>
        </div>
      </div>
    </div>
  )
}
