import { useRef, useState } from 'react'
import { TurnstileWidget } from './TurnstileWidget'
import { TURNSTILE_ENABLED } from './turnstileConfig'
import { createWaitlistSignup } from './waitlist'

const HONEYPOT_STYLE = {
  position: 'absolute',
  left: '-9999px',
  width: '1px',
  height: '1px',
  opacity: 0,
  pointerEvents: 'none',
}

export function LandingNav({ waitlistHref = '#waitlist' }) {
  return (
    <nav className="landing-nav">
      <div className="landing-container landing-nav-inner">
        <a href="./index.html" className="landing-brand">
          <img src="/recurdo-logo.svg" alt="Recurdo" className="landing-brand-logo" />
        </a>
        <a href={waitlistHref} className="landing-nav-link">
          join waitlist
        </a>
      </div>
    </nav>
  )
}

export function LandingFooter() {
  return (
    <footer className="landing-footer">
      <div className="landing-container landing-footer-inner">
        <div className="landing-footer-brand">
          <img src="/recurdo-logo.svg" alt="Recurdo" className="landing-footer-logo" />
        </div>
        <div className="landing-footer-links">
          <a href="./privacy.html">privacy policy</a>
          <span>·</span>
          <a href="./terms.html">terms of use</a>
        </div>
        <div className="landing-footer-note">made with ↯ local AI</div>
      </div>
    </footer>
  )
}

export function WaitlistForm({ note }) {
  const [email, setEmail] = useState('')
  const [honeypot, setHoneypot] = useState('')
  const [startedAt] = useState(() => Date.now())
  const [submitted, setSubmitted] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [turnstileToken, setTurnstileToken] = useState('')
  const [turnstileArmed, setTurnstileArmed] = useState(false)
  const turnstileRef = useRef(null)

  const armTurnstile = () => {
    if (TURNSTILE_ENABLED) {
      setTurnstileArmed(true)
    }
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    const normalized = email.trim()
    if (!normalized) {
      return
    }

    armTurnstile()

    if (TURNSTILE_ENABLED && !turnstileToken) {
      setErrorMessage('complete the security check and try again')
      return
    }

    setSubmitting(true)
    setErrorMessage('')
    try {
      await createWaitlistSignup(normalized, {
        source: 'frontend-landing',
        honeypot,
        elapsedMs: Date.now() - startedAt,
        turnstileToken,
      })
      setSubmitted(true)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'could not join right now')
      turnstileRef.current?.reset()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="landing-form-block">
      {!submitted ? (
        <form onSubmit={handleSubmit}>
          <div className="landing-form-row">
            <input
              className="landing-input"
              type="email"
              placeholder="your@email.com"
              value={email}
              onChange={(event) => {
                armTurnstile()
                setEmail(event.target.value)
              }}
              onFocus={armTurnstile}
              required
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
            <button className="landing-button" type="submit" disabled={submitting}>
              {submitting ? 'joining...' : 'join waitlist'}
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
                appearance="always"
                errorClassName="landing-note"
                onTokenChange={(token) => {
                  setTurnstileToken(token)
                  if (errorMessage) {
                    setErrorMessage('')
                  }
                }}
              />
            </div>
          ) : null}
        </form>
      ) : (
        <p className="landing-success">
          ✓ you're on the list — we'll reach out when it's ready
        </p>
      )}
      {errorMessage ? <p className="landing-note">{errorMessage}</p> : null}
      <p className="landing-note">{note}</p>
    </div>
  )
}

export function LegalPage({ title, lede, sections }) {
  return (
    <div className="legal-shell">
      <LandingNav waitlistHref="./index.html#waitlist" />
      <main className="legal-content">
        <div className="landing-container">
          <h1 className="legal-title">{title}</h1>
          <p className="legal-lede">{lede}</p>

          {sections.map((section) => (
            <section key={section.title} className="legal-section">
              <h2>{section.title}</h2>
              {section.paragraphs?.map((paragraph) => (
                <p key={paragraph}>{paragraph}</p>
              ))}
              {section.items ? (
                <ul>
                  {section.items.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              ) : null}
            </section>
          ))}
        </div>
      </main>
      <LandingFooter />
    </div>
  )
}
