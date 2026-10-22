import { useRef, useState } from 'react'
import StarField from '../components/StarField'
import { TurnstileWidget } from '../landing/TurnstileWidget'
import { TURNSTILE_ENABLED } from '../landing/turnstileConfig'
import { createWaitlistSignup } from '../landing/waitlist'

const HONEYPOT_STYLE = {
  position: 'absolute',
  left: '-9999px',
  width: '1px',
  height: '1px',
  opacity: 0,
  pointerEvents: 'none',
}

export default function BetaExpiredScreen({ user, onSignOut }) {
  const [email, setEmail] = useState(user?.email || '')
  const [honeypot, setHoneypot] = useState('')
  const [startedAt] = useState(() => Date.now())
  const [state, setState] = useState('idle')
  const [turnstileToken, setTurnstileToken] = useState('')
  const [turnstileArmed, setTurnstileArmed] = useState(false)
  const turnstileRef = useRef(null)

  const armTurnstile = () => {
    if (TURNSTILE_ENABLED) {
      setTurnstileArmed(true)
    }
  }

  const joinWaitlist = async () => {
    if (!email.includes('@')) return
    armTurnstile()
    if (TURNSTILE_ENABLED && !turnstileToken) {
      setState('error')
      return
    }
    setState('loading')

    try {
      await createWaitlistSignup(email.trim(), {
        source: 'beta_expired',
        honeypot,
        elapsedMs: Date.now() - startedAt,
        turnstileToken,
      })
      setState('done')
    } catch {
      setState('error')
      turnstileRef.current?.reset()
    }
  }

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
          position: 'absolute',
          width: 680,
          height: 680,
          borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(196,160,80,0.06) 0%, transparent 70%)',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          pointerEvents: 'none',
        }}
      />

      <div
        style={{
          position: 'relative',
          zIndex: 10,
          width: '100%',
          maxWidth: 540,
          margin: '0 32px',
          background: 'linear-gradient(135deg, rgba(196,160,80,0.12) 0%, rgba(196,160,80,0.04) 100%)',
          border: '1px solid rgba(196,160,80,0.25)',
          borderRadius: 20,
          padding: '60px 52px',
          textAlign: 'center',
          backdropFilter: 'blur(20px)',
          boxShadow: '0 24px 70px rgba(0,0,0,0.28)',
        }}
      >
        <div className="label" style={{ marginBottom: 20, color: 'rgba(196,160,80,0.5)' }}>
          currently in private beta for macos
        </div>

        <h1
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 'clamp(36px, 5vw, 56px)',
            fontWeight: 300,
            lineHeight: 1.1,
            color: 'rgba(196,160,80,0.9)',
            marginBottom: 8,
          }}
        >
          be first to
        </h1>
        <h1
          style={{
            fontFamily: 'var(--serif)',
            fontSize: 'clamp(36px, 5vw, 56px)',
            fontWeight: 300,
            fontStyle: 'italic',
            lineHeight: 1.1,
            color: 'rgba(196,160,80,0.5)',
            marginBottom: 18,
          }}
        >
          try it
        </h1>

        <p
          style={{
            margin: '0 auto 32px',
            maxWidth: 380,
            color: 'rgba(255,255,255,0.56)',
            fontSize: 14,
            lineHeight: 1.8,
          }}
        >
          Your beta window has ended, but your local index is still intact. Join the waitlist and
          we&apos;ll reach out first when access opens back up.
        </p>

        {state === 'done' ? (
          <div
            style={{
              padding: '16px 18px',
              borderRadius: 12,
              border: '1px solid rgba(196,160,80,0.25)',
              background: 'rgba(196,160,80,0.08)',
              color: 'rgba(196,160,80,0.92)',
              fontSize: 14,
            }}
          >
            you&apos;re on the list. we&apos;ll be in touch.
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                type="email"
                value={email}
                onChange={(event) => {
                  armTurnstile()
                  setEmail(event.target.value)
                  if (state === 'error') {
                    setState('idle')
                  }
                }}
                onFocus={armTurnstile}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    void joinWaitlist()
                  }
                }}
                placeholder="your@email.com"
                style={{
                  flex: 1,
                  padding: '12px 16px',
                  borderRadius: 8,
                  border: '1px solid rgba(196,160,80,0.2)',
                  background: 'rgba(0,0,0,0.3)',
                  color: 'rgba(196,160,80,0.8)',
                  fontSize: 14,
                  fontFamily: 'var(--sans)',
                  outline: 'none',
                  caretColor: 'var(--gold)',
                }}
              />
              <input
                type="text"
                tabIndex={-1}
                autoComplete="off"
                aria-hidden="true"
                value={honeypot}
                onChange={(event) => setHoneypot(event.target.value)}
                style={HONEYPOT_STYLE}
              />
              <button
                type="button"
                onClick={() => void joinWaitlist()}
                disabled={state === 'loading'}
                style={{
                  padding: '12px 24px',
                  borderRadius: 8,
                  border: 'none',
                  background: 'var(--gold)',
                  color: '#000',
                  fontSize: 14,
                  fontWeight: 500,
                  fontFamily: 'var(--sans)',
                  whiteSpace: 'nowrap',
                  opacity: state === 'loading' ? 0.72 : 1,
                }}
              >
                {state === 'loading' ? 'joining...' : 'join waitlist'}
              </button>
            </div>

            {turnstileArmed ? (
              <div
                style={{
                  marginTop: 14,
                  display: 'flex',
                  justifyContent: 'center',
                }}
              >
                <TurnstileWidget
                  ref={turnstileRef}
                  onTokenChange={(token) => {
                    setTurnstileToken(token)
                    if (state === 'error') {
                      setState('idle')
                    }
                  }}
                  errorClassName="mono"
                />
              </div>
            ) : null}

            {state === 'error' ? (
              <p
                className="mono"
                style={{
                  marginTop: 12,
                  fontSize: 10,
                  color: 'rgba(255,150,150,0.72)',
                }}
              >
                something went wrong. try again.
              </p>
            ) : null}
          </>
        )}

        <p
          className="mono"
          style={{
            marginTop: 20,
            fontSize: 9,
            color: 'rgba(196,160,80,0.3)',
            lineHeight: 1.9,
          }}
        >
          your indexed files are safe — they live on your machine
          <br />
          nothing was deleted
        </p>

        <button
          type="button"
          onClick={onSignOut}
          style={{
            marginTop: 24,
            background: 'none',
            border: 'none',
            color: 'rgba(255,255,255,0.15)',
            fontSize: 11,
            fontFamily: 'var(--sans)',
          }}
        >
          sign out
        </button>
      </div>
    </div>
  )
}
