import { useRef, useEffect } from 'react'
import { SourceBadge } from './SourceBadge'
import { Star, Check } from 'lucide-react'

interface AgentItem {
  name: string
  source: string
  description?: string
}

/** Shared agent list used in dropdown portals across WelcomeView, ChatPage, and AgentsPage */
export default function AgentDropdownList({ agents, activeAgent, defaultAgent, onSelect }: {
  agents: AgentItem[]
  activeAgent: string
  defaultAgent: string
  onSelect: (name: string) => void
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])
  return (
    <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-0.5">
      {agents.map(a => {
        const active = activeAgent === a.name
        const isDefault = a.name === defaultAgent
        return (
          <button key={a.name} ref={active ? activeRef : undefined} role="option" aria-selected={active} tabIndex={-1} className={`w-full text-left px-2.5 py-2 flex flex-col gap-0.5 rounded-md cursor-pointer transition-all ${active ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`} onClick={() => onSelect(a.name)}>
            <div className="flex items-center gap-2">
              <span className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>{a.name}</span>
              <SourceBadge source={a.source}>{a.source}{isDefault ? <> <Star className="lucide-inline" /></> : ''}</SourceBadge>
              {active && <span className="text-accent text-[12px]"><Check className="lucide-inline" /></span>}
            </div>
            {a.description && <span className="text-[12px] text-muted leading-tight line-clamp-2" title={a.description}>{a.description}</span>}
          </button>
        )
      })}
      {agents.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">No matches</div>}
    </div>
  )
}
