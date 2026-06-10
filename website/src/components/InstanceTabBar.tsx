/**
 * InstanceTabBar — a thin, full-width strip at the very top of the dashboard
 * that switches between the local dashboard and connected remote instances.
 *
 * Modeled on the Electron desktop app's native tab bar: it appears ONLY when
 * at least one remote instance is connected, so the common single-instance
 * experience is pixel-identical to before. Everything *below* this bar is the
 * switchable "window" — the Local dashboard, or a remote instance's embedded
 * dashboard (see InstancesViewport). The bar intentionally carries no product
 * brand of its own; each pane shows its own brand, so switching never doubles
 * the icon/title.
 *
 * Tabs: [Local] + one chip per connected instance, horizontally scrollable
 * when they overflow a narrow window. A right-aligned cluster reflects the
 * ACTIVE remote pane's tunnel connection state + token auto-refresh countdown
 * (host SSH expiry lives in the title bar, not duplicated here).
 */
import { useCallback } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Home, Server, Loader2 } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { setWarm, setActiveId } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'

// Proactive token refresh fires once elapsed reaches this fraction of the TTL
// (must match InstancesViewport.REFRESH_AT_ELAPSED_FRAC). Drives the countdown
// to the next auto-refresh shown in the tunnel-status cluster.
const REFRESH_AT_ELAPSED_FRAC = 0.8

/** Parse a `<int>[hm]` TTL (e.g. "20h", "30m") to seconds; 0 if unparseable. */
function ttlToSeconds(ttl: string): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

/** Compact human duration: "4h 12m", "12m", or "<1m". */
function fmtDuration(secs: number): string {
  if (secs < 60) return '<1m'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`
}

export default function InstanceTabBar() {
  const dispatch = useAppDispatch()
  const activeId = useAppSelector(s => s.instances.activeId)
  const warm = useAppSelector(s => s.instances.warm)
  const unread = useAppSelector(s => s.instances.unread)

  // Embedded instance panes are single-level: never run the instances poll or
  // render the switcher, so a remote pane can't recursively connect onward.
  const embedded = isEmbeddedPane()
  // Shared with InstancesViewport / InstancesPanel via the React Query cache.
  const instancesQuery = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances(), enabled: !embedded })
  const disabled = instancesQuery.error instanceof ApiError && instancesQuery.error.status === 403
  const instances = instancesQuery.data?.instances ?? []
  const connected = instances.filter(i => i.status.state === 'connected')

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onSuccess: (st, id) => {
      if (st.state === 'connected' && st.local_port && st.token) {
        dispatch(setWarm({ id, conn: { port: st.local_port, token: st.token } }))
        dispatch(setActiveId(id))
      }
      // On failure leave the active tab unchanged; Settings -> Instances surfaces
      // the detailed error/diagnosis.
    },
  })

  const onSelectInstance = useCallback(
    (id: string) => {
      if (warm[id]) dispatch(setActiveId(id))
      else connectMutation.mutate(id)
    },
    [warm, dispatch, connectMutation],
  )
  const onLocal = useCallback(() => dispatch(setActiveId(null)), [dispatch])

  // Single-instance experience is unchanged: no bar until a remote is connected.
  // Embedded panes never render the switcher (single-level by design).
  if (embedded || disabled || connected.length === 0) return null

  const tabCls = (active: boolean) =>
    'flex items-center gap-1.5 h-6 px-2.5 rounded-md text-[12px] font-medium whitespace-nowrap transition-colors border shrink-0 ' +
    (active
      ? 'bg-accent-subtle text-accent border-accent/40'
      : 'bg-transparent text-muted border-transparent hover:text-text hover:bg-bg-hover')

  // Right-aligned tunnel-status cluster: the ACTIVE remote pane's connection
  // state + countdown to the next token auto-refresh. On the Local tab there is
  // no active tunnel, so the cluster is hidden.
  const activeInst = activeId ? instances.find(i => i.id === activeId) : null
  let tunnelDotCls = ''
  let tunnelLabel = ''
  let tunnelTitle = ''
  if (activeInst) {
    const st = activeInst.status?.state
    if (st === 'connected') {
      tunnelDotCls = 'bg-[var(--ok)]'
      const rem = activeInst.status?.token_ttl_remaining
      const total = ttlToSeconds(activeInst.ttl)
      if (typeof rem === 'number' && total > 0) {
        const untilRefresh = rem - total * (1 - REFRESH_AT_ELAPSED_FRAC)
        tunnelLabel = untilRefresh > 0 ? `connected · refresh ${fmtDuration(untilRefresh)}` : 'connected · refreshing…'
        tunnelTitle = `Tunnel connected. Token valid ${fmtDuration(rem)}; auto-refresh ${untilRefresh > 0 ? `in ${fmtDuration(untilRefresh)}` : 'imminent'}.`
      } else {
        tunnelLabel = 'connected'
        tunnelTitle = 'Tunnel connected.'
      }
    } else if (st === 'connecting') {
      tunnelDotCls = 'bg-[var(--warn)]'
      tunnelLabel = 'connecting…'
      tunnelTitle = 'Tunnel connecting…'
    } else {
      tunnelDotCls = 'bg-[var(--danger)]'
      tunnelLabel = st === 'error' ? 'tunnel error' : (st || 'disconnected')
      tunnelTitle = activeInst.status?.error || `Tunnel ${st || 'disconnected'}.`
    }
  }

  return (
    <div
      className="topbar-glass flex items-center gap-2 h-8 px-2 border-b border-border shrink-0 z-[46]"
      role="tablist"
      aria-label="Instances"
    >
      <div className="flex items-center gap-1 flex-1 min-w-0 overflow-x-auto no-scrollbar">
        <button
          type="button"
          role="tab"
          aria-selected={activeId === null}
          className={tabCls(activeId === null)}
          onClick={onLocal}
          title="Local dashboard"
        >
          <Home size={13} /> Local
        </button>
        {connected.map(inst => {
          const isActive = activeId === inst.id
          const isConnecting = connectMutation.isPending && connectMutation.variables === inst.id
          const badge = unread[inst.id] || 0
          return (
            <button
              key={inst.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              className={tabCls(isActive)}
              onClick={() => onSelectInstance(inst.id)}
              title={`${inst.name} (${inst.ssh_host})`}
            >
              {isConnecting ? <Loader2 size={13} className="animate-spin" /> : <Server size={13} />}
              <span className="max-w-[160px] truncate">{inst.name}</span>
              {badge > 0 && (
                <span
                  aria-label={`${badge} unread`}
                  className="ml-0.5 min-w-[16px] h-4 px-1 rounded-full bg-accent text-accent-fg text-[10px] leading-4 text-center"
                >
                  {badge}
                </span>
              )}
            </button>
          )
        })}
      </div>
      {activeInst && (
        <div className="flex items-center gap-1.5 shrink-0 pl-2 pr-1" title={tunnelTitle}>
          <span className={`w-2 h-2 rounded-full ${tunnelDotCls}`} aria-hidden />
          <span className="text-[11px] text-[var(--muted)] hidden sm:inline">{tunnelLabel}</span>
        </div>
      )}
    </div>
  )
}
