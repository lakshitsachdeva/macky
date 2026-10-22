import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import Galaxy from '@/components/ui/Galaxy'
import BlurText from '@/components/ui/BlurText'
import { TURNSTILE_ENABLED, TurnstileWidget } from '@/components/TurnstileWidget'
import { createWaitlistSignup, fetchWaitlistCount } from '@/lib/supabase'

const HONEYPOT_STYLE = {
  position: 'absolute',
  left: '-9999px',
  width: '1px',
  height: '1px',
  opacity: 0,
  pointerEvents: 'none',
}

const TURNSTILE_STYLE = {
  marginTop: '14px',
  display: 'flex',
  justifyContent: 'flex-start',
}

const SEARCH_QUERIES = [
  {
    text: 'the deck before our first investor calls',
    badge: 'PD',
    name: 'recurdo_seed_story_v12.key',
    meta: 'Pitch deck · edited late night before outreach week · shared with two investors',
    detail: 'Matched through project, timeframe, and who you were emailing.',
    time: '0.34s',
    chips: ['project memory', 'time-aware', 'email context'],
  },
  {
    text: 'emails from Sarah about the contractor invoice',
    badge: 'GM',
    name: 'thread_sarah_contractor_invoice.eml',
    meta: 'Gmail thread · invoice follow-up · attachment included · last touched 6 days ago',
    detail: 'Pulled the thread and the attachment instead of making you guess which app it lived in.',
    time: '0.29s',
    chips: ['gmail', 'attachments', 'person-aware'],
  },
  {
    text: 'what was I working on last February',
    badge: 'TL',
    name: 'february_focus_briefing',
    meta: 'Briefing · hiring, landing page rewrite, benchmark fixes · 12 related items',
    detail: 'Sometimes the answer is not one file. It is the shape of a whole stretch of work.',
    time: '0.41s',
    chips: ['timeline', 'briefing', 'cross-source'],
  },
  {
    text: 'the screenshot with the blue apartment tiles',
    badge: 'SS',
    name: 'Screenshot_apartment_tiles_blue.png',
    meta: 'Screenshots · apartment search · bookmarked from browser · 11 days ago',
    detail: 'Vague visual memory still lands on the exact item.',
    time: '0.26s',
    chips: ['visual memory', 'screenshots', 'fast'],
  },
]

const HERO_SIGNALS = [
  'Ask by person, project, timeframe, or fuzzy memory',
  'Pulls from downloads, screenshots, docs, and Gmail',
  'Private beta on macOS with local-first search',
]

const PROOF_STATS = [
  {
    value: '95.2%',
    label: 'top-1 retrieval on the live internal benchmark',
  },
  {
    value: '2,993',
    label: 'memories already indexed in the current real library',
  },
  {
    value: '3,309',
    label: 'watched files feeding the memory surface',
  },
  {
    value: '155',
    label: 'tests passed before this beta push',
  },
]

const STORY_CARDS = [
  {
    index: '01',
    title: 'You remember the scene, not the filename.',
    body: 'Most search tools demand the exact folder, exact title, exact wording. Recurdo lets you ask the way your brain actually recalls things: from who, around when, for what project, with what feeling.',
  },
  {
    index: '02',
    title: 'The answer can be a file, a thread, or a quick briefing.',
    body: 'Sometimes you want the PDF. Sometimes you want the email thread. Sometimes you want the five-second recap before a meeting. Recurdo is built for all three.',
  },
  {
    index: '03',
    title: 'It makes old work feel usable again.',
    body: 'That buried deck, that invoice loop, that half-forgotten screenshot, that stretch of work from February. The point is not search for search\'s sake. The point is getting momentum back.',
  },
]

const CAPABILITY_CARDS = [
  {
    badge: 'PEOPLE',
    accent: 'sky',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
        <path d="M16 3.13a4 4 0 0 1 0 7.75" />
      </svg>
    ),
    title: 'Person-aware results',
    body: 'Search for the doc from Sarah, the thread with your contractor, or the deck you sent investors without memorizing filenames.',
  },
  {
    badge: 'TIME',
    accent: 'gold',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
    ),
    title: 'Timeline memory',
    body: 'Jump to last week, the month before launch, the period around a trip, or the days after a specific meeting.',
  },
  {
    badge: 'PROJECTS',
    accent: 'rose',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
    ),
    title: 'Project context',
    body: 'Results cluster around the workstream you mean instead of just matching nearby keywords.',
  },
  {
    badge: 'MAIL',
    accent: 'sky',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
        <polyline points="22,6 12,13 2,6" />
      </svg>
    ),
    title: 'Gmail and attachments',
    body: 'Email is not a separate universe anymore. Threads and attachments can show up in the same search flow as files on disk.',
  },
  {
    badge: 'BRIEFINGS',
    accent: 'gold',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
        <polyline points="10 9 9 9 8 9" />
      </svg>
    ),
    title: 'Quick catch-up mode',
    body: 'When you do not want to open ten things, Recurdo can help you re-enter the context fast and keep moving.',
  },
  {
    badge: 'LOCAL',
    accent: 'green',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      </svg>
    ),
    title: 'Private by default',
    body: 'Your search happens on your machine. The value is remembering your digital life without shipping your files away.',
  },
]

const HOW_STEPS = [
  {
    step: '01',
    title: 'Install in minutes',
    body: 'Drop the Mac app and it quietly starts watching your downloads, screenshots, docs, and Gmail. No configuration needed.',
  },
  {
    step: '02',
    title: 'It learns in the background',
    body: 'Recurdo builds a local index tied to people, projects, and time — without sending a single file off your machine.',
  },
  {
    step: '03',
    title: 'Search by memory, not filename',
    body: 'Ask naturally: "the deck before the investor calls" or "what Sarah sent about the invoice." Real recall, not keyword hunting.',
  },
  {
    step: '04',
    title: 'Get back momentum instantly',
    body: 'The right file, the thread, the quick context recap. One search that orients you instead of sending you on a hunt.',
  },
]

const PRIVACY_POINTS = [
  {
    title: 'Local-first',
    body: 'The product is designed so your actual search experience stays on your device.',
  },
  {
    title: 'Quiet in the background',
    body: 'It keeps up with your digital life without turning setup into a job.',
  },
  {
    title: 'Built for real memory',
    body: 'It is not trying to impress you with keywords. It is trying to return the thing you meant.',
  },
]

function readDesktopAuthPayload() {
  if (typeof window === 'undefined') {
    return null
  }

  const hash = window.location.hash.replace(/^#/, '')
  const search = window.location.search.replace(/^\?/, '')
  const merged = [search, hash].filter(Boolean).join('&')
  const params = new URLSearchParams(merged)

  const token = params.get('access_token') || ''
  const refreshToken = params.get('refresh_token') || ''
  const email = params.get('email') || ''
  const state = params.get('desktop_state') || params.get('state') || ''
  const error = params.get('error') || ''
  const errorDescription = params.get('error_description') || ''

  if (!token && !error) {
    return null
  }

  const payload = new URLSearchParams()
  if (token) payload.set('access_token', token)
  if (refreshToken) payload.set('refresh_token', refreshToken)
  if (email) payload.set('email', email)
  if (state) payload.set('state', state)
  if (error) payload.set('error', error)
  if (errorDescription) payload.set('error_description', errorDescription)

  return {
    error,
    errorDescription,
    deepLink: `recurdo://auth/callback?${payload.toString()}`,
  }
}

function useReveal() {
  useEffect(() => {
    const elements = Array.from(document.querySelectorAll('.reveal'))
    if (!elements.length) {
      return undefined
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible')
            observer.unobserve(entry.target)
          }
        })
      },
      { threshold: 0.08 },
    )

    elements.forEach((element) => observer.observe(element))
    return () => observer.disconnect()
  }, [])
}

function SearchDemo() {
  const [typed, setTyped] = useState('')
  const [showResult, setShowResult] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

  useEffect(() => {
    let cancelled = false
    const timeouts = new Set()

    const delay = (ms) =>
      new Promise((resolve) => {
        const timeoutId = window.setTimeout(() => {
          timeouts.delete(timeoutId)
          resolve()
        }, ms)
        timeouts.add(timeoutId)
      })

    const runSequence = async () => {
      let index = 0
      await delay(900)

      while (!cancelled) {
        const query = SEARCH_QUERIES[index]
        let current = ''
        setActiveIndex(index)
        setTyped('')
        setShowResult(false)
        await delay(180)

        for (const char of query.text) {
          if (cancelled) {
            return
          }
          current += char
          setTyped(current)
          await delay(34 + Math.random() * 24)
        }

        await delay(340)
        setShowResult(true)
        await delay(2200)

        for (let cursor = current.length; cursor >= 0; cursor -= 1) {
          if (cancelled) {
            return
          }
          setTyped(current.slice(0, cursor))
          await delay(18)
        }

        setShowResult(false)
        await delay(360)
        index = (index + 1) % SEARCH_QUERIES.length
      }
    }

    void runSequence()

    return () => {
      cancelled = true
      for (const timeoutId of timeouts) {
        window.clearTimeout(timeoutId)
      }
    }
  }, [])

  const query = SEARCH_QUERIES[activeIndex]

  return (
    <div className="demo-shell">
      <div className="demo-topline">
        <span>ask the way you remember</span>
        <span className="demo-status-dot">
          <span className="demo-live-pulse" />
          local results
        </span>
      </div>
      <div className={`search-wrap${typed ? ' focused' : ''}`}>
        <div className="search-text">
          {typed}
          <span className="search-cursor-blink" />
        </div>
      </div>
      <div className={`result-card${showResult ? ' visible' : ''}`}>
        <div className="result-badge">{query.badge}</div>
        <div className="result-info">
          <div className="result-name">{query.name}</div>
          <div className="result-meta">{query.meta}</div>
          <div className="result-detail">{query.detail}</div>
        </div>
        <div className="result-time">{query.time}</div>
      </div>
      <div className="result-chip-row">
        {query.chips.map((chip) => (
          <span key={chip} className="result-chip">
            {chip}
          </span>
        ))}
      </div>
    </div>
  )
}

function WaitlistForm({
  variant = 'hero',
  buttonId,
  inputId,
  note,
  waitlistCount,
  onSignedUp,
}) {
  const [email, setEmail] = useState('')
  const [honeypot, setHoneypot] = useState('')
  const [startedAt] = useState(() => Date.now())
  const [submitted, setSubmitted] = useState(false)
  const [invalid, setInvalid] = useState(false)
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

  const submit = async () => {
    if (submitting) {
      return
    }

    const trimmed = email.trim()
    if (!trimmed || !trimmed.includes('@')) {
      setInvalid(true)
      setErrorMessage('')
      return
    }

    setInvalid(false)
    setErrorMessage('')
    armTurnstile()

    if (TURNSTILE_ENABLED && !turnstileToken) {
      setErrorMessage('Complete the security check and try again.')
      return
    }

    setSubmitting(true)

    try {
      await createWaitlistSignup(trimmed, {
        source: 'landing-page',
        honeypot,
        elapsedMs: Date.now() - startedAt,
        turnstileToken,
      })
      setSubmitted(true)
      await onSignedUp?.()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Could not join right now.')
      turnstileRef.current?.reset()
    } finally {
      setSubmitting(false)
    }
  }

  const formClassName = variant === 'hero' ? 'waitlist-form' : 'cta-form'
  const inputClassName = variant === 'hero' ? 'waitlist-input' : 'cta-input'
  const buttonClassName = variant === 'hero' ? 'waitlist-btn' : 'cta-btn'
  const successClassName = variant === 'hero' ? 'waitlist-success' : 'cta-success'

  return (
    <div className={`form-shell form-shell-${variant}`}>
      {submitted ? (
        <div className={successClassName}>
          You&apos;re in. We&apos;ll reach out when the next wave opens.
        </div>
      ) : (
        <>
          <div className={formClassName}>
            <input
              id={inputId}
              className={inputClassName}
              type="email"
              placeholder="you@company.com"
              value={email}
              onChange={(event) => {
                armTurnstile()
                setEmail(event.target.value)
                if (invalid) {
                  setInvalid(false)
                }
              }}
              onFocus={armTurnstile}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  void submit()
                }
              }}
              style={invalid ? { borderColor: 'rgba(232, 108, 108, 0.6)' } : undefined}
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
              id={buttonId}
              className={buttonClassName}
              type="button"
              onClick={() => void submit()}
            >
              {submitting ? 'joining...' : 'join waitlist'}
            </button>
          </div>
          {turnstileArmed ? (
            <div style={TURNSTILE_STYLE}>
              <TurnstileWidget
                ref={turnstileRef}
                appearance="always"
                onTokenChange={(token) => {
                  setTurnstileToken(token)
                  if (errorMessage) {
                    setErrorMessage('')
                  }
                }}
              />
            </div>
          ) : null}
          {errorMessage ? <div className="waitlist-error">{errorMessage}</div> : null}
          {note ? <div className={variant === 'hero' ? 'waitlist-note' : 'cta-note'}>{note}</div> : null}
          {typeof waitlistCount === 'number' ? (
            <div className="cta-count">
              <span className="count-dot" />
              <span className="count-strong">{waitlistCount.toLocaleString()}</span>
              people already waiting for access
            </div>
          ) : null}
          {TURNSTILE_ENABLED ? (
            <div className="waitlist-meta">Protected by Cloudflare Turnstile.</div>
          ) : null}
        </>
      )}
    </div>
  )
}

function App() {
  useReveal()
  const [waitlistCount, setWaitlistCount] = useState(null)
  const [desktopAuthPayload] = useState(() => readDesktopAuthPayload())

  const loadWaitlistCount = async () => {
    try {
      const count = await fetchWaitlistCount()
      setWaitlistCount(count)
    } catch {
      setWaitlistCount(100)
    }
  }

  useEffect(() => {
    void loadWaitlistCount()
  }, [])

  useEffect(() => {
    if (!desktopAuthPayload?.deepLink) {
      return undefined
    }

    const timer = window.setTimeout(() => {
      window.location.replace(desktopAuthPayload.deepLink)
    }, 120)

    return () => window.clearTimeout(timer)
  }, [desktopAuthPayload])

  if (desktopAuthPayload?.deepLink) {
    return (
      <div className="desktop-auth-shell">
        <div className="noise" />
        <div className="orb orb-1" />
        <div className="orb orb-2" />

        <main className="desktop-auth-panel">
          <div className="nav-badge">desktop sign-in</div>
          <h1 className="desktop-auth-title">
            {desktopAuthPayload.error ? 'sign-in needs your attention' : 'returning you to recurdo'}
          </h1>
          <p className="desktop-auth-copy">
            {desktopAuthPayload.error
              ? desktopAuthPayload.errorDescription || desktopAuthPayload.error
              : 'Authentication finished in your browser. Recurdo should reopen automatically in a moment.'}
          </p>
          <div className="desktop-auth-actions">
            <a className="desktop-auth-button desktop-auth-button-primary" href={desktopAuthPayload.deepLink}>
              open recurdo
            </a>
            <button
              type="button"
              className="desktop-auth-button desktop-auth-button-secondary"
              onClick={() => window.location.replace(desktopAuthPayload.deepLink)}
            >
              try again
            </button>
          </div>
          <p className="desktop-auth-note">
            If the app does not come forward automatically, keep Recurdo open and press the button above once.
          </p>
        </main>
      </div>
    )
  }

  return (
    <>
      <div className="noise" />
      <div className="orb orb-1" />
      <div className="orb orb-2" />
      <div className="orb orb-3" />

      <div className="page">
        <nav className="site-nav">
          <a className="logo" href="#top" aria-label="Recurdo home">
            <img src="/recurdo-logo.svg" alt="Recurdo" className="logo-mark" />
          </a>
          <div className="nav-links">
            <a href="#how">how it works</a>
            <a href="#why">why recurdo</a>
            <a href="#waitlist" className="nav-link-cta">
              join waitlist
            </a>
          </div>
        </nav>

        <section className="hero" id="top">
          <div className="hero-galaxy">
            <Galaxy
              mouseRepulsion={false}
              mouseInteraction={false}
              density={1}
              glowIntensity={0.28}
              saturation={0}
              hueShift={0}
              twinkleIntensity={0.25}
              rotationSpeed={0.012}
              repulsionStrength={1.4}
              autoCenterRepulsion={0}
              starSpeed={0.5}
              speed={0.18}
              transparent
            />
          </div>

          <div className="hero-layout">
            <div className="hero-copy">
              <div className="hero-eyebrow">
                <span className="eyebrow-dot" />
                private beta for macOS
              </div>
              <h1 className="hero-title">
                your files finally
                <br />
                feel searchable
              </h1>

              <BlurText
                text="Recurdo turns your downloads, screenshots, documents, and email history into something you can actually retrieve by memory instead of by filename."
                className="hero-sub"
                delay={16}
                animateBy="words"
                direction="top"
              />

              <div className="hero-signal-list">
                {HERO_SIGNALS.map((signal, i) => (
                  <motion.div
                    key={signal}
                    className="hero-signal"
                    initial={{ opacity: 0, x: -12 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.6 + i * 0.12, duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
                  >
                    <span className="hero-signal-dot" />
                    <span>{signal}</span>
                  </motion.div>
                ))}
              </div>

              <motion.div
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 1.1, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
              >
                <WaitlistForm
                  variant="hero"
                  buttonId="joinBtn"
                  inputId="emailInput"
                  onSignedUp={loadWaitlistCount}
                  note={
                    waitlistCount === null
                      ? 'Loading current waitlist count...'
                      : `${waitlistCount.toLocaleString()} people already want early access.`
                  }
                />
              </motion.div>
            </div>

            <div className="hero-stage reveal visible">
              <SearchDemo />

              <div className="hero-proof-grid">
                <motion.article
                  className="hero-proof-card"
                  whileHover={{ y: -4, transition: { duration: 0.2 } }}
                >
                  <div className="hero-proof-label">benchmark</div>
                  <div className="hero-proof-value">95.2% top-1</div>
                  <p>on real internal retrieval, not toy demo prompts</p>
                </motion.article>
                <motion.article
                  className="hero-proof-card"
                  whileHover={{ y: -4, transition: { duration: 0.2 } }}
                >
                  <div className="hero-proof-label">coverage</div>
                  <div className="hero-proof-value">files + Gmail</div>
                  <p>screenshots, docs, threads, and attachments in one surface</p>
                </motion.article>
                <motion.article
                  className="hero-proof-card"
                  whileHover={{ y: -4, transition: { duration: 0.2 } }}
                >
                  <div className="hero-proof-label">feel</div>
                  <div className="hero-proof-value">search by memory</div>
                  <p>people, timing, project context, and the rough shape of what happened</p>
                </motion.article>
              </div>
            </div>
          </div>
        </section>

        <section className="how-section reveal" id="how">
          <div className="section-heading">
            <div className="section-kicker">from install to recall</div>
            <h2>Four steps to stop losing your own work.</h2>
          </div>
          <div className="how-steps">
            {HOW_STEPS.map((step, i) => (
              <article key={step.step} className="how-step">
                <div className="step-connector" aria-hidden="true" />
                <div className="step-num">{step.step}</div>
                <h3 className="step-title">{step.title}</h3>
                <p className="step-body">{step.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="metrics-band reveal" id="proof">
          <div className="section-heading">
            <div className="section-kicker">every number is real</div>
            <h2>Hard numbers behind every claim we make.</h2>
          </div>
          <div className="metrics-grid">
            {PROOF_STATS.map((stat) => (
              <motion.article
                key={stat.label}
                className="metric-card"
                whileHover={{ y: -6, transition: { duration: 0.22, ease: 'easeOut' } }}
              >
                <div className="metric-value">{stat.value}</div>
                <p>{stat.label}</p>
              </motion.article>
            ))}
          </div>
        </section>

        <section className="story-section reveal" id="why">
          <div className="section-heading">
            <div className="section-kicker">why this feels different</div>
            <h2>This is not a smarter spotlight. It is how your brain actually recalls things.</h2>
          </div>
          <div className="story-grid">
            {STORY_CARDS.map((card) => (
              <motion.article
                key={card.index}
                className="story-card"
                whileHover={{ y: -6, transition: { duration: 0.22, ease: 'easeOut' } }}
              >
                <div className="story-index">{card.index}</div>
                <h3>{card.title}</h3>
                <p>{card.body}</p>
              </motion.article>
            ))}
          </div>
        </section>

        <section className="capability-section reveal">
          <div className="section-heading">
            <div className="section-kicker">six things recurdo does better</div>
            <h2>One search can understand more than one clue at once.</h2>
          </div>
          <div className="capability-grid">
            {CAPABILITY_CARDS.map((card) => (
              <motion.article
                key={card.badge}
                className="capability-card"
                data-accent={card.accent}
                whileHover={{ y: -6, transition: { duration: 0.22, ease: 'easeOut' } }}
              >
                <div className="cap-icon-wrap">
                  <div className={`cap-icon cap-icon-${card.badge.toLowerCase()}`}>
                    {card.icon}
                  </div>
                  <div className="capability-badge">{card.badge}</div>
                </div>
                <h3>{card.title}</h3>
                <p>{card.body}</p>
              </motion.article>
            ))}
          </div>
        </section>

        <section className="command-section reveal">
          <div className="command-copy">
            <div className="section-kicker">more than a result list</div>
            <h2>Recurdo can help you re-enter the context, not just locate the file.</h2>
            <p>
              The most impressive moment is when you search once and immediately feel oriented
              again. A relevant file, the surrounding thread, the rough timeline, the quick recap.
              That is what makes people want it on their Mac.
            </p>
          </div>
          <div className="command-panels">
            <motion.article
              className="command-panel command-panel-brief"
              whileHover={{ y: -4, transition: { duration: 0.2 } }}
            >
              <div className="command-label">quick briefing</div>
              <h3>Hiring process picked back up this week.</h3>
              <p>
                You revisited candidate notes, opened two interview debrief docs, and replied to a
                Gmail thread about scheduling. The strongest related memory is the onboarding plan.
              </p>
            </motion.article>

            <motion.article
              className="command-panel command-panel-timeline"
              whileHover={{ y: -4, transition: { duration: 0.2 } }}
            >
              <div className="command-label">timeline cues</div>
              <ul className="timeline-list">
                <li>Feb 03: first draft of the landing rewrite</li>
                <li>Feb 06: benchmark pass and search tuning</li>
                <li>Feb 11: investor deck revision late at night</li>
                <li>Feb 14: Gmail follow-up and attachment resend</li>
              </ul>
            </motion.article>

            <motion.article
              className="command-panel command-panel-sources"
              whileHover={{ y: -4, transition: { duration: 0.2 } }}
            >
              <div className="command-label">one memory surface</div>
              <div className="source-chip-grid">
                <span>downloads</span>
                <span>screenshots</span>
                <span>pdfs</span>
                <span>gmail</span>
                <span>attachments</span>
                <span>notes</span>
              </div>
            </motion.article>
          </div>
        </section>

        <section className="trust-section reveal">
          <div className="section-heading">
            <div className="section-kicker">private by design</div>
            <h2>Your files never leave your machine.</h2>
          </div>
          <div className="trust-grid">
            {PRIVACY_POINTS.map((point) => (
              <motion.article
                key={point.title}
                className="trust-card"
                whileHover={{ y: -6, transition: { duration: 0.22, ease: 'easeOut' } }}
              >
                <h3>{point.title}</h3>
                <p>{point.body}</p>
              </motion.article>
            ))}
          </div>
        </section>

        <section className="cta-final reveal" id="waitlist">
          <div className="cta-shell">
            <div className="cta-copy">
              <div className="section-kicker section-kicker-dark">currently in private beta</div>
              <h2>
                this is the kind of app
                <br />
                people install the same day
              </h2>
              <p>
                Join the waitlist for early access to the Mac beta and be first in line when the
                next invites go out.
              </p>
            </div>

            <WaitlistForm
              variant="cta"
              waitlistCount={waitlistCount}
              onSignedUp={loadWaitlistCount}
            />
          </div>
        </section>

        <footer className="site-footer">
          <div className="footer-left">
            <img src="/recurdo-logo.svg" alt="Recurdo" className="footer-logo-mark" />
          </div>
          <div className="footer-center">
            <a href="/privacy/">privacy policy</a>
            <a href="/terms/">terms of use</a>
          </div>
          <div className="footer-right">Recurdo · local-first memory search · © 2026</div>
        </footer>
      </div>
    </>
  )
}

export default App
