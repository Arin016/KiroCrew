import { useEffect, useRef } from 'react'
import { X, ChevronUp, ChevronDown, CaseSensitive } from 'lucide-react'
import type { SearchMatch } from '../hooks/useMessageSearch'
import { platformShortcut } from '../utils/platform'

interface SearchBarProps {
  term: string
  setTerm: (t: string) => void
  matches: SearchMatch[]
  currentIdx: number
  next: () => void
  prev: () => void
  close: () => void
  caseSensitive: boolean
  toggleCaseSensitive: () => void
}

export default function SearchBar({ term, setTerm, matches, currentIdx, next, prev, close, caseSensitive, toggleCaseSensitive }: SearchBarProps) {
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) prev()
      else next()
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
    }
  }

  return (
    <div className="absolute top-14 right-4 z-20 flex items-center gap-1.5 bg-bg-elevated border border-border rounded-lg shadow-md px-2.5 py-1.5 text-[13px]">
      <input
        ref={inputRef}
        type="text"
        value={term}
        onChange={e => setTerm(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Find in chat…"
        className="bg-transparent border-none outline-none text-text placeholder:text-muted w-[180px] text-[13px]"
      />
      <button
        onClick={toggleCaseSensitive}
        className={`p-0.5 rounded cursor-pointer border-none transition-colors ${caseSensitive ? 'bg-accent/20 text-accent' : 'bg-transparent text-muted hover:text-text'}`}
        title="Case sensitive"
        aria-label="Case sensitive"
      >
        <CaseSensitive size={15} />
      </button>
      {term && (
        <span className="text-muted text-[12px] whitespace-nowrap tabular-nums">
          {matches.length > 0 ? `${currentIdx + 1} of ${matches.length} results` : 'No results'}
        </span>
      )}
      <button onClick={prev} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={`Previous (${platformShortcut('Shift+Enter')})`} aria-label="Previous match">
        <ChevronUp size={15} />
      </button>
      <button onClick={next} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={`Next (${platformShortcut('Enter')})`} aria-label="Next match">
        <ChevronDown size={15} />
      </button>
      <button onClick={close} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title="Close (Esc)" aria-label="Close (Esc)">
        <X size={15} />
      </button>
    </div>
  )
}
