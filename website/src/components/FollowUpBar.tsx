import { memo, useRef, useState, useEffect, useCallback } from 'react'

export type FollowUpLayout = 'multiline' | 'scroll'

interface FollowUpBarProps {
  options: string[]
  picked: Set<string>
  onSelect: (option: string, event: React.MouseEvent) => void
  /** Double-click sends with this option's text directly (bypasses setInput race). */
  onSend?: (text?: string) => void
  quickSend?: boolean
  /** 'multiline' (default) wraps onto multiple rows; 'scroll' is the original single-line horizontally-scrollable view. */
  layout?: FollowUpLayout
}

function chipClassName(isPicked: boolean, extra: string = '') {
  return `${extra} px-3 py-1.5 rounded-lg text-[13px] cursor-pointer transition-all border ${
    isPicked
      ? 'border-solid border-accent text-accent bg-accent-subtle'
      : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg-elevated'
  }`
}

function chipTitle(isPicked: boolean, quickSend: boolean | undefined, picked: Set<string>, hasOnSend: boolean) {
  if (isPicked) return hasOnSend ? 'Click to remove from input (double-click to send)' : 'Click to remove from input'
  if (quickSend && picked.size === 0) return 'Click to send instantly, Shift+Click to select multiple'
  if (quickSend) return 'Click to add to selection'
  return hasOnSend
    ? 'Click to add to input (double-click to select and send)'
    : 'Click to add to input (editable before sending)'
}

interface ChipProps {
  option: string
  isPicked: boolean
  picked: Set<string>
  quickSend: boolean | undefined
  onSelect: (option: string, event: React.MouseEvent) => void
  onSend?: (text?: string) => void
  className: string
}

/**
 * Single follow-up chip. Handles click/double-click semantics:
 * - When `onSend` is not provided, falls through to direct `onSelect` (legacy callers).
 * - When `quickSend` is active in instant-send state (not picked, no prior picks), falls through
 *   to direct `onSelect` to preserve the no-lag instant-send UX.
 * - Otherwise: single click is debounced 220ms (timer cancelled by double-click) so the user can
 *   double-click to fire `onSend(text)` directly without going through setInput (which would
 *   race with the React state update and cause send() to read a stale inputRef.current).
 */
function Chip({ option, isPicked, picked, quickSend, onSelect, onSend, className }: ChipProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const useDebouncedClick = !!onSend && !(quickSend && !isPicked && picked.size === 0)
  const title = chipTitle(isPicked, quickSend, picked, !!onSend)

  if (!useDebouncedClick) {
    return (
      <button
        onClick={(e) => onSelect(option, e)}
        className={className}
        title={title}
      >
        {option}
      </button>
    )
  }

  const handleClick = (e: React.MouseEvent) => {
    // detail >= 2 means this click is part of a double-click sequence — let
    // onDoubleClick handle it so we don't start a timer that races with it.
    if (e.detail >= 2) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Capture the parts of the event that survive the timer (React pools events).
    const shiftKey = e.shiftKey
    const synth = { shiftKey, detail: 1 } as unknown as React.MouseEvent
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      onSelect(option, synth)
    }, 220)
  }

  const handleDoubleClick = () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Pass option text directly to send() so it doesn't race with setInput.
    // If already picked, send() will use the current input (which already contains o).
    onSend?.(isPicked ? undefined : option)
  }

  return (
    <button
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      className={className}
      title={title}
    >
      {option}
    </button>
  )
}

function ScrollLayout({ options, picked, onSelect, onSend, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [canScrollL, setCanScrollL] = useState(false)
  const [canScrollR, setCanScrollR] = useState(false)

  const updateScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setCanScrollL(el.scrollLeft > 2)
    setCanScrollR(el.scrollLeft + el.clientWidth < el.scrollWidth - 2)
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    updateScroll()
    el.addEventListener('scroll', updateScroll, { passive: true })
    const ro = new ResizeObserver(updateScroll)
    ro.observe(el)
    return () => { el.removeEventListener('scroll', updateScroll); ro.disconnect() }
  }, [updateScroll, options])

  return (
    <div className="relative pt-1">
      {canScrollL && <div className="absolute left-0 top-0 bottom-0 w-6 z-10 pointer-events-none bg-gradient-to-r from-bg to-transparent" />}
      {canScrollR && <div className="absolute right-0 top-0 bottom-0 w-6 z-10 pointer-events-none bg-gradient-to-l from-bg to-transparent" />}
      <div ref={scrollRef} className="flex gap-1.5 overflow-x-auto items-center" style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
        {options.map(o => {
          const isPicked = picked.has(o)
          return (
            <Chip
              key={o}
              option={o}
              isPicked={isPicked}
              picked={picked}
              quickSend={quickSend}
              onSelect={onSelect}
              onSend={onSend}
              className={chipClassName(isPicked, 'shrink-0')}
            />
          )
        })}
      </div>
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, onSend, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
  return (
    <div className="flex gap-1.5 flex-wrap pt-1 items-center">
      {options.map(o => {
        const isPicked = picked.has(o)
        return (
          <Chip
            key={o}
            option={o}
            isPicked={isPicked}
            picked={picked}
            quickSend={quickSend}
            onSelect={onSelect}
            onSend={onSend}
            className={chipClassName(isPicked)}
          />
        )
      })}
    </div>
  )
}

function FollowUpBar({ options, picked, onSelect, onSend, quickSend, layout = 'multiline' }: FollowUpBarProps) {
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} />
}

export default memo(FollowUpBar)
