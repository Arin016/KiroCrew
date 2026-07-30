/**
 * Send user gestures on the live browse mirror back to the browser.
 *
 * The frame stream is one-directional (proxy → gateway → WS → <img>); this is the
 * return leg. Events POST to `/api/browser/input`, land in a per-session queue,
 * and the Playwright proxy long-polls for them.
 *
 * Two things this hook is responsible for:
 *
 * - **Coalescing.** A drag or scroll fires far faster than a ~1-10fps screenshot
 *   mirror can reflect, and every event costs a queue slot. Continuous verbs
 *   (`move`, `wheel`) are batched to one send per animation frame, following the
 *   same rAF-flush pattern `useWebSocket` uses for chunk streams. Discrete verbs
 *   (`click`, `key`, `drag`, `resize`) are sent immediately — dropping one would
 *   lose a real intent.
 * - **Normalized coordinates.** Positions are sent as 0..1 fractions of the
 *   frame, never pixels. The panel's idea of the browser viewport is always one
 *   frame stale, so the proxy does the pixel conversion against the size it
 *   learned from the newest frame. Sending pixels would misplace clicks for one
 *   frame after every resize.
 */
import { useCallback, useEffect, useRef } from 'react'

export type BrowserInputEvent =
  | { verb: 'click'; x: number; y: number; button?: 'left' | 'right' | 'middle'; clickCount?: number }
  | { verb: 'move'; x: number; y: number }
  | { verb: 'drag'; x: number; y: number; x2: number; y2: number }
  | { verb: 'wheel'; dx: number; dy: number }
  | { verb: 'key'; key: string }
  | { verb: 'resize'; width: number; height: number }

/** Verbs where only the latest value matters, so intermediate ones can be dropped. */
const COALESCED = new Set(['move', 'wheel'])

export function useBrowserInput(sessionKey: string | null | undefined) {
  // Latest pending event per coalesced verb, flushed on the next animation frame.
  // Each entry carries the session key it was created for: reading the ref at
  // flush time would route a gesture into whichever session is active THEN, so
  // switching slots inside one frame could deliver session A's drag to session B.
  const pendingRef = useRef<Map<string, { event: BrowserInputEvent; sessionKey: string }>>(new Map())
  const rafRef = useRef<number | null>(null)
  const sessionRef = useRef(sessionKey)
  sessionRef.current = sessionKey

  const postTo = useCallback((event: BrowserInputEvent, key: string) => {
    if (!key) return
    // Fire-and-forget: a dropped gesture must never surface an error dialog, and
    // the next frame shows the user whether it landed.
    void fetch('/api/browser/input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: JSON.stringify({ ...event, session_key: key }),
    }).catch(() => {})
  }, [])

  const flush = useCallback(() => {
    rafRef.current = null
    const pending = pendingRef.current
    if (pending.size === 0) return
    for (const { event, sessionKey: key } of pending.values()) postTo(event, key)
    pending.clear()
  }, [postTo])

  const send = useCallback((event: BrowserInputEvent) => {
    const key = sessionRef.current
    if (!key) return
    if (!COALESCED.has(event.verb)) {
      postTo(event, key)
      return
    }
    const pending = pendingRef.current
    if (event.verb === 'wheel') {
      const prev = pending.get('wheel')
      // Accumulate rather than replace: two notches in one frame should scroll
      // twice as far, not once. Only merge within the same session.
      if (prev && prev.event.verb === 'wheel' && prev.sessionKey === key) {
        event = { verb: 'wheel', dx: prev.event.dx + event.dx, dy: prev.event.dy + event.dy }
      }
    }
    pending.set(event.verb, { event, sessionKey: key })
    if (rafRef.current === null) rafRef.current = requestAnimationFrame(flush)
  }, [flush, postTo])

  useEffect(() => () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    pendingRef.current.clear()
  }, [])

  return send
}
