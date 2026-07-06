import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { normalizeRect, cropCanvas, canvasToFile, type SnipRect } from '../hooks/useScreenSnip'

interface Props {
  /** Captured full-screen frame to crop. */
  frame: HTMLCanvasElement
  /** Called with the cropped PNG File. */
  onComplete: (file: File) => void
  /** Called when the user cancels (Escape or Cancel). */
  onCancel: () => void
  /** Called with a user-facing message when capture/encode fails. */
  onError?: (message: string) => void
}

/** Minimum drag size (display px) below which a release is treated as a stray click, not a capture. */
const MIN_SNIP_PX = 6

/**
 * Fullscreen crop surface with a seamless, OS-native interaction: the user drags
 * a rectangle over the captured frame and the region is captured immediately on
 * release (like macOS Cmd+Shift+4 / Windows Win+Shift+S) — no confirm button.
 * Sub-threshold drags are ignored so a stray click never captures. The cropped
 * region (mapped from display px to source px) becomes a PNG File that flows
 * through the existing image-upload pipeline.
 */
export default function SnipOverlay({ frame, onComplete, onCancel, onError }: Props) {
  const [start, setStart] = useState<{ x: number; y: number } | null>(null)
  const [rect, setRect] = useState<SnipRect | null>(null)
  const rectRef = useRef<SnipRect | null>(null)
  const surfaceRef = useRef<HTMLDivElement>(null)

  // toDataURL on a full-screen canvas is expensive (~50-100ms); memoize so it
  // runs once per frame, not on every drag-induced re-render.
  const dataUrl = useMemo(() => frame.toDataURL(), [frame])

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onCancel])

  const rel = (e: React.MouseEvent) => {
    const r = surfaceRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0, w: 0, h: 0 }
    return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height }
  }

  /** Crop the given display-space rect from the source frame and hand off the PNG File. */
  /** Surface a failure to the user (if a handler is wired) and close the overlay. */
  const onFail = useCallback(() => {
    onError?.('Could not capture the selected region. Please try again.')
    onCancel()
  }, [onError, onCancel])

  const capture = useCallback((r: SnipRect) => {
    const surf = surfaceRef.current?.getBoundingClientRect()
    if (!surf) return
    const scale = frame.width / (surf.width || frame.width)
    const source: SnipRect = {
      x: Math.round(r.x * scale),
      y: Math.round(r.y * scale),
      width: Math.round(r.width * scale),
      height: Math.round(r.height * scale),
    }
    canvasToFile(cropCanvas(frame, source)).then(onComplete, onFail)
  }, [frame, onComplete, onFail])

  const onDown = (e: React.MouseEvent) => {
    const p = rel(e)
    setStart({ x: p.x, y: p.y })
    setRect(null)
    rectRef.current = null
  }

  const onMove = (e: React.MouseEvent) => {
    if (!start) return
    const p = rel(e)
    const next = normalizeRect(start, { x: p.x, y: p.y }, { width: p.w, height: p.h })
    rectRef.current = next
    setRect(next)
  }

  // Release = capture (seamless). Sub-threshold drags are discarded as stray clicks.
  const finish = useCallback(() => {
    setStart(null)
    const r = rectRef.current
    if (r && r.width >= MIN_SNIP_PX && r.height >= MIN_SNIP_PX) {
      capture(r)
    } else {
      setRect(null)
      rectRef.current = null
    }
  }, [capture])

  // Finalize on release ANYWHERE (even off the surface / over the backdrop) so a
  // drag that ends outside the image still captures instead of getting stuck.
  useEffect(() => {
    if (!start) return
    const up = () => finish()
    window.addEventListener('mouseup', up)
    return () => window.removeEventListener('mouseup', up)
  }, [start, finish])

  const hasSelection = !!rect && rect.width > 0 && rect.height > 0

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Crop screen capture"
      className="fixed inset-0 z-[200] bg-black/50 flex flex-col items-center justify-center gap-4"
    >
      {/* Freeform pointer-drag crop region (like macOS Cmd+Shift+4). There is no
          ARIA role that models "drag a rectangle to select", and there is no
          per-pixel keyboard equivalent; Escape-to-cancel is wired globally on the
          window (see the keydown effect above) and the parent is role="dialog".
          Hence the scoped disable for the mouse-only drag surface. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        ref={surfaceRef}
        data-testid="snip-surface"
        aria-label="Drag to select the screen region to capture"
        className="relative max-w-[90vw] max-h-[80vh] cursor-crosshair select-none"
        onMouseDown={onDown}
        onMouseMove={onMove}
      >
        <img
          src={dataUrl}
          alt="Screen capture — drag the area you want; it attaches automatically on release"
          draggable={false}
          className="block max-w-[90vw] max-h-[80vh] pointer-events-none"
        />
        {hasSelection && (
          <div
            className="absolute border-2 border-accent bg-accent/10 pointer-events-none"
            style={{ left: rect!.x, top: rect!.y, width: rect!.width, height: rect!.height }}
          />
        )}
      </div>
      <div className="flex items-center gap-3">
        <span className="text-[12px] text-muted">Drag to capture an area · release to attach · Esc to cancel</span>
        <button
          onClick={onCancel}
          aria-label="Cancel screen snip"
          className="h-8 px-3 rounded-lg text-[13px] text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border cursor-pointer transition-all"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
