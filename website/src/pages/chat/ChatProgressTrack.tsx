import { useState, useRef, useEffect, useCallback } from 'react'
import type { ChatSection } from '../../hooks/useChatNavigation'

interface ChatProgressTrackProps {
  sections: ChatSection[]
  /** Index (within sections) of the section closest to the current scroll position */
  currentIdx: number
  onScrollToSection: (displayIdx: number) => void
}

/**
 * Minimal vertical progress track shown on the right edge of the chat.
 *
 * Both collapsed (lines) and expanded (labels) are always mounted.
 * Hover/focus toggles opacity/pointer-events — no mount delay, instant scroll-to-center.
 * Keyboard accessible: focus opens, ArrowUp/Down navigates, Enter selects, Escape closes.
 */
export default function ChatProgressTrack({ sections, currentIdx, onScrollToSection }: ChatProgressTrackProps) {
  const [open, setOpen] = useState(false)
  const [focusIdx, setFocusIdx] = useState(-1)
  const panelRef = useRef<HTMLDivElement>(null)
  const currentRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLDivElement>(null)

  // Scroll to center the current item whenever panel opens or currentIdx changes while open
  useEffect(() => {
    if (!open) return
    const panel = panelRef.current
    const item = currentRef.current
    if (!panel || !item) return
    panel.scrollTop = item.offsetTop - panel.clientHeight / 2 + item.clientHeight / 2
  }, [open, currentIdx])

  // Focus management: only set focusIdx on open transition (not on scroll-driven currentIdx changes)
  const prevOpenRef = useRef(false)
  useEffect(() => {
    if (open && !prevOpenRef.current) {
      panelRef.current?.focus()
    }
    if (!open) {
      setFocusIdx(-1)
    }
    prevOpenRef.current = open
  }, [open])

  const closingRef = useRef(false)
  const openPanel = useCallback(() => {
    if (!closingRef.current) {
      setFocusIdx(currentIdx)
      setOpen(true)
    }
  }, [currentIdx])
  const closePanel = useCallback(() => {
    closingRef.current = true
    setOpen(false)
    triggerRef.current?.focus()
    queueMicrotask(() => { closingRef.current = false })
  }, [])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        openPanel()
      }
      return
    }

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        setFocusIdx(i => Math.min(i + 1, sections.length - 1))
        break
      case 'ArrowUp':
        e.preventDefault()
        setFocusIdx(i => Math.max(i - 1, 0))
        break
      case 'Home':
        e.preventDefault()
        setFocusIdx(0)
        break
      case 'End':
        e.preventDefault()
        setFocusIdx(sections.length - 1)
        break
      case 'Enter':
      case ' ':
        e.preventDefault()
        if (focusIdx >= 0 && focusIdx < sections.length) {
          onScrollToSection(sections[focusIdx].displayIdx)
          closePanel()
        }
        break
      case 'Escape':
        e.preventDefault()
        closePanel()
        break
      case 'Tab':
        closingRef.current = true
        setOpen(false)
        queueMicrotask(() => { closingRef.current = false })
        break
    }
  }, [open, focusIdx, sections, onScrollToSection, openPanel, closePanel])

  // Keep focused item scrolled into view
  useEffect(() => {
    if (!open || focusIdx < 0) return
    const panel = panelRef.current
    const items = panel?.querySelectorAll('[role="option"]')
    const item = items?.[focusIdx] as HTMLElement | undefined
    if (item) item.scrollIntoView({ block: 'nearest' })
  }, [open, focusIdx])

  if (sections.length < 2) return null

  return (
    <div
      className="absolute right-2 top-1/2 -translate-y-1/2 z-[5] flex flex-col items-end"
      role="navigation"
      aria-label="Chat progress"
      onMouseEnter={openPanel}
      onMouseLeave={() => {
        // Don't close if keyboard focus is inside (user opened via keyboard)
        if (!triggerRef.current?.contains(document.activeElement) &&
            !panelRef.current?.contains(document.activeElement)) {
          setOpen(false)
        }
      }}
      onKeyDown={handleKeyDown}
    >
      {/* Collapsed: thin lines — also serves as keyboard trigger */}
      <div
        ref={triggerRef}
        tabIndex={0}
        role="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Chat progress — navigate conversation sections"
        className="flex flex-col items-end gap-[3px] py-2 cursor-pointer transition-opacity duration-150 outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
        style={{ opacity: open ? 0 : 1, pointerEvents: open ? 'none' : 'auto' }}
        onFocus={openPanel}
      >
        {sections.map((_, i) => (
          <div
            key={i}
            className="rounded-sm transition-all duration-250"
            style={{
              width: i === currentIdx ? 24 : 20,
              height: i === currentIdx ? 3 : 2.5,
              background: i === currentIdx ? 'var(--text)' : 'var(--border)',
            }}
          />
        ))}
      </div>

      {/* Expanded: labeled listbox — always mounted, hidden when not open */}
      <div
        ref={panelRef}
        role="listbox"
        tabIndex={-1}
        aria-label="Conversation sections"
        aria-activedescendant={focusIdx >= 0 ? `progress-item-${focusIdx}` : undefined}
        className="absolute right-0 top-1/2 -translate-y-1/2 rounded-lg border border-border shadow-lg py-1 px-1.5 w-[320px] overflow-y-auto transition-all duration-150 outline-none"
        style={{
          background: 'var(--card)',
          maxHeight: '80vh',
          opacity: open ? 1 : 0,
          pointerEvents: open ? 'auto' : 'none',
          transform: `translateY(-50%) ${open ? 'translateX(0) scale(1)' : 'translateX(8px) scale(0.95)'}`,
        }}
      >
        <div className="flex flex-col">
          {sections.map((section, i) => (
            <div
              key={i}
              id={`progress-item-${i}`}
              ref={i === currentIdx ? currentRef : undefined}
              role="option"
              tabIndex={-1}
              aria-selected={i === currentIdx}
              className={`text-left text-[13px] leading-snug px-2 py-1 rounded transition-colors cursor-pointer truncate hover:bg-bg-hover ${
                i === currentIdx ? 'font-semibold' : ''
              } ${i === focusIdx ? 'ring-1 ring-accent' : ''}`}
              style={{
                color: i === currentIdx ? 'var(--text)' : 'var(--muted)',
              }}
              onClick={() => { onScrollToSection(section.displayIdx); closePanel() }}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onScrollToSection(section.displayIdx); closePanel() } }}
              title={section.label}
            >
              {section.label}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
