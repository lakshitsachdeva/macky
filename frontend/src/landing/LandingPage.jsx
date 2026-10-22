import { LandingFooter, LandingNav, WaitlistForm } from './shared'

function StepIconEye() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

function StepIconBrain() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 4c-2.76 0-5 2.24-5 5v2c0 1.1-.9 2-2 2v2c1.1 0 2 .9 2 2v1c0 2.76 2.24 5 5 5h1v-4H9a2 2 0 0 1-2-2v-1a4 4 0 0 0-1.17-2.83A4 4 0 0 0 7 11V9a2 2 0 0 1 2-2h1V4H9Zm6 0h-1v4h1a2 2 0 0 1 2 2v2a4 4 0 0 0 1.17 2.83A4 4 0 0 0 17 17v1a2 2 0 0 1-2 2h-1v4h1c2.76 0 5-2.24 5-5v-1c0-1.1.9-2 2-2v-2c-1.1 0-2-.9-2-2V9c0-2.76-2.24-5-5-5Z"
        fill="currentColor"
      />
    </svg>
  )
}

function StepIconSearch() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M16 16l4.5 4.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

function LandingPage() {
  return (
    <div className="landing-shell">
      <LandingNav />

      <main>
        <section className="landing-container landing-hero">
          <div className="landing-pill">private beta</div>
          <h1 className="landing-hero-title">
            <span>find anything</span>
            <br />
            <span>you&apos;ve ever seen</span>
          </h1>
          <p className="landing-copy">
            Recurdo watches your downloads, screenshots, documents, and spreadsheets.
            Search by how you remember — not by filename.
          </p>

          <WaitlistForm note="no spam. just a note when the mac app is ready." />
        </section>

        <section className="landing-section centered">
          <div className="landing-container">
            <div className="landing-label">The Problem</div>
            <h2 className="landing-section-title">you know you have it</h2>

            <div className="landing-grid landing-grid-three">
              <article className="landing-card">
                <div className="landing-card-icon">📧</div>
                <div className="landing-card-title">it came in an email</div>
                <p className="landing-card-body">
                  An attachment from three weeks ago. You know who sent it.
                  You just can&apos;t find it in your downloads.
                </p>
              </article>

              <article className="landing-card">
                <div className="landing-card-icon">📸</div>
                <div className="landing-card-title">you screenshotted it</div>
                <p className="landing-card-body">
                  You saw something important and took a screenshot.
                  Now it&apos;s buried in 4,000 other screenshots.
                </p>
              </article>

              <article className="landing-card">
                <div className="landing-card-icon">📄</div>
                <div className="landing-card-title">
                  you definitely downloaded it
                </div>
                <p className="landing-card-body">
                  It&apos;s a PDF. It&apos;s somewhere. You&apos;ve opened it twice.
                  But you cannot remember what you named it.
                </p>
              </article>
            </div>
          </div>
        </section>

        <section className="landing-section centered">
          <div className="landing-container">
            <div className="landing-label">How It Works</div>
            <h2 className="landing-section-title">describe it. find it.</h2>

            <div className="landing-grid landing-grid-three">
              <article className="landing-card">
                <div className="landing-step-icon">
                  <StepIconEye />
                </div>
                <div className="landing-card-title">watches silently</div>
                <p className="landing-card-body">
                  Recurdo indexes your downloads, screenshots,
                  and local files in the background.
                </p>
              </article>

              <article className="landing-card">
                <div className="landing-step-icon">
                  <StepIconBrain />
                </div>
                <div className="landing-card-title">understands context</div>
                <p className="landing-card-body">
                  A local AI model reads every file and
                  builds a memory of what you&apos;ve seen.
                </p>
              </article>

              <article className="landing-card">
                <div className="landing-step-icon">
                  <StepIconSearch />
                </div>
                <div className="landing-card-title">finds anything</div>
                <p className="landing-card-body">
                  Describe it how you remember it.
                  Recurdo finds it in under a second.
                </p>
              </article>
            </div>
          </div>
        </section>

        <section className="landing-container">
          <div className="landing-privacy">
            <h2 className="landing-privacy-title">
              local by default. Gemini enrichment is optional.
            </h2>
            <div className="landing-stats">
              <div className="landing-stat">text stays local</div>
              <div className="landing-stat">Gemini optional</div>
              <div className="landing-stat">open source</div>
            </div>
            <p className="landing-privacy-copy">
              Text extraction, embeddings, OCR, and search run on your Mac.
              Optional Gemini integration can improve image and enrichment results,
              and only that path may send content to Google&apos;s API.
            </p>
          </div>
        </section>

        <section className="landing-section centered" id="waitlist">
          <div className="landing-container">
            <div className="landing-label">Waitlist</div>
            <h2 className="landing-section-title">be first to try it</h2>
            <p className="landing-copy">currently in private beta for macOS</p>
            <WaitlistForm note="23 people already on the waitlist" />
          </div>
        </section>
      </main>

      <LandingFooter />
    </div>
  )
}

export default LandingPage
