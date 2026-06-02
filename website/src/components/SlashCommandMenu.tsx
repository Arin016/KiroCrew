import { useState, useEffect, useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { api } from '../api/client'

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
  const [selected, setSelected] = useState(0)
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
  const filtered = useMemo(
    () => (visible ? commands.filter(c => c.name.slice(1).startsWith(filter)) : []),
    [visible, filter, commands]
  )

  // Step 3 fix: reset selection when filter changes OR menu reopens
  useEffect(() => { setSelected(0) }, [filter, visible])

  const onKey = useCallback((e: KeyboardEvent) => {
    if (!visible || filtered.length === 0) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setSelected(i => (i + 1) % filtered.length) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSelected(i => (i - 1 + filtered.length) % filtered.length) }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); onSelect(filtered[selected >= filtered.length ? 0 : selected].name + ' ') }
    else if (e.key === 'Escape') { e.preventDefault(); onClose() }
  }, [visible, filtered, selected, onSelect, onClose])

  useEffect(() => {
    if (!visible) return
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [visible, onKey])

  if (!visible || filtered.length === 0 || !anchorRef.current) return null

  const rect = anchorRef.current.getBoundingClientRect()
  const menuH = Math.min(filtered.length * 40 + 8, 320)
  const above = rect.top - menuH - 4
  const top = above > 0 ? above : rect.bottom + 4

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      style={{ top, left: rect.left, width: Math.min(rect.width, 380), maxHeight: 320 }}
    >
      {filtered.map((cmd, i) => (
        <button
          key={cmd.name}
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
