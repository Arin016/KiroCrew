import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'

interface SlashCommand {
  name: string
  description: string
}

const FALLBACK_COMMANDS: SlashCommand[] = [
  { name: '/clear', description: 'Clear conversation history' },
  { name: '/compact', description: 'Compact conversation to free context' },
  { name: '/kb', description: 'Search knowledge library' },
  { name: '/mcp', description: 'Show configured MCP servers' },
  { name: '/model', description: 'Show current model' },
  { name: '/prompts', description: 'List or invoke saved prompts & agent SOPs' },
  { name: '/side', description: 'Open side conversation panel' },
  { name: '/tools', description: 'Show available tools' },
  { name: '/usage', description: 'Show billing and usage information' },
]

interface Props {
  input: string
  anchorRef: React.RefObject<HTMLElement | null>
  onSelect: (command: string) => void
  onClose: () => void
  open?: boolean
}

const FRONTEND_COMMANDS: SlashCommand[] = [
  { name: '/kb', description: 'Search knowledge library' },
]

export default function SlashCommandMenu({ input, anchorRef, onSelect, onClose, open = true }: Props) {
  const { data: apiCommands = FALLBACK_COMMANDS } = useQuery<SlashCommand[]>({
    queryKey: ['slash-commands'],
    queryFn: () => api.slashCommands(),
    enabled: typeof api.slashCommands === 'function',
  })
  const commands = useMemo(() => {
    const names = new Set(apiCommands.map(c => c.name))
    return [...apiCommands, ...FRONTEND_COMMANDS.filter(c => !names.has(c.name))].sort((a, b) => a.name.localeCompare(b.name))
  }, [apiCommands])

  const match = input.match(/^\/([a-z]*)$/)
  const visible = open && !!match
  const filter = match?.[1] ?? ''

  // Displayed order (bottom-up when the menu opens above); resultsRef mirrors it
  // so the keyboard-nav choose() indexes the same list the user sees.
  const [displayed, setDisplayed] = useState<SlashCommand[]>([])
  const resultsRef = useRef<SlashCommand[]>([])

  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const c = r[idx >= r.length ? 0 : idx]
    if (c) onSelect(c.name + ' ')
  }, [onSelect])

  // Consolidated onto the SAME nav hook as the $skill / @file pickers — this is
  // what gives the slash menu arrow-scroll and consistent Enter/Tab/Escape (it
  // previously rolled its own selection state with no scroll-into-view).
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open: visible,
    count: displayed.length,
    onChoose: choose,
    onClose,
  })

  // Order + initial selection: bottom-up when the menu opens above the input
  // (shared helper — identical to the other pickers). Filter is computed INSIDE
  // the effect and keyed on primitives (visible/filter/commands) so unrelated
  // re-renders (e.g. arrow-key selection changes) don't reset the selection.
  useEffect(() => {
    if (!visible) { setDisplayed([]); resultsRef.current = []; return }
    const f = commands.filter(c => c.name.slice(1).startsWith(filter))
    const above = anchorRef.current ? menuGeometry(anchorRef.current, f.length, 40).above : false
    const { ordered, initialIndex } = bottomUpOrder(f, above)
    setDisplayed(ordered); resultsRef.current = ordered
    setSelected(initialIndex)
  }, [visible, filter, commands, anchorRef, setSelected])

  // Scroll the selected row into view once it renders (open + filter change),
  // matching the $skill / @file pickers.
  useEffect(() => {
    if (!visible) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [displayed, visible, itemRefs, selectedRef])

  if (!visible || displayed.length === 0 || !anchorRef.current) return null

  const { top, left, width, maxHeight } = menuGeometry(anchorRef.current, displayed.length, 40)

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left, width: Math.min(width, 380), maxHeight }}
    >
      {displayed.map((cmd, i) => (
        <button
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          key={cmd.name}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          onMouseEnter={() => setSelected(i)}
          onMouseDown={e => { e.preventDefault(); onSelect(cmd.name + ' ') }}
        >
          <span className="text-[13px] font-mono font-semibold text-accent shrink-0">{cmd.name}</span>
          <span className="text-[12px] truncate">{cmd.description}</span>
        </button>
      ))}
    </div>,
    document.body
  )
}
