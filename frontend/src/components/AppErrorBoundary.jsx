import { Component } from 'react'

function formatError(error) {
  if (!error) {
    return 'Unknown startup error'
  }

  if (typeof error === 'string') {
    return error
  }

  return error.message || error.toString() || 'Unknown startup error'
}

export default class AppErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = {
      error: null,
      info: null,
    }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Desktop app render failed:', error, info)
    this.setState({ info })
  }

  handleReload = () => {
    window.location.reload()
  }

  render() {
    const { error, info } = this.state
    if (!error) {
      return this.props.children
    }

    return (
      <div
        style={{
          width: '100vw',
          minHeight: '100vh',
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
            maxWidth: 560,
            borderRadius: 20,
            border: '1px solid rgba(196,160,80,0.18)',
            background: 'rgba(255,255,255,0.03)',
            boxShadow: '0 24px 70px rgba(0,0,0,0.28)',
            padding: '36px 32px',
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
            app startup issue
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
            the app hit a render error
          </h1>
          <p
            style={{
              margin: '16px 0 22px',
              color: 'rgba(255,255,255,0.56)',
              fontSize: 14,
              lineHeight: 1.7,
            }}
          >
            Recurdo is running, but the desktop UI failed while loading. Reloading usually
            recovers once the local backend is ready.
          </p>
          <div
            style={{
              padding: '14px 16px',
              borderRadius: 12,
              background: 'rgba(0,0,0,0.35)',
              border: '1px solid rgba(255,255,255,0.08)',
              color: 'rgba(255,255,255,0.76)',
              fontFamily: 'var(--mono)',
              fontSize: 11,
              lineHeight: 1.7,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {formatError(error)}
            {info?.componentStack ? `\n${info.componentStack.trim()}` : ''}
          </div>
          <div style={{ display: 'flex', gap: 10, marginTop: 20 }}>
            <button
              type="button"
              onClick={this.handleReload}
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
              reload
            </button>
          </div>
        </div>
      </div>
    )
  }
}
