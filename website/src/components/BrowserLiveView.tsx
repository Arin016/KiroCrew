import { useCallback, useEffect, useRef, useState } from 'react'
import { Monitor, Maximize2, Minimize2, Minus } from 'lucide-react'

/**
 * BrowserLiveView — floating window that mirrors the headless [BROWSE] Chromium.
 *
 * On a cloud desktop the browse session runs headless on the dev host; this
 * panel mirrors it to the laptop over the dashboard's existing WS + reverse-SSH
 * tunnel. Frames arrive as `kiroclaw-browser-frame` window events (routed from
 * the WS `browser_frame` message in useWebSocket) — each is a screenshot the
 * agent already captured, forwarded by the Playwright MCP proxy.
 *
 * Self-contained, lifecycle-driven — there is no top-bar button. Three states:
 *   hidden → (first frame) → open (small)  ⇄  chip (corner)
 * - It stays hidden until the first frame, then auto-opens at the SMALL size so
 *   it's a non-disruptive corner thumbnail. A size toggle expands it to a large,
 *   readable view (and back).
 * - Minimizing collapses it to a tiny corner chip rather than destroying it; the
 *   chip is the re-open affordance and only exists while there's browse activity.
 *   New frames update the image but never force a collapsed panel back open.
 * Read-only by design (no debug port; interactive control is out of scope — Mesh-1998).
 */

interface FrameDetail {
  data: string
  format?: string
  device_width?: number | null
  device_height?: number | null
}

type Mode = 'hidden' | 'chip' | 'open'
type Size = 'small' | 'large'

const WIDTHS: Record<Size, number> = { small: 150, large: 620 }

export default function BrowserLiveView() {
  const [mode, setMode] = useState<Mode>('hidden')
  const [size, setSize] = useState<Size>('small')
  const [frame, setFrame] = useState<string | null>(null)
  const [lastTs, setLastTs] = useState<number | null>(null)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{ dx: number; dy: number } | null>(null)

  // Frames auto-open the panel (small) the first time, so the user sees activity
  // even if they never opened it. Once it's open or collapsed to the chip, a new
  // frame only updates the image — it never forces a collapsed panel back open.
  useEffect(() => {
    const onFrame = (e: Event) => {
      const d = (e as CustomEvent<FrameDetail>).detail
      if (!d?.data) return
      setFrame(`data:image/${d.format || 'jpeg'};base64,${d.data}`)
      setLastTs(Date.now())
      setMode(m => (m === 'hidden' ? 'open' : m))
    }
    window.addEventListener('kiroclaw-browser-frame', onFrame)
    return () => window.removeEventListener('kiroclaw-browser-frame', onFrame)
  }, [])

  // Programmatic open⇄chip toggle. No UI button dispatches this today (the panel
  // is lifecycle-driven); kept as an internal hook for a future shortcut/command.
  useEffect(() => {
    const onToggle = () => setMode(m => (m === 'open' ? 'chip' : 'open'))
    window.addEventListener('kiroclaw-toggle-browser-live', onToggle)
    return () => window.removeEventListener('kiroclaw-toggle-browser-live', onToggle)
  }, [])

  const onHeaderPointerDown = useCallback((e: React.PointerEvent) => {
    dragRef.current = { dx: e.clientX - pos.x, dy: e.clientY - pos.y }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [pos])
  const onHeaderPointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragRef.current) return
    setPos({ x: e.clientX - dragRef.current.dx, y: e.clientY - dragRef.current.dy })
  }, [])
  const onHeaderPointerUp = useCallback(() => { dragRef.current = null }, [])

  if (mode === 'hidden') return null

  if (mode === 'chip') {
    return (
      <button
        className="fixed z-[60] bottom-4 right-4 flex items-center gap-2 px-3 py-2 rounded-full border border-border bg-card shadow-lg hover:bg-bg-hover transition-colors"
        onClick={() => setMode('open')}
        aria-label="Show live browser view"
        title="Show live browser view"
      >
        <Monitor size={14} className="text-muted" />
        <span className="text-[12px] font-medium text-text">Browser</span>
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${frame ? 'animate-pulse' : ''}`}
          style={{ backgroundColor: frame ? 'var(--ok)' : 'var(--muted)' }}
          aria-hidden
        />
      </button>
    )
  }

  return (
    <div
      className="fixed z-[60] flex flex-col rounded-xl border border-border bg-card shadow-xl overflow-hidden"
      style={{ right: 16 - pos.x, bottom: 16 - pos.y, width: WIDTHS[size], maxWidth: '92vw' }}
      role="dialog"
      aria-label="Live browser view"
    >
      <div
        className="flex items-center gap-2 px-3 py-2 border-b border-border cursor-move select-none"
        style={{ backgroundColor: 'var(--bg-elevated)' }}
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerUp}
        onPointerCancel={onHeaderPointerUp}
      >
        <Monitor size={14} className="shrink-0 text-muted" />
        <span className="text-[13px] font-medium text-text">Browser — live</span>
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${frame ? 'animate-pulse' : ''}`}
          style={{ backgroundColor: frame ? 'var(--ok)' : 'var(--muted)' }}
          aria-hidden
        />
        <div className="flex-1" />
        <button
          onPointerDown={e => e.stopPropagation()}
          onClick={() => setSize(s => (s === 'small' ? 'large' : 'small'))}
          aria-label={size === 'small' ? 'Expand live browser view' : 'Shrink live browser view'}
          title={size === 'small' ? 'Expand' : 'Shrink'}
          className="p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          {size === 'small' ? <Maximize2 size={13} /> : <Minimize2 size={13} />}
        </button>
        <button
          onPointerDown={e => e.stopPropagation()}
          onClick={() => setMode('chip')}
          aria-label="Minimize live browser view to corner"
          title="Minimize to corner"
          className="p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          <Minus size={14} />
        </button>
      </div>

      <div
        className="relative bg-black flex items-center justify-center"
        style={{ minHeight: size === 'small' ? 90 : 220 }}
      >
        {frame ? (
          <img src={frame} alt="Live browser session" className="w-full h-auto block" />
        ) : (
          <div className="flex flex-col items-center gap-2 py-8 text-muted">
            <Monitor size={18} />
            <span className="text-[11px]">Waiting for the browser to take a screenshot…</span>
          </div>
        )}
      </div>

      <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted flex items-center justify-between">
        <span>Read-only mirror{size === 'large' ? ' of the headless browse session' : ''}</span>
        {lastTs && <span>updated {new Date(lastTs).toLocaleTimeString()}</span>}
      </div>
    </div>
  )
}
