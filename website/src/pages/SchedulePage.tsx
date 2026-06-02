import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { List, CalendarDays, ClipboardList, ChevronRight, Globe, Check, History } from 'lucide-react'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Btn, SendBtn, Badge, SearchInput, EmptyState, Skeleton } from '../components/ui'
import SegmentedControl from '../components/SegmentedControl'
import WeekGrid from '../components/WeekGrid'
import TimezoneSelect from '../components/TimezoneSelect'
import JobForm from '../components/JobForm'
import JobLogsView from '../components/JobLogsView'
import type { KiroClawAgent } from '../components/AgentSelector'
import InfoTip from '../components/InfoTip'
import type { CronJob } from '../types'
import { useAgents } from '../hooks/useAgents'
import { useCronActions } from '../hooks/useCronActions'
import { SaveCreateLabel } from '../utils/cronUtils'
import { useSortableTable } from '../hooks/useSortableTable'
import SortableHeader from '../components/SortableHeader'
import ExecutionsView from '../components/ExecutionsView'
import { sanitizeLlmOutput } from '../utils/sanitize'

const RENDER_TZ_STORAGE_KEY = 'kiroclaw.schedule.renderTz'
/**
 * Collapsed-by-default message cell. Shows a 1-line preview with a chevron;
 * click to toggle a <pre> block that preserves whitespace/indentation.
 * Accepts pre-sanitized message to avoid double sanitization (parent memoizes).
 */
export function CollapsibleMessage({ message }: { message: string }) {
  const [open, setOpen] = useState(false)
  const safe = useMemo(() => sanitizeLlmOutput(message), [message])
  const preview = safe.length > 80 ? safe.slice(0, 80).replace(/\s+/g, ' ') + '…' : safe.replace(/\s+/g, ' ')
  return (
    <div className="text-sm">
      <Btn
        onClick={e => { e.stopPropagation(); setOpen(v => !v) }}
        className="!p-0 !border-none !rounded-none flex items-start gap-1 text-left w-full hover:text-text-strong"
        title={open ? 'Collapse' : 'Expand'}
      >
        <ChevronRight size={14} className={`mt-[3px] shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span className={open ? 'text-muted text-[12px] min-w-0' : 'truncate min-w-0'}>{open ? 'Hide message' : preview}</span>
      </Btn>
      {open && (
        <pre
          onClick={e => e.stopPropagation()}
          className="mt-1.5 p-2.5 bg-bg-elevated border border-border rounded-md text-[12px] font-mono whitespace-pre-wrap break-words max-h-[280px] overflow-y-auto leading-relaxed"
        >{safe}</pre>
      )}
    </div>
  )
}


const fmtAgo = (ts?: number) => {
  if (!ts) return '—'
  const s = Math.floor((Date.now() / 1000) - ts)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

const fmtIn = (ts?: number | null) => {
  if (ts == null) return '—'
  const s = Math.floor(ts - Date.now() / 1000)
  if (s <= 0) return 'now'
  if (s < 60) return 'in <1m'
  if (s < 3600) return `in ${Math.floor(s / 60)}m`
  if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); return `in ${h}h ${m}m` }
  const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); return `in ${d}d ${h}h`
}

export default function SchedulePage() {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const { agents, defaultAgent } = useAgents(0)
  const [cronFilter, setCronFilter] = useState('')
  const [selected, setSelected] = useState<CronJob | null>(null)
  const [creating, setCreating] = useState(false)
  const [jobsView, setJobsView] = useState<'list' | 'calendar' | 'executions'>('list')
  const [renderTz, setRenderTz] = useState<string>(() => {
    try {
      const stored = localStorage.getItem(RENDER_TZ_STORAGE_KEY)
      if (stored) return stored
    } catch {
      // localStorage unavailable (private mode) — fall through to default
    }
    return Intl.DateTimeFormat().resolvedOptions().timeZone
  })
  useEffect(() => {
    try {
      localStorage.setItem(RENDER_TZ_STORAGE_KEY, renderTz)
    } catch {
      // localStorage unavailable — don't block rendering
    }
  }, [renderTz])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const sanitizedJobs = useMemo(() => jobs.map(j => ({ ...j, safeMessage: sanitizeLlmOutput(j.message) })), [jobs])

  const load = useCallback(async () => {
    try {
      setLoadError(null)
      const d = await api.crons()
      const fresh: CronJob[] = d.jobs || []
      setJobs(fresh)
      setSelected(prev => prev ? fresh.find((j: CronJob) => j.id === prev.id) ?? null : null)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Failed to load jobs')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => { load() }, [load])

  const { running, actionError, setActionError, runNow, openInChat } = useCronActions(load)
  const filteredJobs = useMemo(() => sanitizedJobs.filter(j => !cronFilter || (j.name+' '+j.safeMessage+' '+(j.agent||'')).toLowerCase().includes(cronFilter.toLowerCase())), [sanitizedJobs, cronFilter])
  const scheduleComparators = useMemo(() => ({
    name: (a: CronJob, b: CronJob) => a.name.localeCompare(b.name),
    schedule: (a: CronJob, b: CronJob) => (a.schedule || '').localeCompare(b.schedule || ''),
    status: (a: CronJob, b: CronJob) => {
      const rank = (j: CronJob) =>
        !j.enabled ? 0 : j.last_status === 'error' ? 1 : j.last_status === 'ok' ? 2 : 3;
      return rank(a) - rank(b);
    },
    lastRun: (a: CronJob, b: CronJob) => (a.last_run_ts || 0) - (b.last_run_ts || 0),
    nextRun: (a: CronJob, b: CronJob) => (a.next_run_ts || 0) - (b.next_run_ts || 0),
  }), [])
  const { sorted: sortedScheduleJobs, sort: schedSort, toggle: toggleSchedSort } = useSortableTable(filteredJobs, 'cron-schedule', scheduleComparators, { key: 'nextRun', dir: 'asc' })

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        <PageHeader title="Schedule" subtitle="Manage recurring cron jobs and scheduled tasks" />
        <div className="flex-1 overflow-y-auto px-6 pb-8 min-h-0">
          {loadError ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <p className="text-danger text-sm mb-3">{loadError}</p>
              <Btn onClick={load}>Retry</Btn>
            </div>
          ) : loading ? (
            <div className="flex items-center justify-center py-20"><Skeleton className="h-6 w-32 rounded" /></div>
          ) : jobs.length === 0 && !creating ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <svg className="w-16 h-16 stroke-current fill-none text-muted/20 mb-4" viewBox="0 0 24 24" strokeWidth={1} strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>
                <line x1="3" y1="10" x2="21" y2="10"/>
                <circle cx="12" cy="15" r="1.5"/>
                <path d="M9.5 15h-2M16.5 15h-2"/>
              </svg>
              <div className="text-muted text-sm font-medium">No scheduled jobs yet</div>
              <p className="text-sm text-muted max-w-[360px] mb-5 mt-2">Schedule recurring tasks to run automatically — check pipelines, generate reports, monitor services, or anything your agent can do.</p>
              <SendBtn onClick={() => { setSelected(null); setCreating(true) }}>
                <span className="flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  Create your first job
                </span>
              </SendBtn>
              <p className="text-[12px] text-muted mt-3">or <a href="/chat" className="text-accent hover:underline">ask in chat</a> — try "remind me to check my pipeline every morning"</p>
            </div>
          ) : (<>
          <div className="flex items-center gap-2 px-3 py-2.5 mb-4 rounded-lg bg-accent-subtle border border-accent/20 text-[13px] text-text">
            <svg className="w-4 h-4 stroke-current fill-none shrink-0 text-accent" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <span>You can also create schedules by chatting — try <em>"remind me to check my pipeline every morning at 9am"</em></span>
            <a href="/chat" className="ml-auto text-accent text-[13px] font-medium shrink-0 hover:underline">Open Chat</a>
          </div>

          <Card><CardTitle>
            <div className="flex items-center justify-between w-full">
              <span className="flex items-center gap-1.5">Jobs <InfoTip text="Scheduled jobs run on the configured interval or cron expression." /></span>
              <div className="flex items-center gap-2">
                <SendBtn onClick={() => { setSelected(null); setCreating(true) }}>
                  <span className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                    Add Job
                  </span>
                </SendBtn>
                <SegmentedControl
                  segments={[
                    { key: 'list' as const, label: 'List', icon: <List size={14} /> },
                    { key: 'calendar' as const, label: 'Calendar', icon: <CalendarDays size={14} /> },
                    { key: 'executions' as const, label: 'Executions', icon: <History size={14} /> },
                  ]}
                  value={jobsView}
                  onChange={setJobsView}
                  layoutId="schedule-view"
                />
              </div>
            </div>
          </CardTitle>
            {jobsView === 'calendar' ? (<>
              <div className="flex items-center gap-2 mb-3 text-[13px] text-muted">
                <Globe className="lucide-inline" />
                <label htmlFor="schedule-render-tz" className="mr-1">Render in</label>
                <TimezoneSelect id="schedule-render-tz" value={renderTz} onChange={setRenderTz} />
                <InfoTip text="Changes only how the calendar grid is displayed — does not change when any job actually fires." />
              </div>
              <WeekGrid jobs={jobs} selectedId={selected?.id} onSelect={setSelected} renderTz={renderTz} />
            </>) : jobsView === 'executions' ? (
              <ExecutionsView selectedJobId={selected?.id} />
            ) : (<>
            <div className="mb-3"><SearchInput placeholder="Filter jobs…" value={cronFilter} onChange={e => setCronFilter(e.target.value)} /></div>
            <div className="overflow-x-auto"><table className="w-full border-collapse table-striped"><thead><tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[72px]">ID</th>
              <SortableHeader label="Name" sortKey="name" sort={schedSort} onToggle={toggleSchedSort} className="w-[100px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">Type</th>
              <SortableHeader label="Schedule" sortKey="schedule" sort={schedSort} onToggle={toggleSchedSort} className="w-[110px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[200px]">Message</th>
              <SortableHeader label="Status" sortKey="status" sort={schedSort} onToggle={toggleSchedSort} className="w-[70px]" />
              <SortableHeader label="Last Run" sortKey="lastRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[80px]" />
              <SortableHeader label="Next Run" sortKey="nextRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[90px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[180px]">Actions</th>
            </tr></thead>
            <tbody>{jobs.length === 0
              ? <tr><td colSpan={9}><EmptyState icon={<ClipboardList className="lucide-inline" />} title="No cron jobs" /></td></tr>
              : sortedScheduleJobs.length === 0
              ? <tr><td colSpan={9} className="text-muted italic px-2.5 py-3.5 text-sm">No matching jobs</td></tr>
              : sortedScheduleJobs.map(j => (
              <tr key={j.id} className={`hover:bg-bg-hover transition-colors cursor-pointer ${selected?.id === j.id ? 'bg-accent-subtle' : ''}`} onClick={() => { setCreating(false); setSelected(selected?.id === j.id ? null : j) }}>
                <td className="px-2.5 py-2 border-b border-border text-sm"><code>{j.id}</code></td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{j.name}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{j.script ? <span className="text-[var(--accent)] font-medium text-[13px]">script · python</span> : j.command ? <span className="text-[var(--warn)] font-medium text-[13px]">command · shell</span> : <span className="text-muted text-[13px]">agent · {j.agent || 'default'}</span>}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><code>{j.schedule}</code>{j.timezone && <span className="block text-[11px] text-muted">{j.timezone.replace(/_/g, ' ')}</span>}</td>
                <td className="px-2.5 py-2 border-b border-border align-top max-w-[360px]"><CollapsibleMessage message={j.script ? j.script : j.command ? j.command : j.safeMessage} /></td>
                <td className="px-2.5 py-2 border-b border-border text-sm" title={j.last_error || j.last_result || ''}>{j.enabled ? (j.last_status === 'ok' ? <Badge variant="ok">OK</Badge> : j.last_status === 'error' ? <Badge variant="err">Error</Badge> : <Badge variant="ok">Ready</Badge>) : <Badge variant="warn">Paused</Badge>}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{fmtAgo(j.last_run_ts)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted" title={j.next_run_ts ? new Date(j.next_run_ts * 1000).toLocaleString() : ''}>{fmtIn(j.next_run_ts)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm whitespace-nowrap" onClick={e => e.stopPropagation()}>
                  <span title={j.strict_schedule ? 'Disable strict schedule (allow jitter)' : 'Enable strict schedule (no jitter)'}><Btn onClick={async () => { try { await api.updateCron(j.id, { strict_schedule: !j.strict_schedule }); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : 'Failed' }) } }}>{j.strict_schedule ? <><Check className="lucide-inline" /> Strict</> : 'Strict'}</Btn></span>{' '}
                  <span title={j.enabled ? 'Run now' : 'Resume to run'}><Btn onClick={() => runNow(j.id)} disabled={!j.enabled || running.has(j.id)}>{running.has(j.id) ? '...' : 'Run'}</Btn></span>{' '}
                  <span title={j.has_slot ? 'Continue session' : j.has_result ? 'View last result' : 'No result'}><Btn onClick={() => openInChat(j.id)} disabled={!j.has_result && !j.has_slot}>{j.has_slot ? 'Continue' : 'View'}</Btn></span>{' '}
                  <Btn onClick={async () => { try { await api.toggleCron(j.id, !j.enabled); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : 'Failed' }) } }}>{j.enabled ? 'Pause' : 'Resume'}</Btn>
                  {actionError?.id === j.id && <span className="text-danger text-[12px] ml-1">{actionError.msg}</span>}
                </td>
              </tr>
            ))}</tbody></table></div>
            </>)}
          </Card>
          </>)}
        </div>
      </div>

      <AnimatePresence>
        {(selected || creating) && (
          <motion.div
            key="panel"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 'auto', opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="shrink-0 overflow-hidden h-full"
          >
            <JobDetailPanel
              key={selected?.id || 'new'}
              job={selected || undefined}
              agents={agents}
              defaultAgent={defaultAgent}
              onClose={() => { setSelected(null); setCreating(false) }}
              onSaved={() => { load(); setSelected(null); setCreating(false) }}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function JobDetailPanel({ job, agents, defaultAgent, onClose, onSaved }: {
  job?: CronJob; agents: KiroClawAgent[]; defaultAgent: string; onClose: () => void; onSaved: () => void
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [panelError, setPanelError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [width, setWidth] = useState(380)
  const [, setDragging] = useState(false)
  const [detailTab, setDetailTab] = useState<'details' | 'logs'>('details')
  useEffect(() => { setDetailTab('details') }, [job?.id])
  const panelRef = useRef<HTMLDivElement>(null)
  const submitRef = useRef<(() => void) | null>(null)
  const moveRef = useRef<((ev: MouseEvent) => void) | null>(null)
  const upRef = useRef<(() => void) | null>(null)
  const widthRef = useRef(width)
  widthRef.current = width

  useEffect(() => {
    return () => {
      if (moveRef.current) document.removeEventListener('mousemove', moveRef.current)
      if (upRef.current) document.removeEventListener('mouseup', upRef.current)
    }
  }, [])

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setDragging(true)
    const startX = e.clientX; const startW = widthRef.current
    const onMove = (ev: MouseEvent) => {
      setWidth(Math.max(300, Math.min(startW + (startX - ev.clientX), window.innerWidth * 0.6)))
    }
    const onUp = () => {
      setDragging(false)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      moveRef.current = null; upRef.current = null
    }
    moveRef.current = onMove; upRef.current = onUp
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  return (
    <div ref={panelRef} className="shrink-0 border-l border-border bg-bg flex flex-col h-full overflow-hidden relative" style={{ width, minWidth: 300 }}>
      <div className="absolute left-[-2px] top-0 bottom-0 w-[5px] cursor-col-resize z-20 group/drag flex items-center justify-center" onMouseDown={onDragStart}>
        <div className="w-[2px] h-full bg-transparent group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-colors duration-200" />
      </div>
      <div className="flex items-center justify-between px-5 py-4 border-b border-border">
        <span className="text-base font-semibold text-text-strong truncate">{job ? job.name : 'New Job'}</span>
        <Btn aria-label="Close" onClick={onClose}>
          <svg className="w-4 h-4 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </Btn>
      </div>
      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4">
        {job && (
          <div className="flex items-center justify-between">
            <SegmentedControl
              segments={[
                { key: 'details' as const, label: 'Details' },
                { key: 'logs' as const, label: 'Logs' },
              ]}
              value={detailTab}
              onChange={setDetailTab}
              layoutId="panel-tab"
            />
            <div className="flex gap-2">
              <Btn onClick={async () => { try { await api.toggleCron(job.id, !job.enabled); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : 'Failed') } }}>{job.enabled ? 'Pause' : 'Resume'}</Btn>
              <SendBtn onClick={async () => { try { await api.runCron(job.id); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : 'Failed') } }}>Run Now</SendBtn>
            </div>
          </div>
        )}
        {!job && (
          <div className="flex items-center justify-between">
            <Badge variant="ok">New</Badge>
          </div>
        )}
        {detailTab === 'logs' && job ? (
          <JobLogsView jobId={job.id} isRunning={job.is_running} runningSince={job.running_since} />
        ) : (
          <>
            <JobForm job={job} agents={agents as any} defaultAgent={defaultAgent} onSaved={onSaved} layout="vertical" externalSubmit submitRef={submitRef} onSavingChange={setSaving} />
            {panelError && <div className="text-danger text-[13px]">{panelError}</div>}
            {job?.script && (job.last_result || job.last_error) && (
              <div className="flex flex-col gap-1.5">
                <label className="text-[12px] text-muted font-medium">{job.last_error ? 'Last Error' : 'Last Output'}</label>
                <pre className={`text-[12px] font-mono whitespace-pre-wrap break-words rounded border px-2.5 py-2 max-h-[200px] overflow-y-auto ${job.last_error ? 'bg-danger/5 border-danger/20 text-danger' : 'bg-bg-elevated border-border text-text'}`}>{job.last_error || job.last_result}</pre>
              </div>
            )}
            {job?.last_run_ts && (
              <div className="flex flex-col gap-1.5">
                <label className="text-[12px] text-muted font-medium">Last Run</label>
                <span className="text-sm text-text">{new Date(job.last_run_ts * 1000).toLocaleString()}</span>
              </div>
            )}
          </>
        )}
      </div>
      {/* Fixed footer */}
      <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-between">
        {job ? (
          <Btn danger onClick={() => setConfirmDelete(true)}>
            <span className="flex items-center gap-1.5">
              <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              Delete
            </span>
          </Btn>
        ) : <div />}
        <SendBtn onClick={() => submitRef.current?.()} disabled={saving}>
          <SaveCreateLabel isEdit={!!job} saving={saving} />
        </SendBtn>
      </div>
      {confirmDelete && job && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setConfirmDelete(false)}>
          <div className="bg-bg-elevated rounded-xl border border-border p-6 w-[360px] max-w-[90vw] shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-text mb-2">Delete &quot;{job.name}&quot;?</h3>
            <p className="text-sm text-muted mb-4">This will permanently remove the scheduled job. This action cannot be undone.</p>
            <div className="flex gap-2 justify-end">
              <Btn onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn danger disabled={deleting} onClick={async () => { try { setDeleteError(null); setDeleting(true); await api.deleteCron(job.id); onSaved() } catch (e: unknown) { setDeleteError(e instanceof Error ? e.message : 'Delete failed') } finally { setDeleting(false) } }}>{deleting ? 'Deleting...' : 'Delete'}</Btn>
            </div>
            {deleteError && <p className="text-danger text-[12px] mt-2">{deleteError}</p>}
          </div>
        </div>
      )}
    </div>
  )
}
