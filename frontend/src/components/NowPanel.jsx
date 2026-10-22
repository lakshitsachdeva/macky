import { useCallback, useEffect, useRef, useState } from 'react'
import { getNowFeed, logInteractionEvent, clickEvent, openItem, revealItem } from '../api/client'

/**
 * The "Now" side panel — proactive, query-less recommendations.
 *
 * Polls /now every POLL_INTERVAL_MS. We intentionally do not use WebSockets
 * for this: the feed's inputs (interaction_events, items.modified_at) change
 * slowly, and the backend already TTL-caches the response, so a cheap GET is
 * more than enough. It also keeps this panel working in "backend is just
 * FastAPI" deployments without the WS fallback logic.
 *
 * On user click: open the item via the existing /items/open helper AND log a
 * click event so the Phase 5 reranker learns from Now-feed traffic the same
 * way it learns from /search traffic.
 */

const POLL_INTERVAL_MS = 30_000
const DEFAULT_LIMIT = 7

function reasonIcon(reason) {
  if (!reason) return '·'
  if (reason.includes('just working')) return '↺'
  if (reason.includes('copied')) return '⌘'
  if (reason.includes('around now')) return '◷'
  if (reason.includes('Fresh') || reason.includes('updated')) return '✧'
  return '·'
}

export default function NowPanel({ authHeaders = {}, limit = DEFAULT_LIMIT }) {
  const [feed, setFeed] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const reqIdRef = useRef(0)

  const load = useCallback(
    async ({ refresh = false } = {}) => {
      const rid = reqIdRef.current + 1
      reqIdRef.current = rid
      setError(null)
      try {
        const payload = await getNowFeed({ limit, refresh }, authHeaders)
        if (reqIdRef.current !== rid) return
        setFeed(Array.isArray(payload?.items) ? payload.items : [])
      } catch (err) {
        if (reqIdRef.current !== rid) return
        // Don't blank the feed on transient errors — showing stale cards is
        // better UX than a flash of "error" when the backend is reloading.
        setError(err?.message || 'Unable to load suggestions')
      } finally {
        if (reqIdRef.current === rid) setLoading(false)
      }
    },
    [authHeaders, limit],
  )

  useEffect(() => {
    void load()
    const timerId = window.setInterval(() => { void load() }, POLL_INTERVAL_MS)
    // Refetch when the window gains focus — new day, new hour-of-day matches.
    const handleFocus = () => { void load({ refresh: true }) }
    window.addEventListener('focus', handleFocus)
    return () => {
      window.clearInterval(timerId)
      window.removeEventListener('focus', handleFocus)
    }
  }, [load])

  const handleOpen = useCallback(
    async (item, index) => {
      if (!item?.item_id) return
      // Fire-and-forget click log so reranker has full-funnel visibility.
      try {
        logInteractionEvent(clickEvent({
          itemId: item.item_id,
          query: null,
          rankPosition: index + 1,
        }))
      } catch (err) { void err }
      try {
        await openItem(item.item_id, authHeaders)
      } catch (err) {
        console.warn('NowPanel open failed', err)
        setError(err?.message || 'Unable to open file')
      }
    },
    [authHeaders],
  )

  const handleReveal = useCallback(
    async (item) => {
      if (!item?.item_id) return
      try {
        await revealItem(item.item_id, authHeaders)
      } catch (err) {
        console.warn('NowPanel reveal failed', err)
      }
    },
    [authHeaders],
  )

  return (
    <div className="surface-card now-panel" style={wrapStyle}>
      <div style={headerStyle}>
        <div style={titleStyle}>
          <span style={{ opacity: 0.6, fontSize: 10, letterSpacing: 1, textTransform: 'uppercase' }}>
            Right now
          </span>
          <span style={{ fontSize: 13, fontWeight: 500, color: 'rgba(255,255,255,0.92)' }}>
            On your mind
          </span>
        </div>
        <button
          type="button"
          onClick={() => void load({ refresh: true })}
          style={refreshBtnStyle}
          title="Refresh suggestions"
        >
          ↻
        </button>
      </div>

      {loading && feed.length === 0 ? (
        <div style={emptyStyle}>Looking at what you've been working on…</div>
      ) : feed.length === 0 ? (
        <div style={emptyStyle}>
          {error
            ? `Couldn't load suggestions: ${error}`
            : 'No suggestions yet — click a file or run a search and we\'ll start learning.'}
        </div>
      ) : (
        <ul style={listStyle}>
          {feed.map((item, index) => (
            <li
              key={item.item_id || index}
              style={rowStyle}
              onDoubleClick={() => void handleOpen(item, index)}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                <span style={iconStyle}>{reasonIcon(item.reason)}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={rowTitleStyle} title={item.title}>
                    {item.title || item.filename || item.item_id}
                  </div>
                  <div style={rowMetaStyle}>
                    <span>{item.reason}</span>
                    {item.source_type ? (
                      <>
                        <span style={{ opacity: 0.4 }}>·</span>
                        <span style={{ opacity: 0.6 }}>{item.source_type}</span>
                      </>
                    ) : null}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button
                    type="button"
                    style={actionBtnStyle}
                    onClick={() => void handleReveal(item)}
                    title="Reveal in Finder"
                  >
                    reveal
                  </button>
                  <button
                    type="button"
                    style={{ ...actionBtnStyle, background: 'rgba(200,170,90,0.12)', color: '#e4c685' }}
                    onClick={() => void handleOpen(item, index)}
                  >
                    open
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ── Inline styles — keep this component self-contained; don't leak a bunch
// of CSS selectors into the global sheet for a Phase-3 experimental surface.

const wrapStyle = { padding: 0, overflow: 'hidden' }

const headerStyle = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '14px 16px 10px',
  borderBottom: '1px solid rgba(255,255,255,0.05)',
}

const titleStyle = { display: 'flex', flexDirection: 'column', gap: 2 }

const refreshBtnStyle = {
  width: 26, height: 26, borderRadius: 13,
  background: 'rgba(255,255,255,0.04)',
  border: '1px solid rgba(255,255,255,0.08)',
  color: 'rgba(255,255,255,0.7)',
  cursor: 'pointer',
  fontSize: 13,
}

const emptyStyle = {
  padding: '16px',
  color: 'rgba(255,255,255,0.5)',
  fontSize: 12,
  lineHeight: 1.5,
}

const listStyle = { listStyle: 'none', margin: 0, padding: 4 }

const rowStyle = {
  padding: '10px 12px',
  borderRadius: 8,
  cursor: 'default',
}

const rowTitleStyle = {
  fontSize: 13,
  color: 'rgba(255,255,255,0.92)',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const rowMetaStyle = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  fontSize: 11,
  color: 'rgba(255,255,255,0.55)',
  marginTop: 2,
}

const iconStyle = {
  width: 20, height: 20, display: 'grid', placeItems: 'center',
  background: 'rgba(200,170,90,0.08)', color: '#e4c685',
  borderRadius: 6, fontSize: 11, flexShrink: 0,
}

const actionBtnStyle = {
  padding: '4px 10px',
  fontSize: 11,
  background: 'rgba(255,255,255,0.05)',
  border: '1px solid rgba(255,255,255,0.08)',
  borderRadius: 6,
  color: 'rgba(255,255,255,0.78)',
  cursor: 'pointer',
}
