import { useState, useRef, useEffect, useMemo, memo } from 'react'
import { Bot, ChevronDown } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { sseSubagentDone } from '../../store/chatSlice'
import { api } from '../../api/client'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { SubagentActivity } from '../../types'

const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}

/** Compact subagent activity indicator above the chat input. */
const SubagentProgressBar = memo(function SubagentProgressBar({ slot }: { slot: string | null }) {
  // Use chatSlice.subagents — populated by subagent_spawn/tool/done WS events
  // (dashboardSlice.subagentRunning only updates on subagent_status which fires at completion)
  const dispatch = useAppDispatch()
  const subagents = useAppSelector(s => slot === s.chat.activeSlot ? s.chat.subagents : s.chat.slotActivity[slot ?? '']?.subagents ?? EMPTY_SUBAGENTS)
  const activeList = useMemo(() => Object.values(subagents).filter(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending'), [subagents])
  const running = activeList.length
  const activeListRef = useRef(activeList)
  activeListRef.current = activeList
  const hasActive = running > 0
  const [expanded, setExpanded] = useState(false)
  const [, setTick] = useState(0)
  // 1Hz tick to update elapsed timers + 30s reconciliation to clear phantom agents
  useEffect(() => {
    if (!hasActive || !slot) return
    let cancelled = false
    const t = setInterval(() => setTick(n => 1 - n), 1000)
    const reconcile = setInterval(() => {
      api.spawnList().then((d: any) => {
        if (cancelled) return
        const backendIds = new Set((d.agents || []).filter((a: any) => !a.done && a.parent === `dashboard:${slot}`).map((a: any) => a.id))
        activeListRef.current.forEach(a => {
          if (!backendIds.has(a.id)) dispatch(sseSubagentDone({ slot, id: a.id, elapsed: Math.round((Date.now() - a.startedAt) / 1000), error: 'reconciliation: agent no longer tracked by backend' }))
        })
      }).catch(() => {})
    }, 30_000)
    return () => { cancelled = true; clearInterval(t); clearInterval(reconcile) }
  }, [hasActive, slot, dispatch])
  // Auto-collapse when agents finish
  useEffect(() => { if (!hasActive) setExpanded(false) }, [hasActive])
  if (!hasActive) return null
  return (
    <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
    <div className="mb-1 rounded-md bg-accent/10 border border-accent/20 animate-slide-up overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[13px] font-mono cursor-pointer hover:bg-accent/5 transition-colors"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        aria-label={`${running} subagent${running > 1 ? 's' : ''} running`}
      >
        <Bot size={14} className="text-accent shrink-0" />
        <span className="text-text-strong font-medium">{running} agent{running > 1 ? 's' : ''} running</span>
        <ChevronDown size={14} className={`text-muted ml-auto shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`} />
      </button>
      {expanded && activeList.length > 0 && (
        <div className="px-3 pb-2 space-y-0.5">
          {activeList.map((a, i) => {
            const isLast = i === activeList.length - 1
            const taskPreview = sanitizeLlmOutput((a.task || '').slice(0, 80)) + ((a.task || '').length > 80 ? '…' : '')
            const elapsed = Math.round((Date.now() - a.startedAt) / 1000)
            return (
              <div key={a.id} className="flex items-start gap-1.5 text-[12px] text-muted font-mono">
                <span className="shrink-0 text-border select-none">{isLast ? '└─' : '├─'}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-text">{taskPreview || sanitizeLlmOutput(a.agent || 'agent')}</span>
                    <span className="shrink-0 tabular-nums text-muted/50">{elapsed}s</span>
                  </div>
                  {a.lastTool && <div className="text-accent/60 truncate">→ {sanitizeLlmOutput(a.lastTool)}</div>}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
    </div>
  )
})

export default SubagentProgressBar
