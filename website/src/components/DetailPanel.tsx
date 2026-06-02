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

export default function DetailPanel({ title, onClose, footer, headerActions, secondaryHeaderActions, initialWidth = 380, minWidth = 300, storageKey, children, noPadding = false, headerClassName }: DetailPanelProps) {
  const [width, setWidth] = useState(() => {
    if (storageKey) {
      const v = parseInt(localStorage.getItem(storageKey) || '', 10)
      if (!isNaN(v) && v >= minWidth) return v
    }
    return initialWidth
  })
  const widthRef = useRef(width)
  widthRef.current = width
  const moveRef = useRef<((ev: MouseEvent) => void) | null>(null)
  const upRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    return () => {
      if (moveRef.current) document.removeEventListener('mousemove', moveRef.current)
      if (upRef.current) document.removeEventListener('mouseup', upRef.current)
    }
  }, [])

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX; const startW = widthRef.current
    const onMove = (ev: MouseEvent) => {
      setWidth(Math.max(minWidth, Math.min(startW + (startX - ev.clientX), window.innerWidth * 0.6)))
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      moveRef.current = null; upRef.current = null
      if (storageKey) localStorage.setItem(storageKey, String(widthRef.current))
    }
    moveRef.current = onMove; upRef.current = onUp
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [minWidth])

  return (
    <motion.div
      initial={{ width: 0, opacity: 0 }}
      animate={{ width: 'auto', opacity: 1 }}
      exit={{ width: 0, opacity: 0 }}
      transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
      className="shrink-0 overflow-hidden h-full"
    >
      <div className="shrink-0 border-l border-border bg-bg flex flex-col h-full overflow-hidden relative" style={{ width, minWidth }}>
        <div className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-20 group/drag" onMouseDown={onDragStart}>
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
          <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-between">
            {footer}
          </div>
        )}
      </div>
    </motion.div>
  )
}
