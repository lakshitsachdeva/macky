import React, { useEffect, useMemo, useState } from 'react'
import ReactDOM from 'react-dom/client'
import {
  fetchAccessStatus,
  getAccessSupabaseClient,
  requestInstallerDownload,
  restoreAccessSessionFromUrl,
  signInForAccess,
  signOutAccess,
} from './lib/access'
import './index.css'
import './download.css'

function formatDate(value) {
  if (!value) {
    return '—'
  }
  try {
    return new Intl.DateTimeFormat('en-IN', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(value))
  } catch {
    return String(value)
  }
}

function tierLabel(access) {
  const tier = String(access?.tier || '').trim().toLowerCase()
  if (tier === 'beta' || tier === 'whitelist') {
    return 'private beta'
  }
  if (tier === 'expired') {
    return 'expired beta'
  }
  if (tier === 'blocked') {
    return 'revoked'
  }
  return tier || 'pending'
}

function statusTone(access) {
  if (access?.is_blocked) {
    return 'blocked'
  }
  if (access?.has_access) {
    return 'approved'
  }
  return 'pending'
}

function LoginScreen({ error }) {
  return (
    <div className="download-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="download-shell">
        <section className="download-hero-card">
          <div className="download-hero-copy">
            <div className="download-kicker">recurdo for macos</div>
            <h1 className="download-title">your beta download is waiting behind a real access gate.</h1>
            <p className="download-subtitle">
              Sign in with the Google account you used for Recurdo. If your beta request has been
              approved, you can download the latest Mac build here right away.
            </p>
            <div className="download-actions">
              <button
                type="button"
                className="download-button download-button-primary"
                onClick={() => void signInForAccess('/download.html')}
              >
                sign in with google
              </button>
              <a className="download-button download-button-secondary" href="/">
                back to website
              </a>
            </div>
            {error ? <p className="download-error">{error}</p> : null}
          </div>

          <div className="download-hero-side">
            <div className="download-glow-card">
              <div className="download-mini-label">what happens next</div>
              <div className="download-mini-title">approved users can install immediately.</div>
              <p className="download-mini-copy">
                Once your request is approved, this page unlocks the latest DMG for your account.
              </p>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

function PendingState({ session, access, error, onRetry }) {
  const blocked = Boolean(access?.is_blocked)
  const headline = blocked ? 'this account is not cleared for beta access.' : 'your request is still pending approval.'
  const copy = blocked
    ? 'This Google account is currently blocked from the beta. If that looks wrong, sign in with another account or contact the team.'
    : 'You are signed in correctly, but this email has not been approved for the beta yet. Once approved, this page will unlock the download automatically.'

  return (
    <div className="download-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="download-shell">
        <section className="download-topbar">
          <div>
            <div className="download-kicker">beta access</div>
            <div className="download-user">{session?.user?.email || 'signed in'}</div>
          </div>
          <div className="download-actions">
            <button type="button" className="download-button download-button-secondary" onClick={() => void onRetry()}>
              refresh status
            </button>
            <button type="button" className="download-button download-button-secondary" onClick={() => void signOutAccess()}>
              use another account
            </button>
          </div>
        </section>

        <section className="download-hero-card">
          <div className="download-hero-copy">
            <div className={`download-status-pill ${blocked ? 'blocked' : 'pending'}`}>
              {blocked ? 'access revoked' : 'awaiting approval'}
            </div>
            <h1 className="download-title">{headline}</h1>
            <p className="download-subtitle">{copy}</p>
            {error ? <p className="download-error">{error}</p> : null}
          </div>

          <div className="download-hero-side">
            <div className="download-stack-card">
              <div className="download-mini-label">account status</div>
              <div className="download-mini-title">{tierLabel(access)}</div>
              <p className="download-mini-copy">
                {blocked
                  ? 'This account cannot download the beta right now.'
                  : 'Approval is still pending for this account.'}
              </p>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

function LoadingState({ session }) {
  return (
    <div className="download-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="download-shell">
        <section className="download-topbar">
          <div>
            <div className="download-kicker">beta access</div>
            <div className="download-user">{session?.user?.email || 'signed in'}</div>
          </div>
        </section>

        <section className="download-hero-card">
          <div className="download-hero-copy">
            <div className="download-status-pill pending">verifying access</div>
            <h1 className="download-title">checking your beta entitlement.</h1>
            <p className="download-subtitle">
              Recurdo is checking your beta access and getting your download ready.
            </p>
          </div>
        </section>
      </div>
    </div>
  )
}

function DownloadApp() {
  const [session, setSession] = useState(null)
  const [statusPayload, setStatusPayload] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [downloadBusy, setDownloadBusy] = useState(false)

  const loadStatus = async (accessToken) => {
    setLoading(true)
    setError('')
    try {
      const payload = await fetchAccessStatus(accessToken)
      setStatusPayload(payload)
    } catch (nextError) {
      const status = nextError?.statusCode || 0
      if (status === 401) {
        await signOutAccess().catch(() => {})
        setSession(null)
        setStatusPayload(null)
        setError('Your session expired. Please sign in again.')
        return
      }
      if (status === 403) {
        setStatusPayload({
          authenticated: true,
          user: {
            id: session?.user?.id || '',
            email: session?.user?.email || '',
            name: session?.user?.user_metadata?.full_name || '',
          },
          access: nextError?.payload?.access || {
            has_access: false,
            tier: 'pending',
            days_remaining: 0,
            expires_at: null,
            is_blocked: false,
          },
        })
        setError(nextError instanceof Error ? nextError.message : 'Beta access is not active for this account yet.')
        return
      }
      setError(nextError instanceof Error ? nextError.message : 'Could not verify beta access right now.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    const supabase = getAccessSupabaseClient()

    const bootstrap = async () => {
      try {
        const currentSession = await restoreAccessSessionFromUrl('/download.html')
        if (cancelled) {
          return
        }
        setSession(currentSession)
        if (currentSession?.access_token) {
          await loadStatus(currentSession.access_token)
        } else {
          setLoading(false)
        }
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : 'Could not restore session')
          setLoading(false)
        }
      }
    }

    void bootstrap()

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession)
      if (nextSession?.access_token) {
        void loadStatus(nextSession.access_token)
      } else {
        setStatusPayload(null)
        setLoading(false)
      }
    })

    return () => {
      cancelled = true
      subscription.subscription.unsubscribe()
    }
  }, [])

  const access = statusPayload?.access || null
  const approved = Boolean(access?.has_access)
  const tone = statusTone(access)
  const featureCards = useMemo(() => ([
    {
      label: 'mac download',
      title: 'get the latest approved beta build',
      copy: 'Everything you need to install Recurdo on your Mac is available here once your account is approved.',
    },
    {
      label: 'beta access',
      title: access?.expires_at ? `access ends ${formatDate(access.expires_at)}` : 'your beta access is active',
      copy: 'Your account stays ready for download during the current beta window.',
    },
    {
      label: 'signed in',
      title: 'switch accounts anytime',
      copy: 'Need to test another email? Sign out here and come back with a different approved account.',
    },
  ]), [access?.expires_at])

  const handleDownload = async () => {
    if (!session?.access_token) {
      return
    }

    setDownloadBusy(true)
    setError('')
    try {
      const payload = await requestInstallerDownload(session.access_token)
      if (payload?.download_path) {
        window.location.assign(payload.download_path)
      }
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not start the installer download')
    } finally {
      setDownloadBusy(false)
    }
  }

  const handleRefresh = async () => {
    if (!session?.access_token) {
      return
    }
    await loadStatus(session.access_token)
  }

  if (!session) {
    return <LoginScreen error={error} />
  }

  if (loading && !statusPayload) {
    return <LoadingState session={session} />
  }

  if (!approved) {
    return <PendingState session={session} access={access} error={error} onRetry={handleRefresh} />
  }

  return (
    <div className="download-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="download-shell">
        <section className="download-topbar">
          <div>
            <div className="download-kicker">approved beta access</div>
            <div className="download-user">{session.user?.email || 'signed in'}</div>
          </div>
          <div className="download-actions">
            <button type="button" className="download-button download-button-secondary" onClick={() => void handleRefresh()} disabled={loading}>
              refresh status
            </button>
            <button type="button" className="download-button download-button-secondary" onClick={() => void signOutAccess()}>
              sign out
            </button>
          </div>
        </section>

        <section className="download-hero-card">
          <div className="download-hero-copy">
            <div className={`download-status-pill ${tone}`}>{tierLabel(access)}</div>
            <h1 className="download-title">your macOS beta is unlocked.</h1>
            <p className="download-subtitle">
              Your account is approved. Download the latest Recurdo DMG for macOS below.
            </p>

            <div className="download-metrics">
              <div className="download-metric">
                <span className="download-metric-label">status</span>
                <span className="download-metric-value">approved</span>
              </div>
              <div className="download-metric">
                <span className="download-metric-label">tier</span>
                <span className="download-metric-value">{tierLabel(access)}</span>
              </div>
              <div className="download-metric">
                <span className="download-metric-label">expires</span>
                <span className="download-metric-value">{formatDate(access?.expires_at)}</span>
              </div>
            </div>

            <div className="download-actions">
              <button
                type="button"
                className="download-button download-button-primary"
                onClick={() => void handleDownload()}
                disabled={downloadBusy}
              >
                {downloadBusy ? 'preparing dmg…' : 'download recurdo dmg'}
              </button>
              <a className="download-button download-button-secondary" href="/">
                visit website
              </a>
            </div>
            {error ? <p className="download-error">{error}</p> : null}
          </div>

          <div className="download-hero-side">
            <div className="download-glow-card">
              <div className="download-mini-label">release access</div>
              <div className="download-mini-title">latest macOS beta</div>
              <p className="download-mini-copy">
                Built for approved testers only. If you reinstall later, just come back here and
                sign in again with the same approved account.
              </p>
            </div>
          </div>
        </section>

        <section className="download-feature-grid">
          {featureCards.map((card) => (
            <article key={card.label} className="download-feature-card">
              <div className="download-mini-label">{card.label}</div>
              <h2 className="download-feature-title">{card.title}</h2>
              <p className="download-feature-copy">{card.copy}</p>
            </article>
          ))}
        </section>
      </div>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <DownloadApp />
  </React.StrictMode>,
)
