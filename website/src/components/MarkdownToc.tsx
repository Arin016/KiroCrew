import { memo, useState, useCallback, useEffect, useRef } from 'react'
import { List, X } from 'lucide-react'

export interface TocEntry { level: number; text: string; slug: string; index: number }

/** Extract TOC entries from rendered DOM headings — guarantees consistency with what the user sees */
export function extractHeadingsFromDOM(container: HTMLElement | null): TocEntry[] {
  if (!container) return []
  const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6')
  const entries: TocEntry[] = []
  headings.forEach((h, i) => {
    const text = h.textContent?.trim() || ''
    if (!text) return
    const level = parseInt(h.tagName[1], 10)
    const slug = (h as HTMLElement).id || ''
    entries.push({ level, text, slug, index: i })
  })
  return entries
}

function scrollToHeading(container: HTMLElement | null, entry: TocEntry) {
  if (!container) return
  const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6')
  const el = headings[entry.index]
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** Toggle button — placed in the header toolbar */
export const TocToggle = memo(function TocToggle({ visible, hasHeadings, onClick }: { visible: boolean; hasHeadings: boolean; onClick: () => void }) {
  if (!hasHeadings) return null
  return (
    <button
      className={`p-1.5 rounded-md border cursor-pointer transition-all ${visible ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
      onClick={onClick}
      title="Table of contents"
      aria-label="Table of contents"
    >
      <List size={14} />
    </button>
  )
})

/** The TOC drawer panel — renders as a fixed-width right sidebar */
export default memo(function MarkdownToc({ containerRef, onClose }: { containerRef: React.RefObject<HTMLElement | null>; onClose: () => void }) {
  const [entries, setEntries] = useState<TocEntry[]>([])
  const [active, setActive] = useState<number>(-1)
  const navRef = useRef<HTMLElement>(null)

  // Extract headings from DOM after render
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    // Use MutationObserver to re-extract when content changes
    const extract = () => {
      const next = extractHeadingsFromDOM(container)
      setEntries(prev => {
        if (prev.length === next.length && prev.every((e, i) => e.text === next[i].text && e.level === next[i].level && e.index === next[i].index)) return prev
        return next
      })
    }
    extract()
    const mo = new MutationObserver(extract)
    mo.observe(container, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [containerRef])

  const handleClick = useCallback((entry: TocEntry, i: number) => {
    scrollToHeading(containerRef.current, entry)
    setActive(i)
  }, [containerRef])

  // IntersectionObserver to track active heading on scroll
  useEffect(() => {
    const container = containerRef.current
    if (!container || entries.length === 0) return
    const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6')
    if (headings.length === 0) return

    const observer = new IntersectionObserver((obs) => {
      for (const e of obs) {
        if (e.isIntersecting) {
          const idx = Array.from(headings).indexOf(e.target as Element)
          const entryIdx = entries.findIndex(en => en.index === idx)
          if (entryIdx >= 0) setActive(entryIdx)
          break
        }
      }
    }, { root: container, rootMargin: '0px 0px -80% 0px', threshold: 0 })

    headings.forEach(h => observer.observe(h))
    return () => observer.disconnect()
  }, [containerRef, entries])

  if (entries.length === 0) return null
  const minLevel = Math.min(...entries.map(e => e.level))

  return (
    <div className="h-full flex flex-col border-l border-border bg-bg w-[220px] shrink-0">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-[12px] font-medium text-muted">Contents</span>
        <button className="p-0.5 rounded text-muted hover:text-text cursor-pointer bg-transparent border-none" onClick={onClose} aria-label="Close table of contents"><X size={13} /></button>
      </div>
      <nav ref={navRef} className="flex-1 overflow-y-auto py-2 px-1">
        {entries.map((entry, i) => (
          <button
            key={`${entry.index}-${entry.slug}`}
            className={`w-full text-left text-[12px] leading-tight px-2 py-1.5 rounded cursor-pointer bg-transparent border-none transition-colors truncate ${active === i ? 'bg-accent-subtle text-accent' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
            style={{ paddingLeft: `${(entry.level - minLevel) * 12 + 8}px` }}
            onClick={() => handleClick(entry, i)}
            title={entry.text}
          >
            {entry.text}
          </button>
        ))}
      </nav>
    </div>
  )
})
