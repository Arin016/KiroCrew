import { useState, useMemo, useCallback, type ReactNode } from 'react'
import { ClipboardList, Anchor, Heart, Bot, Lock, GitBranch, Bell, X, MailOpen, Check, MessageSquare, CheckCircle, Ban, ArrowLeft, Clock } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAppSelector, useAppDispatch } from '../store'
import {
  deleteNotification, clearNotifications, ackAllNotifications,
  ackNotification, unackNotification,
} from '../store/notificationsSlice'
import { switchSlot, resumeFromHistory } from '../store/chatSlice'
import { PageHeader, StatCard, Card, CardTitle, EmptyState, Badge, SearchInput } from '../components/ui'
import InfoTip from '../components/InfoTip'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { CronAckBar } from './chat'
import { api } from '../api/client'
import type { Notification } from '../types'

type Category = 'all' | 'cron' | 'hook' | 'heartbeat' | 'agent' | 'approval' | 'subagent' | 'taskrunner'

const CATEGORIES: { key: Category; label: string; icon: ReactNode }[] = [
  { key: 'all', label: 'All', icon: <ClipboardList className="lucide-inline" /> },
  { key: 'cron', label: 'Cron', icon: <Clock className="lucide-inline" /> },
  { key: 'hook', label: 'Hooks', icon: <Anchor className="lucide-inline" /> },
  { key: 'heartbeat', label: 'Heartbeat', icon: <Heart className="lucide-inline" /> },
  { key: 'agent', label: 'Agent', icon: <Bot className="lucide-inline" /> },
  { key: 'approval', label: 'Approval', icon: <Lock className="lucide-inline" /> },
  { key: 'subagent', label: 'Subagent', icon: <GitBranch className="lucide-inline" /> },
  { key: 'taskrunner', label: 'Tasks', icon: <ClipboardList className="lucide-inline" /> },
]

function parseTs(ts: string): Date {
  let d = new Date(ts)
  if (isNaN(d.getTime())) {
    const epoch = parseFloat(ts)
    if (!isNaN(epoch)) d = new Date(epoch * 1000)
  }
  if (isNaN(d.getTime()) || d.getTime() < Date.UTC(2020, 0, 1)) return new Date(NaN)
  return d
}

function dateGroup(d: Date): string {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86400000)
  const weekAgo = new Date(today.getTime() - 6 * 86400000)
  if (d >= today) return 'Today'
  if (d >= yesterday) return 'Yesterday'
  if (d >= weekAgo) return 'This Week'
  return d.toLocaleDateString([], { year: 'numeric', month: 'short' })
}

const KIND_META: Record<string, { icon: ReactNode; color: string; label: string; borderColor: string }> = {
  cron:       { icon: <Clock className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Cron Job',   borderColor: 'border-l-accent' },
  hook:       { icon: <Anchor className="lucide-inline" />, color: 'bg-info/15 text-info',      label: 'Webhook',    borderColor: 'border-l-info' },
  heartbeat:  { icon: <Heart className="lucide-inline" />, color: 'bg-ok/15 text-ok',          label: 'Heartbeat',  borderColor: 'border-l-ok' },
  agent:      { icon: <Bot className="lucide-inline" />, color: 'bg-info/15 text-info',      label: 'Agent',      borderColor: 'border-l-info' },
  approval:   { icon: <Lock className="lucide-inline" />, color: 'bg-warn/15 text-warn',      label: 'Approval',   borderColor: 'border-l-warn' },
  subagent:   { icon: <GitBranch className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Subagent',   borderColor: 'border-l-accent' },
  taskrunner: { icon: <ClipboardList className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Task Runner', borderColor: 'border-l-accent' },
}
const DEFAULT_META = { icon: <Bell className="lucide-inline" />, color: 'bg-muted/15 text-muted', label: 'Notification', borderColor: 'border-l-muted' }

function fmtTime(ts: string): string {
  const d = parseTs(ts)
  return isNaN(d.getTime()) ? 'Unknown date' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function fmtFull(ts: string): string {
  const d = parseTs(ts)
  return isNaN(d.getTime()) ? 'Unknown date' : d.toLocaleString()
}

function stripMd(text: string): string {
  return text.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[*_~`#>]+/g, '').replace(/\n+/g, ' ').trim()
}

/* ── Detail Panel (right side) ── */
function DetailPanel({ n, onClose, dispatch, navigate }: {
  n: Notification; onClose: () => void
  dispatch: ReturnType<typeof useAppDispatch>; navigate: ReturnType<typeof useNavigate>
}) {
  const km = KIND_META[n.kind] || DEFAULT_META
  const slots = useAppSelector(s => s.dashboard.slots)

  // Direct slot link from notification meta
  const directSlot = n.slot ? slots.find(s => s.key === n.slot) : null

  // Fuzzy match: try to find a related chat slot from the body/title
  const relatedSlot = useMemo(() => {
    if (directSlot) return null  // prefer direct
    for (const s of slots) {
      if (s.title && s.title.length >= 4 && n.body) {
        const re = new RegExp(`\\b${s.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i')
        if (re.test(n.body)) return s
      }
      if (n.title?.includes(s.key)) return s
    }
    return null
  }, [slots, n, directSlot])

  return (
    <div className="flex flex-col h-full border-l border-border bg-bg">
      {/* Header */}
      <div className="px-5 py-3 border-b border-border flex items-center justify-between bg-chrome shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[16px]">{km.icon}</span>
          <span className="text-sm font-semibold text-text-strong truncate">{n.title}</span>
        </div>
        <button className="text-muted text-[13px] cursor-pointer hover:text-text bg-transparent border-none font-body shrink-0 ml-2" onClick={onClose}><X className="lucide-inline" /> Close</button>
      </div>

      {/* Meta bar */}
      <div className="px-5 py-3 border-b border-border flex items-center gap-3 flex-wrap bg-bg-elevated shrink-0">
        <span className={`px-2 py-[3px] rounded-full text-[12px] font-bold ${km.color} border border-current/20`}>{km.label}</span>
        <span className="text-[13px] text-muted font-mono">{fmtFull(n.ts)}</span>
        {n.acked
          ? <Badge variant="ok">Read</Badge>
          : <Badge variant="warn">Unread</Badge>
        }
        {n.acked
          ? <button className="text-[13px] text-muted cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(unackNotification(n.ts))}><MailOpen className="lucide-inline" /> Mark unread</button>
          : <button className="text-[13px] text-ok cursor-pointer hover:text-text bg-transparent border-none font-body" onClick={() => dispatch(ackNotification(n.ts))}><Check className="lucide-inline" /> Mark read</button>
        }
      </div>

      {/* Source & navigation */}
      <div className="px-5 py-2.5 border-b border-border flex items-center gap-2 flex-wrap shrink-0">
        <span className="text-[12px] text-muted uppercase tracking-[.04em] font-medium">Source:</span>
        <span className="text-[13px] text-text">{km.label}{n.kind === 'cron' && n.job_id ? ` (${n.job_id.slice(0, 8)})` : n.kind === 'taskrunner' && n.task_id ? ` (${n.task_id.slice(0, 8)})` : (directSlot || relatedSlot) ? ` · ${(directSlot || relatedSlot)!.title || (directSlot || relatedSlot)!.key}` : ''}</span>
        <span className="flex-1" />
        {/* Jump-to buttons */}
        {n.kind === 'cron' && (
          <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => navigate('/schedule')}><Clock className="lucide-inline" /> View Cron Jobs</button>
        )}
        {n.kind === 'cron' && n.job_id && n.slot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(n.slot!)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> Continue session</button>
        )}
        {n.kind === 'cron' && n.job_id && !n.slot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { const res = await api.cronToChat(n.job_id!); if (res.error) { console.error('cronToChat error', res.error); return }; if (res.slot) { dispatch(switchSlot(res.slot)); navigate('/chat') } } catch (e) { console.error('cronToChat failed', e) } }}><MessageSquare className="lucide-inline" /> View last result</button>
        )}
        {directSlot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(directSlot.key)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> Go to Chat</button>
        )}
        {!directSlot && n.slot && !(n.kind === 'cron' && n.job_id && n.slot) && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await dispatch(resumeFromHistory({ key: n.slot!, title: n.title })); navigate('/chat') } catch (e) { console.error('Resume failed', e) } }}><MessageSquare className="lucide-inline" /> Resume Chat</button>
        )}
        {!directSlot && !n.slot && relatedSlot && (
          <button className="px-3 py-1.5 rounded-md bg-accent text-accent-fg text-[13px] font-medium cursor-pointer border-none hover:brightness-110 transition-all" onClick={() => { dispatch(switchSlot(relatedSlot.key)); navigate('/chat') }}><MessageSquare className="lucide-inline" /> Go to Chat</button>
        )}
        {n.slack_link && (
          <a href={n.slack_link} target="_blank" rel="noopener noreferrer" className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body no-underline inline-flex items-center gap-1"><MessageSquare className="lucide-inline" /> Open in Slack</a>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="msg-content bg-card border border-border rounded-lg px-5 py-4 text-sm leading-relaxed text-text shadow-[inset_0_1px_0_var(--card-hl)] max-w-[820px] overflow-x-auto break-words">
          <MarkdownRenderer content={n.body || ''} />
        </div>

        {/* Kind-specific actions */}
        {n.kind === 'approval' && (
          <div className="flex gap-3 mt-4">
            <button className="px-4 py-2 rounded-lg bg-ok text-ok-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await api.resolveApproval(n.approval_id || n.ts, 'approve'); dispatch(deleteNotification(n.ts)); onClose() } catch (e) { console.error('Approve failed', e) } }}><CheckCircle className="lucide-inline" /> Approve</button>
            <button className="px-4 py-2 rounded-lg bg-danger text-danger-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => { try { await api.resolveApproval(n.approval_id || n.ts, 'reject'); dispatch(deleteNotification(n.ts)); onClose() } catch (e) { console.error('Reject failed', e) } }}><Ban className="lucide-inline" /> Reject</button>
          </div>
        )}
        {n.kind === 'cron' && n.job_id && (
          <CronAckBar key={n.ts} notification={n} onDone={onClose} />
        )}
        {n.kind === 'taskrunner' && n.task_id && (
          <div className="flex gap-3 mt-4">
            <button className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => {
              try { const res = await api.taskRunToChat(n.task_id!); if (res.slot) { dispatch(switchSlot(res.slot)); navigate('/chat') } } catch (e) { console.error('Task nav failed', e) }
            }}><MessageSquare className="lucide-inline" /> Continue in Chat</button>
            <button className="px-3 py-1.5 rounded-md border border-border text-[13px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong transition-all font-body" onClick={() => navigate('/projects')}><ClipboardList className="lucide-inline" /> View Project</button>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Main Page ── */
export default function NotificationsPage() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const items = useAppSelector(s => s.notifications.items)
  const [category, setCategory] = useState<Category>('all')
  const [filter, setFilter] = useState('')
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const isMobile = useIsMobile()

  const filtered = useMemo(() => {
    let list = [...items].reverse()
    if (category !== 'all') list = list.filter(n => n.kind === category)
    if (filter) {
      const q = filter.toLowerCase()
      list = list.filter(n => ((n.title || '') + (n.body || '')).toLowerCase().includes(q))
    }
    return list
  }, [items, category, filter])

  const groups = useMemo(() => {
    const map = new Map<string, Notification[]>()
    for (const n of filtered) {
      const g = dateGroup(parseTs(n.ts))
      const arr = map.get(g)
      if (arr) arr.push(n); else map.set(g, [n])
    }
    return map
  }, [filtered])

  const unread = items.filter(n => !n.acked).length
  const byCat = useCallback((k: string) => items.filter(n => n.kind === k).length, [items])
  const selected = items.find(n => n.ts === selectedTs) || null

  // Auto-ack on select
  const handleSelect = useCallback((n: Notification) => {
    setSelectedTs(n.ts)
    if (!n.acked) dispatch(ackNotification(n.ts))
  }, [dispatch])

  return (
    <>
      <PageHeader title="Notifications" subtitle="All agent activity, cron results, webhooks, and approvals" />
      <div className="px-6 pb-8 flex-1 min-h-0 flex flex-col overflow-hidden">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))] mb-4 shrink-0">
          <StatCard label="Total" value={items.length} accent />
          <StatCard label="Unread" value={unread} />
          <StatCard label="Cron" value={byCat('cron')} />
          <StatCard label="Hooks" value={byCat('hook')} />
          <StatCard label="Heartbeat" value={byCat('heartbeat')} />
        </div>

        {/* Split layout: list + detail */}
        <div className="flex-1 min-h-0 flex gap-4">
          {/* Left: list */}
          <div className={`flex flex-col shrink-0 ${isMobile ? 'w-full' : 'min-w-[320px] max-w-[420px] w-[40%]'} ${isMobile && selected ? 'hidden' : ''}`}>
            <Card className="flex flex-col flex-1 min-h-0">
              <CardTitle>Activity Feed <InfoTip text="Click a notification to view details. Jump to the source chat or job from the detail panel." /></CardTitle>

              {/* Category tabs */}
              <div className="flex gap-1 mb-2 flex-wrap shrink-0">
                {CATEGORIES.map(c => (
                  <button key={c.key} className={`px-2 py-1 rounded-md text-[12px] font-medium cursor-pointer border transition-all font-body ${category === c.key ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'}`} onClick={() => setCategory(c.key)}>
                    {c.icon} {c.label}
                  </button>
                ))}
              </div>

              {/* Search + actions */}
              <div className="flex gap-2 mb-2 items-center shrink-0">
                <div className="flex-1"><SearchInput placeholder="Search…" value={filter} onChange={e => setFilter(e.target.value)} /></div>
                {unread > 0 && <button className="px-2 py-1 rounded-md border border-ok/40 bg-ok/10 text-ok text-[12px] font-semibold cursor-pointer hover:bg-ok/20 transition-all font-body whitespace-nowrap" onClick={() => dispatch(ackAllNotifications())}><Check className="lucide-inline" /> All</button>}
                {items.length > 0 && <button className="px-2 py-1 rounded-md border border-danger/40 bg-transparent text-danger text-[12px] font-medium cursor-pointer hover:bg-danger/10 transition-all font-body whitespace-nowrap" onClick={() => { if (confirm('Clear all notifications?')) { dispatch(clearNotifications()); setSelectedTs(null) } }}><X className="lucide-inline" /> Clear</button>}
              </div>

              {/* List */}
              <div className="flex-1 overflow-y-auto scroll-shadow">
                {filtered.length === 0 ? (
                  <EmptyState icon={<Bell className="lucide-inline" />} title="No notifications" subtitle={filter ? 'Try a different search' : 'Activity will appear here'} />
                ) : (
                  Array.from(groups.entries()).map(([group, notes]) => (
                    <div key={group} className="mb-3">
                      <div className="text-[11px] font-semibold text-muted uppercase tracking-[.04em] mb-1.5 px-1">{group}</div>
                      {notes.map(n => {
                        const km = KIND_META[n.kind] || DEFAULT_META
                        const active = selectedTs === n.ts
                        return (
                          <div key={n.ts}
                            className={`group flex items-center gap-2 px-2.5 py-2 rounded-md mb-1 cursor-pointer transition-all border-l-[3px] ${km.borderColor} ${active ? 'bg-accent-subtle border border-accent' : 'border border-transparent hover:bg-bg-hover hover:border-border'} ${n.acked && !active ? 'opacity-50' : ''}`}
                            onClick={() => handleSelect(n)}
                          >
                            <span className="text-[13px] shrink-0">{km.icon}</span>
                            <div className="flex-1 min-w-0">
                              <div className="text-[13px] font-medium text-text-strong truncate leading-tight">{n.title}</div>
                              <div className="text-[12px] text-muted truncate mt-0.5">{stripMd(n.body || '').slice(0, 80)}</div>
                            </div>
                            <div className="flex flex-col items-end gap-0.5 shrink-0">
                              <span className="text-[11px] text-muted font-mono">{fmtTime(n.ts)}</span>
                              {!n.acked && <span className="w-1.5 h-1.5 rounded-full bg-accent animate-dot-breathe" />}
                            </div>
                            <span className="opacity-0 group-hover:opacity-40 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0" onClick={e => { e.stopPropagation(); dispatch(deleteNotification(n.ts)); if (selectedTs === n.ts) setSelectedTs(null) }}><X className="lucide-inline" /></span>
                          </div>
                        )
                      })}
                    </div>
                  ))
                )}
              </div>
            </Card>
          </div>

          {/* Right: detail panel */}
          {isMobile && selected ? (
            <div className="flex-1 min-w-0">
              <Card className="flex flex-col h-full min-h-0">
                <button className="flex items-center gap-1 px-2 py-1.5 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none mb-1" onClick={() => setSelectedTs(null)}>
                  <ArrowLeft size={14} /> Back
                </button>
                <DetailPanel
                  key={selected.ts}
                  n={selected}
                  onClose={() => setSelectedTs(null)}
                  dispatch={dispatch}
                  navigate={navigate}
                />
              </Card>
            </div>
          ) : !isMobile && <div className="flex-1 min-w-0">
            {selected ? (
              <Card className="flex flex-col h-full min-h-0">
                <DetailPanel
                  key={selected.ts}
                  n={selected}
                  onClose={() => setSelectedTs(null)}
                  dispatch={dispatch}
                  navigate={navigate}
                />
              </Card>
            ) : (
              <Card className="flex items-center justify-center h-full">
                <EmptyState icon={<ArrowLeft className="lucide-inline" />} title="Select a notification" subtitle="Click any item to view details and navigate to its source" />
              </Card>
            )}
          </div>}
        </div>
      </div>
    </>
  )
}
