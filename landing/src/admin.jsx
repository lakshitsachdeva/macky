import React, { useEffect, useMemo, useState } from 'react'
import ReactDOM from 'react-dom/client'
import {
  fetchAdminOverview,
  getAdminSupabaseClient,
  restoreAdminSessionFromUrl,
  runAdminAction,
  signInAdminWithGoogle,
  signOutAdmin,
} from './lib/admin'
import './index.css'
import './admin.css'

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

function normalize(text) {
  return String(text || '').trim().toLowerCase()
}

function summarizeCounts(overview) {
  const requests = Array.isArray(overview?.requests) ? overview.requests : []
  const grants = Array.isArray(overview?.grants) ? overview.grants : []

  return {
    pendingRequests: requests.filter((row) => row.status === 'pending').length,
    approvedRequests: requests.filter((row) => row.status === 'approved' || row.status === 'invited').length,
    activeGrants: grants.filter((row) => !row.is_blocked).length,
    blockedGrants: grants.filter((row) => row.is_blocked).length,
  }
}

function LoginState({ error }) {
  return (
    <div className="admin-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="admin-login-wrap">
        <div className="admin-login-card">
          <div className="admin-kicker">recurdo admin</div>
          <div className="admin-title">manage access</div>
          <p className="admin-subtitle">
            Review waitlist requests, grant 90-day beta access, and revoke runtime entitlements
            from one place. This page only works for explicitly approved admin accounts.
          </p>
          <div className="admin-actions" style={{ marginTop: 20, justifyContent: 'flex-start' }}>
            <button type="button" className="admin-button" onClick={() => void signInAdminWithGoogle()}>
              sign in with google
            </button>
          </div>
          {error ? <p className="admin-error" style={{ marginTop: 18 }}>{error}</p> : null}
        </div>
      </div>
    </div>
  )
}

function AdminApp() {
  const [session, setSession] = useState(null)
  const [overview, setOverview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyKey, setBusyKey] = useState('')
  const [grantEmail, setGrantEmail] = useState('')
  const [grantReason, setGrantReason] = useState('')
  const [search, setSearch] = useState('')

  const loadOverview = async (accessToken) => {
    setLoading(true)
    setError('')
    try {
      const payload = await fetchAdminOverview(accessToken)
      setOverview(payload)
    } catch (nextError) {
      const status = nextError?.statusCode || 0
      if (status === 401 || status === 403) {
        await signOutAdmin().catch(() => {})
        setSession(null)
        setOverview(null)
        setError(status === 403
          ? 'This Google account is not authorized for the admin dashboard.'
          : 'Your admin session expired. Please sign in again.')
        return
      }
      setError(nextError instanceof Error ? nextError.message : 'Could not load admin dashboard')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    const supabase = getAdminSupabaseClient()

    const bootstrap = async () => {
      try {
        const currentSession = await restoreAdminSessionFromUrl()
        if (cancelled) {
          return
        }
        setSession(currentSession)
        if (currentSession?.access_token) {
          await loadOverview(currentSession.access_token)
        } else {
          setLoading(false)
        }
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : 'Could not restore admin session')
          setLoading(false)
        }
      }
    }

    void bootstrap()

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession)
      if (nextSession?.access_token) {
        void loadOverview(nextSession.access_token)
      } else {
        setOverview(null)
        setLoading(false)
      }
    })

    return () => {
      cancelled = true
      subscription.subscription.unsubscribe()
    }
  }, [])

  const counts = useMemo(() => summarizeCounts(overview), [overview])

  const filteredRequests = useMemo(() => {
    const rows = Array.isArray(overview?.requests) ? overview.requests : []
    const q = normalize(search)
    if (!q) {
      return rows
    }
    return rows.filter((row) => {
      const haystack = [
        row.email,
        row.status,
        row.source,
        row.matched_profile?.name,
        row.matched_profile?.email,
        row.access?.tier,
      ]
        .filter(Boolean)
        .join(' ')
      return normalize(haystack).includes(q)
    })
  }, [overview?.requests, search])

  const filteredGrants = useMemo(() => {
    const rows = Array.isArray(overview?.grants) ? overview.grants : []
    const q = normalize(search)
    if (!q) {
      return rows
    }
    return rows.filter((row) => {
      const haystack = [
        row.profile?.email,
        row.profile?.name,
        row.tier,
        row.notes,
      ]
        .filter(Boolean)
        .join(' ')
      return normalize(haystack).includes(q)
    })
  }, [overview?.grants, search])

  const runAction = async (key, payload) => {
    if (!session?.access_token) {
      return
    }

    setBusyKey(key)
    setError('')
    try {
      const result = await runAdminAction(session.access_token, payload)
      setOverview(result)
      if (payload.action === 'approve') {
        setGrantEmail('')
        setGrantReason('')
      }
    } catch (nextError) {
      const status = nextError?.statusCode || 0
      if (status === 401 || status === 403) {
        await signOutAdmin().catch(() => {})
        setSession(null)
        setOverview(null)
        setError(status === 403
          ? 'This Google account is not authorized for the admin dashboard.'
          : 'Your admin session expired. Please sign in again.')
        return
      }
      setError(nextError instanceof Error ? nextError.message : 'Could not update admin state')
    } finally {
      setBusyKey('')
    }
  }

  const handleRefresh = async () => {
    if (!session?.access_token) {
      return
    }
    await loadOverview(session.access_token)
  }

  if (!session) {
    return <LoginState error={error} />
  }

  return (
    <div className="admin-root">
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />

      <div className="admin-shell">
        <section className="admin-topbar">
          <div>
            <div className="admin-kicker">recurdo admin</div>
            <div className="admin-title">access control</div>
            <p className="admin-subtitle">
              Requests live in `waitlist_signups`. Real runtime grants live in `access`. Approving
              someone now creates or restores a 90-day beta entitlement.
            </p>
          </div>

          <div className="admin-actions">
            <div className="admin-user-chip">
              <span className="admin-chip-dot" />
              {session.user?.email || 'admin'}
            </div>
            <button type="button" className="admin-secondary-button" onClick={() => void handleRefresh()} disabled={loading}>
              refresh
            </button>
            <button
              type="button"
              className="admin-secondary-button"
              onClick={() => void signOutAdmin()}
              disabled={busyKey !== ''}
            >
              sign out
            </button>
          </div>
        </section>

        <section className="admin-summary-grid">
          <div className="admin-summary-card">
            <div className="admin-summary-label">pending requests</div>
            <div className="admin-summary-value">{counts.pendingRequests}</div>
            <div className="admin-summary-note">Fresh emails waiting for approval.</div>
          </div>
          <div className="admin-summary-card">
            <div className="admin-summary-label">approved requests</div>
            <div className="admin-summary-value">{counts.approvedRequests}</div>
            <div className="admin-summary-note">Approved or consumed request rows.</div>
          </div>
          <div className="admin-summary-card">
            <div className="admin-summary-label">active grants</div>
            <div className="admin-summary-value">{counts.activeGrants}</div>
            <div className="admin-summary-note">Users who currently pass runtime access checks.</div>
          </div>
          <div className="admin-summary-card">
            <div className="admin-summary-label">blocked grants</div>
            <div className="admin-summary-value">{counts.blockedGrants}</div>
            <div className="admin-summary-note">Users explicitly revoked in `access`.</div>
          </div>
        </section>

        <section className="admin-panel-grid">
          <div className="admin-panel">
            <div className="admin-panel-label">grant directly</div>
            <div className="admin-panel-title">approve by email</div>
            <p className="admin-panel-copy">
              This writes an approved request row and, if the person has already signed in, creates
              or restores their `access` row immediately for a fresh 90-day beta window.
            </p>

            <div className="admin-grant-form">
              <input
                className="admin-input"
                type="email"
                placeholder="person@email.com"
                value={grantEmail}
                onChange={(event) => setGrantEmail(event.target.value)}
              />
              <textarea
                className="admin-textarea"
                placeholder="optional note: founder, early tester, investor intro..."
                value={grantReason}
                onChange={(event) => setGrantReason(event.target.value)}
              />
              <div className="admin-actions" style={{ justifyContent: 'flex-start' }}>
                <button
                  type="button"
                  className="admin-button"
                  disabled={!grantEmail.trim() || busyKey === 'grant-email'}
                  onClick={() => void runAction('grant-email', {
                    action: 'approve',
                    email: grantEmail.trim(),
                    reason: grantReason.trim(),
                  })}
                >
                  {busyKey === 'grant-email' ? 'granting…' : 'approve email'}
                </button>
              </div>
            </div>

            {error ? <p className="admin-error" style={{ marginTop: 18 }}>{error}</p> : null}
          </div>

          <div className="admin-panel">
            <div className="admin-toolbar">
              <div>
                <div className="admin-panel-label">filter</div>
                <div className="admin-panel-title">requests and grants</div>
              </div>
              <input
                className="admin-search"
                type="search"
                placeholder="search by email, name, status..."
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <p className="admin-panel-copy">
              The request queue is separate from runtime access. Revoke from the access list when
              you want someone blocked immediately on the next verification pass.
            </p>
          </div>
        </section>

        <section className="admin-panel">
          <div className="admin-toolbar">
            <div>
              <div className="admin-panel-label">waitlist inbox</div>
              <div className="admin-panel-title">requests</div>
            </div>
            <div className="admin-muted">{loading ? 'loading…' : `${filteredRequests.length} visible`}</div>
          </div>

          <div className="admin-stack">
            {!filteredRequests.length ? (
              <div className="admin-empty">No matching requests.</div>
            ) : filteredRequests.map((row) => {
              const busyApprove = busyKey === `approve-${row.id}`
              const busyDeny = busyKey === `deny-${row.id}`
              const busyRevoke = busyKey === `revoke-request-${row.id}`
              return (
                <div key={row.id} className="admin-list-card">
                  <div className="admin-card-head">
                    <div>
                      <div className="admin-email">{row.email}</div>
                      <div className="admin-meta">
                        requested {formatDate(row.created_at)} · source {row.source}
                        {row.matched_profile?.id ? ` · signed in as ${row.matched_profile.id}` : ' · has not signed in yet'}
                        {row.access?.expires_at ? ` · beta expires ${formatDate(row.access.expires_at)}` : ''}
                      </div>
                    </div>
                    <div className="admin-badges">
                      <span className={`admin-badge ${row.status}`}>{row.status}</span>
                      {row.access?.is_blocked ? <span className="admin-badge blocked">blocked</span> : null}
                      {row.access && !row.access.is_blocked ? <span className="admin-badge invited">has access</span> : null}
                    </div>
                  </div>

                  <div className="admin-actions-row">
                    <button
                      type="button"
                      className="admin-inline-button"
                      disabled={busyApprove}
                      onClick={() => void runAction(`approve-${row.id}`, {
                        action: 'approve',
                        email: row.email,
                        reason: 'approved from request queue',
                      })}
                    >
                      {busyApprove ? 'approving…' : 'approve'}
                    </button>
                    <button
                      type="button"
                      className="admin-inline-button"
                      disabled={busyDeny}
                      onClick={() => void runAction(`deny-${row.id}`, {
                        action: 'deny',
                        requestId: row.id,
                        email: row.email,
                        reason: 'denied from request queue',
                      })}
                    >
                      {busyDeny ? 'denying…' : 'deny'}
                    </button>
                    {row.access ? (
                      <button
                        type="button"
                        className="admin-inline-button"
                        disabled={busyRevoke}
                        onClick={() => void runAction(`revoke-request-${row.id}`, {
                          action: 'revoke',
                          userId: row.access.user_id,
                          email: row.email,
                          reason: 'revoked from request queue',
                        })}
                      >
                        {busyRevoke ? 'revoking…' : 'revoke'}
                      </button>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        <section className="admin-panel">
          <div className="admin-toolbar">
            <div>
              <div className="admin-panel-label">runtime grants</div>
              <div className="admin-panel-title">access table</div>
            </div>
            <div className="admin-muted">{loading ? 'loading…' : `${filteredGrants.length} visible`}</div>
          </div>

          <div className="admin-stack">
            {!filteredGrants.length ? (
              <div className="admin-empty">No matching grants.</div>
            ) : filteredGrants.map((row) => {
              const busyRevoke = busyKey === `revoke-${row.id}`
              const busyRestore = busyKey === `restore-${row.id}`
              return (
                <div key={row.id} className="admin-list-card">
                  <div className="admin-card-head">
                    <div>
                      <div className="admin-email">{row.profile?.email || row.user_id}</div>
                      <div className="admin-meta">
                        {row.profile?.name || 'unknown user'} · granted {formatDate(row.beta_started_at)} · tier {row.tier}
                        {row.expires_at ? ` · expires ${formatDate(row.expires_at)}` : ''}
                        {row.notes ? ` · ${row.notes}` : ''}
                      </div>
                    </div>
                    <div className="admin-badges">
                      <span className={`admin-badge ${row.is_blocked ? 'blocked' : 'invited'}`}>
                        {row.is_blocked ? 'blocked' : 'active'}
                      </span>
                    </div>
                  </div>

                  <div className="admin-actions-row">
                    {row.is_blocked ? (
                      <button
                        type="button"
                        className="admin-inline-button"
                        disabled={busyRestore}
                        onClick={() => void runAction(`restore-${row.id}`, {
                          action: 'restore',
                          userId: row.user_id,
                          email: row.profile?.email || '',
                          reason: 'restored from admin dashboard',
                        })}
                      >
                        {busyRestore ? 'restoring…' : 'restore'}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="admin-inline-button"
                        disabled={busyRevoke}
                        onClick={() => void runAction(`revoke-${row.id}`, {
                          action: 'revoke',
                          userId: row.user_id,
                          email: row.profile?.email || '',
                          reason: 'revoked from admin dashboard',
                        })}
                      >
                        {busyRevoke ? 'revoking…' : 'revoke'}
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      </div>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AdminApp />
  </React.StrictMode>,
)
