import { safeSetItem } from '../utils/safeStorage'
import React, { useState, useEffect, useCallback, useRef } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { Btn } from './ui'

interface DetailPanelProps {
  title: React.ReactNode
  onClose: () => void
  footer?: React.ReactNode
  headerActions?: React.ReactNode
  /** Optional second toolbar rendered below the main header. Used to
   * separate identity/view actions (close, refresh, fullscreen, etc.)
   * from contextual editor controls (mode toggle, save, formatting).
   * Only renders when provided. */
  secondaryHeaderActions?: React.ReactNode
  initialWidth?: number
  minWidth?: number
  storageKey?: string
  children: React.ReactNode
  /** Drop the default px-5 py-4 children padding. Used by panels that fill the viewport themselves (e.g. Monaco diff). */
  noPadding?: boolean
  /** Override the header's default border-color/bg (e.g. to match an embedded editor). When provided, replaces the default `border-border bg-bg` styling. */
  headerClassName?: string
}

/**
 * Upper bound for the panel width: 60% of the current viewport. The panel is
 * `shrink-0` inside an `overflow-hidden` row (no horizontal scroll), and its
 * width is persisted per `storageKey`. A width sized on a wide external
 * monitor would otherwise push the panel's right-edge header actions (diff
 * toggle, Edit/Preview, etc.) off-screen when the same panel is later opened
 * on a smaller laptop screen. Matches the drag cap in onDragStart below.
 */
const maxPanelWidth = () => (typeof window !== 'undefined' ? Math.round(window.innerWidth * 0.6) : Infinity)
const clampPanelWidth = (w: number, minWidth: number) => Math.max(minWidth, Math.min(w, maxPanelWidth()))

export default function DetailPanel({ title, onClose, footer, headerActions, secondaryHeaderActions, initialWidth = 380, minWidth = 300, storageKey, children, noPadding = false, headerClassName }: DetailPanelProps) {
  const [width, setWidth] = useState(() => {
    if (storageKey) {
      const v = parseInt(localStorage.getItem(storageKey) || '', 10)
      if (!isNaN(v) && v >= minWidth) return clampPanelWidth(v, minWidth)
    }
    return clampPanelWidth(initialWidth, minWidth)
  })
  const widthRef = useRef(width)
  widthRef.current = width
  const moveRef = useRef<((ev: MouseEvent) => void) | null>(null)
  const upRef = useRef<(() => void) | null>(null)
  // True while a resize-handle drag is in progress. The window `resize` listener
  // must not fight an active drag: a viewport change mid-drag would otherwise
  // clamp `width` down and, via onUp below, persist that clamped value over the
  // width the user actually dragged to.
  const draggingRef = useRef(false)

  useEffect(() => {
    return () => {
      if (moveRef.current) document.removeEventListener('mousemove', moveRef.current)
      if (upRef.current) document.removeEventListener('mouseup', upRef.current)
    }
  }, [])

  // Re-clamp on viewport shrink so a persisted width that's wider than the
  // current screen can never leave the right-edge header actions off-screen.
  // Only clamps down (never auto-grows), and is suppressed while a drag is in
  // progress (see draggingRef) so it can't clobber the in-flight drag value;
  // the preferred width stays in localStorage and is restored (re-clamped) on a
  // larger screen.
  useEffect(() => {
    const onResize = () => {
      if (draggingRef.current) return
      setWidth((w) => clampPanelWidth(w, minWidth))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [minWidth])

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    draggingRef.current = true
    const startX = e.clientX; const startW = widthRef.current
    const onMove = (ev: MouseEvent) => {
      setWidth(clampPanelWidth(startW + (startX - ev.clientX), minWidth))
    }
    const onUp = () => {
      draggingRef.current = false
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      moveRef.current = null; upRef.current = null
      // Persist the width the user dragged to (their preferred width) BEFORE
      // re-clamping the render. A resize that arrived mid-drag was suppressed,
      // so widthRef.current still holds the dragged value; this keeps the
      // preferred width in localStorage for restore (re-clamped) on a larger
      // screen rather than saving a resize-clamped value.
      if (storageKey) safeSetItem(storageKey, String(widthRef.current))
      // Re-clamp the live render once to the current viewport, in case a resize
      // arrived mid-drag, so the panel can't stay wider than the screen.
      setWidth((w) => clampPanelWidth(w, minWidth))
    }
    moveRef.current = onMove; upRef.current = onUp
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [minWidth, storageKey])

  return (
    <motion.div
      initial={{ width: 0, opacity: 0 }}
      animate={{ width: 'auto', opacity: 1 }}
      exit={{ width: 0, opacity: 0 }}
      transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
      className="shrink-0 overflow-hidden h-full"
    >
      <div className="shrink-0 border-l border-border bg-bg flex flex-col h-full overflow-hidden relative" style={{ width, minWidth }}>
        {/* Drag-to-resize splitter: pointer-only affordance (no meaningful
            keyboard gesture for a 6px handle); role="separator" is the correct
            ARIA role. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div role="separator" aria-orientation="vertical" aria-label="Resize panel" className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-20 group/drag" onMouseDown={onDragStart}>
          <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent" />
        </div>
        <div className={`flex items-center justify-between px-3 h-12 shrink-0 border-b ${headerClassName ?? 'border-border'}`}>
          <div className="flex items-center gap-2 min-w-0">
            <Btn className="p-1.5 shrink-0" onClick={onClose} aria-label="Close panel" title="Close panel"><X size={16} /></Btn>
            <span className="text-base font-semibold text-text-strong truncate">{title}</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {headerActions}
          </div>
        </div>
        {secondaryHeaderActions && (
          <div className={`flex items-center justify-between px-3 h-10 shrink-0 border-b ${headerClassName ?? 'border-border'} bg-bg-elevated/30`}>
            {secondaryHeaderActions}
          </div>
        )}
        <div className={noPadding ? "flex-1 overflow-hidden flex flex-col" : "flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4"}>
          {children}
        </div>
        {footer && (
          <div className="shrink-0 border-t border-border px-5 py-3 flex items-center justify-between">
            {footer}
          </div>
        )}
      </div>
    </motion.div>
  )
}
