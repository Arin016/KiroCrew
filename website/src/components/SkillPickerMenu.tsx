import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'

// Mesh-588 — $skill inline trigger autocomplete.
// Mirrors FilePickerMenu but lists skills (from /api/skills, all sources:
// kiroclaw + workspace + AIM). Selecting one inserts a `$leaf` token; the
// backend SkillsLoader.resolve_dollar_skills then expands it (allowlist match
// on the leaf segment — no path is constructed from user input, per ARCC BSC1).

interface SkillItem {
  key: string          // full key, e.g. "WorkforceEmploymentKnowledgeBase/oncall-handover"
  name: string
  description: string
  source?: string      // kiroclaw | aim | kiro-user | kiro-workspace
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  // Receives the leaf token to insert (e.g. "oncall-handover") plus the full key.
  onSelect: (info: { leaf: string; key: string }) => void
  onClose: () => void
}

// Last path segment of a skill key — this is what `$token` matches against.
function leafOf(key: string): string {
  const i = key.lastIndexOf('/')
  return i === -1 ? key : key.slice(i + 1)
}

export default function SkillPickerMenu({ query, anchorRef, open, onSelect, onClose }: Props) {
  const [results, setResults] = useState<SkillItem[]>([])
  const resultsRef = useRef<SkillItem[]>([])

  // Shared skills cache. Keyed ['skills'] so it dedupes with SkillsTab's query
  // and any focus-prefetch in ChatInput — the menu's first open is warm if the
  // list was already fetched. staleTime is long because skills change rarely
  // (added via setup/AIM sync). `enabled: open` keeps the menu lazy: no fetch
  // until it's actually shown (the focus-prefetch warms the cache separately).
  const { data, isLoading } = useQuery<SkillItem[]>({
    queryKey: ['skills'],
    queryFn: () => api.skills(),
    enabled: open,
    staleTime: 5 * 60 * 1000, // 5 min
  })
  const loading = isLoading && open

  // Choose handler reads from resultsRef (current at keypress time).
  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const s = r[idx >= r.length ? 0 : idx]
    if (s) onSelect({ leaf: leafOf(s.key || s.name), key: s.key || s.name })
  }, [onSelect])

  // Shared Arrow/Enter/Tab/Escape + scroll-into-view (see useListKeyboardNav).
  const { selected, setSelected, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
  })

  // Filter by leaf-name substring (case-insensitive). Empty query lists all,
  // capped for menu height. Dedupe by leaf so the same $token isn't ambiguous.
  useEffect(() => {
    if (!open) return
    const list = Array.isArray(data) ? data : []
    const q = query.toLowerCase()
    const seen = new Set<string>()
    const filtered: SkillItem[] = []
    for (const s of list) {
      const leaf = leafOf(s.key || s.name).toLowerCase()
      if (q && !leaf.includes(q)) continue
      if (seen.has(leaf)) continue
      seen.add(leaf)
      filtered.push(s)
      if (filtered.length >= 50) break
    }
    setResults(filtered); resultsRef.current = filtered
    setSelected(0)
  }, [query, open, data, setSelected])

  if (!open || !anchorRef.current) return null

  const rect = anchorRef.current.getBoundingClientRect()
  const menuH = Math.min((results.length || 1) * 48 + 8, 320)
  const above = rect.top - menuH - 4
  const top = above > 0 ? above : rect.bottom + 4

  const empty = loading
    ? <div className="px-3 py-3 text-[12px] text-muted">Loading skills…</div>
    : <div className="px-3 py-3 text-[12px] text-muted">No matching skills</div>

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left: rect.left, width: Math.min(rect.width, 460), maxHeight: 320 }}
    >
      {results.length === 0 ? empty : results.map((s, i) => {
        const leaf = leafOf(s.key || s.name)
        return (
          <div
            role="option"
            aria-selected={i === selected}
            tabIndex={-1}
            key={s.key || s.name}
            ref={el => { itemRefs.current[i] = el }}
            className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
            title={s.key}
            onMouseEnter={() => setSelected(i)}
            onMouseDown={e => { e.preventDefault(); onSelect({ leaf, key: s.key || s.name }) }}
          >
            <Sparkles size={14} className="shrink-0 lucide-inline" />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-mono font-semibold truncate">${leaf}</div>
              <div className="text-[11px] text-muted truncate">{s.description || s.key}</div>
            </div>
            {s.source && s.source !== 'kiroclaw' && (
              <span className="text-[10px] text-muted shrink-0 whitespace-nowrap uppercase tracking-wide">{s.source}</span>
            )}
          </div>
        )
      })}
    </div>,
    document.body
  )
}
