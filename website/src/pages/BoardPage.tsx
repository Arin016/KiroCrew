import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from '../store'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { api } from '../api/client'
import { PageHeader, EmptyState } from '../components/ui'
import type { ChatSlot, PendingApproval } from '../types'
import { AlertTriangle, Ban, Check, CheckCircle, Clock, LayoutGrid, Loader2, MessageSquareDot, X, Zap } from 'lucide-react'
import TrustDropdown from '../components/TrustDropdown'

type Lane = 'approval' | 'your_turn' | 'working' | 'idle'

const LANES: { id: Lane; label: string; icon: React.ReactNode; hint: string }[] = [
  { id: 'approval',  label: 'Needs approval',  icon: <Clock size={14} />,            hint: 'Tool call awaiting your approve / deny' },
  { id: 'your_turn', label: 'Your turn',       icon: <MessageSquareDot size={14} />, hint: 'Agent finished — waiting for your input' },
  { id: 'working',   label: 'Working',         icon: <Zap size={14} />,              hint: 'Agent is actively processing' },
  { id: 'idle',      label: 'Idle',            icon: <Clock size={14} />,            hint: 'Session alive, no recent activity' },
]

const YOUR_TURN_DECAY_MS = 30 * 60 * 1000  // 30 minutes → Your Turn decays to Idle

const STALL_REASON_LABEL: Record<string, string> = {
  subagent_timeout: 'Subagent result injection timed out',
  prompt_stuck:     'Prompt already in progress',
}

type StallInfo = { reason: string; since_ts: number }
type HealthResponse = { stalled: Record<string, StallInfo> }

/**
 * Lane priority logic (highest → lowest):
 *   1. approval  — pending tool approval takes absolute precedence; user must act
 *   2. idle      — stalled sessions (subagent_timeout / prompt_stuck) are demoted
 *                  to idle even if they appear "running" or "your_turn", because
 *                  the stall hides the real state and the user can't interact
 *   3. working   — agent is actively processing (running flag from backend)
 *   4. your_turn — agent finished, waiting for user input; decays to idle after
 *                  YOUR_TURN_DECAY_MS to avoid stale "your turn" cards
 *   5. idle      — default fallback for sessions with no actionable state
 */
function inferLane(slot: ChatSlot, stalled: Record<string, StallInfo>): Lane {
  if (slot.pending_approval && slot.pending_approval_info) return 'approval'
  if (stalled[slot.key]) return 'idle'   // stall overrides running & your_turn
  if (slot.running) return 'working'
  if (slot.has_options || slot.waiting_for_input) {
    if (slot.last_activity_ts) {
      const age = Date.now() - new Date(slot.last_activity_ts).getTime()
      if (age > YOUR_TURN_DECAY_MS) return 'idle'
    }
    return 'your_turn'
  }
  return 'idle'
}

function timeAgo(iso?: string): string {
  if (!iso) return ''
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 0 || !isFinite(s)) return ''
  if (s < 60) return '<1m ago'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function lastActionText(slot: ChatSlot, lane: Lane): string {
  // On the Your Turn lane, prefer the longer server-side prompt_preview so the
  // user has full context for which option to click.
  if (lane === 'your_turn' && slot.prompt_preview) {
    const firstLine = slot.prompt_preview.split('\n').find(l => l.trim()) ?? slot.prompt_preview
    return firstLine
  }
  const raw = (slot.last_message || '').trim()
  if (!raw) return '(no messages yet)'
  // Strip the [OPTIONS: ...] suffix so the card shows the real content.
  const cleaned = raw.replace(/\[OPTIONS:[^\]]*\]\s*$/, '').trim()
  const firstLine = cleaned.split('\n').find(l => l.trim()) ?? cleaned
  return firstLine.length > 140 ? firstLine.slice(0, 137) + '…' : firstLine
}

function SlotCard({ slot, stall, approval, lane, locked, sentChoice, onOpen, onChoose, onApprove, onClose }: {
  slot: ChatSlot
  stall?: StallInfo
  approval?: PendingApproval
  lane: Lane
  locked: boolean
  sentChoice: string | null
  onOpen: () => void
  onChoose: (choice: string) => void
  onApprove: (action: string, pattern?: string) => void
  onClose: () => void
}) {
  const stallReason = stall ? (STALL_REASON_LABEL[stall.reason] ?? stall.reason) : null
  const showOptions = lane === 'your_turn' && slot.options && slot.options.length > 0
  const showApproval = lane === 'approval' && approval !== undefined
  const showWaitingBadge = lane === 'your_turn' && !showOptions
  const activityAgo = slot.last_activity_ts ? timeAgo(slot.last_activity_ts) : ''
  const handleChoose = (e: React.MouseEvent, opt: string) => {
    e.stopPropagation()
    e.preventDefault()
    if (locked) return
    onChoose(opt)
  }
  const handleApprove = (e: React.MouseEvent, action: string, pattern?: string) => {
    e.stopPropagation()
    e.preventDefault()
    if (locked) return
    onApprove(action, pattern)
  }
  return (
    <div
      role="group"
      aria-label={`Session: ${slot.title || slot.key}`}
      className={`relative w-full text-left bg-card border border-border rounded-lg p-3 transition-all duration-300 ${locked ? 'opacity-60 pointer-events-none' : 'hover:bg-bg-hover hover:border-accent cursor-pointer'}`}
    >
      <button
        onClick={locked ? undefined : onOpen}
        disabled={locked}
        onKeyDown={(e) => { if (!locked && e.key === 'Enter' && e.target === e.currentTarget) onOpen() }}
        className="absolute inset-0 z-0 bg-transparent border-none cursor-pointer"
        aria-label={`Open session ${slot.title || slot.key}`}
        tabIndex={locked ? -1 : 0}
      />
      <div className="relative z-10">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-sm font-medium text-text-strong truncate">{slot.title || slot.key}</div>
        <div className="flex items-center gap-1.5 shrink-0">
          <div className="text-[13px] text-muted whitespace-nowrap">{timeAgo(slot.last_ts)}</div>
          <button
            onClick={(e) => { e.stopPropagation(); e.preventDefault(); onClose() }}
            className="text-muted hover:text-danger p-0.5 rounded transition-colors"
            title="Close session"
            aria-label="Close session"
          >
            <X size={14} />
          </button>
        </div>
      </div>
      <div className="text-[13px] text-muted mb-2">
        {slot.agent || 'default'} · {slot.messages} msgs
      </div>
      {stallReason && (
        <div className="text-[13px] text-warn mb-2 flex items-center gap-1">
          <AlertTriangle size={12} /> {stallReason}
        </div>
      )}
      {showWaitingBadge && (
        <div className="text-[13px] text-accent mb-2 flex items-center gap-1 font-medium">
          <MessageSquareDot size={12} /> Waiting for your input
        </div>
      )}
      {lane === 'working' && activityAgo && (
        <div className="text-[13px] text-muted mb-1 flex items-center gap-1">
          <Loader2 size={10} className="animate-spin" /> Active · {activityAgo}
        </div>
      )}
      {lane === 'idle' && activityAgo && (
        <div className="text-[13px] text-muted mb-1">
          Last activity {activityAgo}
        </div>
      )}
      <div className={lane === 'your_turn' ? 'text-[13px] text-text/80 line-clamp-4 whitespace-pre-line' : 'text-[13px] text-text/80 line-clamp-2'}>
        {lastActionText(slot, lane)}
      </div>
      {showApproval && (
        <div className="mt-2 border border-warn/40 bg-warn/5 rounded p-2">
          <div className="text-[13px] font-semibold text-text-strong mb-1">
            Tool call: <span className="font-mono text-accent">{approval?.tool || 'unknown'}</span>
          </div>
          {approval?.tool_input && (
            <div className="text-[11px] text-muted font-mono whitespace-pre-wrap break-all line-clamp-3 mb-2">
              {approval?.tool_input}
            </div>
          )}
          <div className="flex flex-wrap gap-1.5">
            {(() => {
              const toolName = approval?.tool || ''
              const isShell = toolName.startsWith('Running: ')
              const normalized = toolName.replace(/^(Running: |Reading )/, '')
              const baseCmd = normalized.split(/\s+/)[0] || normalized
              const btnCls = 'text-[13px] px-2 py-1 rounded border transition-colors flex items-center gap-1 border-accent/60 text-accent hover:bg-accent hover:text-bg'
              const isSentApprove = sentChoice === 'approved'
              const isSentReject = sentChoice === 'rejected'
              return (
                <>
                  <button onClick={(e) => handleApprove(e, 'approved')} disabled={locked}
                    className={`${btnCls} ${isSentApprove ? 'border-accent bg-accent text-bg' : ''} ${locked && !isSentApprove ? 'opacity-50' : ''}`}
                    title={locked ? 'Sending…' : 'Approve this tool call'}>
                    <CheckCircle size={12} /> {isSentApprove ? <><Check size={12} className="inline" /> Approve</> : 'Approve'}
                  </button>
                  <TrustDropdown
                    fullCommand={normalized}
                    baseCommand={baseCmd}
                    isShell={isShell}
                    disabled={locked}
                    className={btnCls}
                    onAction={(action, pattern) => onApprove(action, pattern)}
                  />
                  <button onClick={(e) => handleApprove(e, 'rejected')} disabled={locked}
                    className={`text-[13px] px-2 py-1 rounded border transition-colors flex items-center gap-1 border-danger/60 text-danger hover:bg-danger hover:text-bg ${isSentReject ? 'border-danger bg-danger text-bg' : ''} ${locked && !isSentReject ? 'opacity-50' : ''}`}
                    title={locked ? 'Sending…' : 'Reject this tool call'}>
                    <Ban size={12} /> {isSentReject ? <><Check size={12} className="inline" /> Reject</> : 'Reject'}
                  </button>
                </>
              )
            })()}
          </div>
        </div>
      )}
      {showOptions && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {slot.options!.map((opt, i) => {
            const isSent = sentChoice === opt
            return (
              <button
                key={i}
                onClick={(e) => handleChoose(e, opt)}
                disabled={locked}
                className={`text-[13px] px-2 py-1 rounded border transition-colors text-left whitespace-normal break-words max-w-full ${isSent ? 'border-accent bg-accent text-bg' : 'border-accent/60 text-accent hover:bg-accent hover:text-bg'} ${locked && !isSent ? 'opacity-50' : ''}`}
                title={locked ? 'Sending…' : `Reply "${opt}" to this session`}
              >
                {isSent ? <><Check size={12} className="inline" /> {opt}</> : opt}
              </button>
            )
          })}
        </div>
      )}
      </div>
    </div>
  )
}

function LaneColumn({ lane, slots, stalled, approvals, lockedMap, onOpen, onChoose, onApprove, onClose }: {
  lane: typeof LANES[number]
  slots: ChatSlot[]
  stalled: Record<string, StallInfo>
  approvals: Record<string, PendingApproval>
  lockedMap: Record<string, string>
  onOpen: (key: string) => void
  onChoose: (key: string, choice: string) => void
  onApprove: (key: string, action: string, pattern?: string) => void
  onClose: (key: string, title: string) => void
}) {
  return (
    <div role="region" aria-label={lane.label} className="flex flex-col min-w-0 bg-bg-accent border border-border rounded-lg p-3 h-full">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-muted">{lane.icon}</span>
        <h2 className="text-sm font-semibold text-text-strong">{lane.label}</h2>
        <span className="text-[13px] text-muted ml-auto">{slots.length}</span>
      </div>
      <p className="text-[13px] text-muted mb-3">{lane.hint}</p>
      <div className="flex flex-col gap-2 overflow-y-auto">
        {slots.length === 0 ? (
          <div className="text-[13px] text-muted italic py-4 text-center">—</div>
        ) : (
          slots.map(s => (
            <SlotCard
              key={s.key}
              slot={s}
              stall={stalled[s.key]}
              approval={approvals[s.key]}
              lane={lane.id}
              locked={lockedMap[s.key] !== undefined}
              sentChoice={lockedMap[s.key] ?? null}
              onOpen={() => onOpen(s.key)}
              onChoose={(choice) => onChoose(s.key, choice)}
              onApprove={(action, pattern) => onApprove(s.key, action, pattern)}
              onClose={() => onClose(s.key, s.title || s.key)}
            />
          ))
        )}
      </div>
    </div>
  )
}

export default function BoardPage() {
  const slots = useAppSelector(s => s.dashboard.slots) as ChatSlot[]
  const navigate = useNavigate()
  const dispatch = useAppDispatch()

  // Force periodic re-render so time-ago labels and lane decay stay fresh
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  const { data: health, isLoading: healthLoading, isError: healthError } = useQuery<HealthResponse>({
    queryKey: ['sessions-health'],
    queryFn: api.sessionsHealth,
    refetchInterval: 15_000,
    staleTime: 10_000,
  })
  const stalled = health?.stalled ?? {}

  // Pending tool approvals come in on each slot as `pending_approval_info`
  // (populated by _ChatSlot.to_dict when a permission message is unresolved).
  // No separate query needed — SSE keeps this fresh.
  const approvalBySlot: Record<string, PendingApproval> = {}
  for (const s of slots) {
    if (s.pending_approval_info) approvalBySlot[s.key] = s.pending_approval_info
  }

  const byLane: Record<Lane, ChatSlot[]> = { approval: [], your_turn: [], working: [], idle: [] }
  for (const slot of slots) byLane[inferLane(slot, stalled)].push(slot)

  const openSlot = (key: string) => {
    const slot = slots.find(s => s.key === key)
    dispatch(switchSlot(key))
    navigate(slot?.mode === 'orchestrator' ? '/orchestrated' : '/chat')
  }

  // Lockout map survives across lane reflow (SSE can move a slot to a different
  // lane between click and SSE ack). Keyed by slot.key -> chosen action string.
  // Entry is cleared by a 3 000 ms timer; if the slot is still in approval/waiting
  // lane after that, the user can click again.
  const [lockedMap, setLockedMap] = useState<Record<string, string>>({})
  const lockGuard = useRef(new Set<string>())
  const lockTimers = useRef(new Map<string, number>())
  const unlockSlot = (key: string) => {
    lockGuard.current.delete(key)
    const tid = lockTimers.current.get(key)
    if (tid !== undefined) clearTimeout(tid)
    lockTimers.current.delete(key)
    setLockedMap(m => {
      if (!(key in m)) return m  // same reference → no re-render
      const { [key]: _, ...rest } = m
      return rest
    })
  }
  const lockSlot = (key: string, action: string) => {
    const prev = lockTimers.current.get(key)
    if (prev !== undefined) clearTimeout(prev)
    lockGuard.current.add(key)
    setLockedMap(m => ({ ...m, [key]: action }))
    const id = window.setTimeout(() => unlockSlot(key), 3_000)
    lockTimers.current.set(key, id)
  }
  // Fix #1: Clear all pending lock timeouts on unmount to prevent timer leaks
  useEffect(() => {
    return () => {
      lockTimers.current.forEach(id => clearTimeout(id))
      lockTimers.current.clear()
    }
  }, [])
  // Clear locks reactively when SSE updates change slot state
  const lockedMapRef = useRef<Record<string, string>>({})
  lockedMapRef.current = lockedMap
  useEffect(() => {
    const current = lockedMapRef.current
    const keysToUnlock: string[] = []
    for (const key of Object.keys(current)) {
      const slot = slots.find(s => s.key === key)
      if (!slot) { keysToUnlock.push(key); continue }
      const wasApproval = ['approved', 'trust', 'rejected'].includes(current[key])
      if (wasApproval && (!slot.pending_approval || slot.running)) keysToUnlock.push(key)
      else if (!wasApproval && (slot.running || !slot.has_options)) keysToUnlock.push(key)
    }
    if (keysToUnlock.length > 0) {
      for (const key of keysToUnlock) {
        lockGuard.current.delete(key)
        const tid = lockTimers.current.get(key)
        if (tid !== undefined) { clearTimeout(tid); lockTimers.current.delete(key) }
      }
      setLockedMap(prev => {
        const next = { ...prev }
        for (const key of keysToUnlock) delete next[key]
        return next
      })
    }
  }, [slots])  // only slots — the signal that backend state actually changed

  const chooseOption = (key: string, choice: string) => {
    if (lockGuard.current.has(key)) return
    lockSlot(key, choice)
    void api.sendChat(choice, key).catch(() => unlockSlot(key))
  }
  const resolveApproval = (slotKey: string, action: string, pattern?: string) => {
    if (lockGuard.current.has(slotKey)) return
    lockSlot(slotKey, action)
    const extra: Record<string, string> = {}
    const rid = approvalBySlot[slotKey]?.request_id
    if (rid) extra.request_id = rid
    if (pattern) extra.pattern = pattern
    void api.approveChatSlot(slotKey, action, extra).catch(() => unlockSlot(slotKey))
  }
  const closeSlot = (key: string, title: string) => {
    if (window.confirm(`Close session "${title}"?\n\nThis will end the chat and remove it from the board.`)) {
      void dispatch(deleteSlot(key))
    }
  }

  if (slots.length === 0) {
    return (
      <div className="p-6">
        <PageHeader title="Board" subtitle="Auto-sorted lanes for every active session" />
        <EmptyState icon={<LayoutGrid size={32} />} title="No active sessions" subtitle="Start a chat to populate the board." />
      </div>
    )
  }

  return (
    <div data-testid="board-page" className="p-6 h-full flex flex-col">
      <PageHeader title="Board" subtitle="Auto-sorted lanes for every active session" />
      {healthLoading && (
        <div className="text-[13px] text-muted mb-2 flex items-center gap-1">
          <Loader2 size={12} className="animate-spin" /> Checking session health…
        </div>
      )}
      {healthError && (
        <div className="text-[13px] text-warn mb-2 flex items-center gap-1">
          <AlertTriangle size={12} /> Could not load session health
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 flex-1 min-h-0">
        {LANES.map(lane => (
          <LaneColumn
            key={lane.id}
            lane={lane}
            slots={byLane[lane.id]}
            stalled={stalled}
            approvals={approvalBySlot}
            lockedMap={lockedMap}
            onOpen={openSlot}
            onChoose={chooseOption}
            onApprove={resolveApproval}
            onClose={closeSlot}
          />
        ))}
      </div>
    </div>
  )
}
