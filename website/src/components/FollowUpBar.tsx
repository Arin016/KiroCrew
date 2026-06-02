import { memo, useRef, useState, useEffect, useCallback } from 'react'

export type FollowUpLayout = 'multiline' | 'scroll'

interface FollowUpBarProps {
  options: string[]
  picked: Set<string>
  onSelect: (option: string, event: React.MouseEvent) => void
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

function chipTitle(isPicked: boolean, quickSend: boolean | undefined, picked: Set<string>) {
  if (isPicked) return 'Click to remove from input'
  if (quickSend && picked.size === 0) return 'Click to send instantly, Shift+Click to select multiple'
  if (quickSend) return 'Click to add to selection'
  return 'Click to add to input (editable before sending)'
}

function ScrollLayout({ options, picked, onSelect, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
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
            <button
              key={o}
              onClick={(e) => onSelect(o, e)}
              className={chipClassName(isPicked, 'shrink-0')}
              title={chipTitle(isPicked, quickSend, picked)}
            >
              {o}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
  return (
    <div className="flex gap-1.5 flex-wrap pt-1 items-center">
      {options.map(o => {
        const isPicked = picked.has(o)
        return (
          <button
            key={o}
            onClick={(e) => onSelect(o, e)}
            className={chipClassName(isPicked)}
            title={chipTitle(isPicked, quickSend, picked)}
          >
            {o}
          </button>
        )
      })}
    </div>
  )
}

function FollowUpBar({ options, picked, onSelect, quickSend, layout = 'multiline' }: FollowUpBarProps) {
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} quickSend={quickSend} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} quickSend={quickSend} />
}

export default memo(FollowUpBar)
