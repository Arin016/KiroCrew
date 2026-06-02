import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { Handshake, Shield, ShieldPlus, ShieldCheck, ChevronDown } from 'lucide-react'

interface TrustDropdownProps {
  fullCommand: string
  baseCommand: string
  isShell: boolean
  disabled?: boolean
  className?: string
  onAction: (action: string, pattern?: string) => void
}

const POPUP_MIN_WIDTH = 220
const VIEWPORT_PAD = 8

export default function TrustDropdown({ fullCommand, baseCommand, isShell, disabled, className, onAction }: TrustDropdownProps) {
  const [open, setOpen] = useState(false)
  // 'right' = popup anchored to button's right edge, extends leftward.
  // 'left'  = popup anchored to button's left edge, extends rightward.
  // Picked dynamically based on which side has room in the viewport.
  const [align, setAlign] = useState<'left' | 'right'>('right')
  const ref = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  const handleOpen = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    setOpen(!open)
  }, [open])

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Measure on open: prefer right-anchored (popup grows leftward, common when
  // the Trust button sits in the right-hand button cluster). If that would
  // clip the popup's left edge past the viewport, fall back to left-anchored.
  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return
    const rect = triggerRef.current.getBoundingClientRect()
    const rightAnchoredLeftEdge = rect.right - POPUP_MIN_WIDTH
    setAlign(rightAnchoredLeftEdge < VIEWPORT_PAD ? 'left' : 'right')
  }, [open])

  const truncated = fullCommand.length > 30 ? fullCommand.slice(0, 30) + '…' : fullCommand
  const itemClass = 'flex items-center gap-2 w-full px-3 py-1.5 text-[12px] text-text cursor-pointer border-none bg-transparent text-left font-body hover:bg-bg-hover transition-colors'

  return (
    <div className="relative" ref={ref}>
      <button ref={triggerRef} disabled={disabled} className={className} onClick={handleOpen}>
        <Handshake size={12} className="shrink-0" />Trust<ChevronDown size={10} className="shrink-0 opacity-70" />
      </button>
      {open && (
        <div className={`absolute bottom-full ${align === 'right' ? 'right-0' : 'left-0'} mb-1 bg-card border border-border rounded-lg shadow-lg py-1 min-w-[220px] z-[9999]`}>
          <button className={itemClass}
            onClick={e => { e.stopPropagation(); setOpen(false); onAction('trust_command', fullCommand) }}>
            <Shield size={12} className="shrink-0 text-accent" />
            <span className="truncate">Trust &ldquo;<span className="font-mono">{truncated}</span>&rdquo;</span>
          </button>
          {isShell && (
            <button className={itemClass}
              onClick={e => { e.stopPropagation(); setOpen(false); onAction('trust_base', baseCommand.split(',').map(b => b.trim() + ' *').join(',')) }}>
              <ShieldPlus size={12} className="shrink-0 text-ok" />
              <span className="truncate">Trust all &ldquo;<span className="font-mono">{baseCommand.split(',').join(', ')}</span>&rdquo; commands</span>
            </button>
          )}
          <button className={itemClass}
            onClick={e => { e.stopPropagation(); setOpen(false); onAction('trust') }}>
            <ShieldCheck size={12} className="shrink-0 text-warn" />
            <span>Trust all tools</span>
          </button>
        </div>
      )}
    </div>
  )
}
