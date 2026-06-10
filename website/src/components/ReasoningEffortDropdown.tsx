import { useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { effortLabel } from './ChatInput'
import { EFFORT_LEVELS } from '../lib/effort'
import { api } from '../api/client'

// Cold-start fallback before /api/effort-levels resolves (or on fetch failure).
// Sourced from the shared effort vocabulary (incl. 'xhigh') so the dropdown
// never silently drops a level beta offered statically.
const FALLBACK_LEVELS: string[] = [...EFFORT_LEVELS]

function normalizeLevels(data: string[]): string[] {
  return ['', ...data.filter(l => l !== '' && l !== 'default')]
}

interface Props {
  slot: string
  currentEffort: string
  onClose: () => void
  embedded?: boolean
}

export default function ReasoningEffortDropdown({ slot, currentEffort, onClose, embedded }: Props) {
  // Keyed by slot: each slot's levels come from its own live ACP model, so the
  // cache must not bleed across slots. staleTime 0 + refetchOnMount means every
  // time the popover (re)mounts we fetch the slot's current levels, so a model
  // switch is reflected immediately rather than served stale from cache.
  const { data: levels = FALLBACK_LEVELS } = useQuery({
    queryKey: ['effort-levels', slot],
    queryFn: () => api.effortLevels(slot).then(data =>
      Array.isArray(data) && data.length > 0
        ? normalizeLevels(data)
        : FALLBACK_LEVELS
    ),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  // The slot's persisted effort may be a level absent from the fetched/fallback
  // list (e.g. a value valid for persistence but not in this model's reported
  // set, or before the fetch resolves). Always include it so the active level
  // is shown with a check and remains reselectable.
  const shownLevels = levels.includes(currentEffort)
    ? levels
    : [...levels, currentEffort]

  return (
    <div className={embedded ? '' : 'rounded-lg bg-bg-elevated border border-border py-1 w-[200px]'}>
      {shownLevels.map(level => {
        const active = currentEffort === level
        return (
          <button
            key={level || 'default'}
            title={level ? `Set reasoning effort to ${level}` : 'Use the model default effort (Opus/Sonnet only)'}
            onClick={async () => {
              onClose()
              try { await api.chatSlotReasoningEffort(slot, level) } catch (err) { console.warn('Failed to set reasoning effort', err) }
            }}
            className={`flex items-center gap-2 w-full px-3 py-2 text-[13px] font-medium cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover ${active ? 'text-accent' : 'text-text'}`}
          >
            <span>{effortLabel(level || '')}</span>
            {active && <Check size={13} className="ml-auto text-accent" />}
          </button>
        )
      })}
    </div>
  )
}
