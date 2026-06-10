/**
 * InstancesViewport — renders the remote instance panes inside the pane stack
 * below the top instance tab bar (see InstanceTabBar / App.tsx). Each connected
 * instance's dashboard is an absolutely-positioned, full-bleed <iframe>; the
 * active instance is shown and the rest stay warm (mounted, hidden). The whole
 * stack is hidden when the Local tab is active so the native dashboard (a
 * sibling pane) shows through — nothing is unmounted, so switching is instant.
 *
 * Load-bearing rules (carried over from the old InstancesPage):
 * - **Hide-not-unmount**: every warm instance's <iframe> stays mounted; only
 *   `display` toggles. Unmounting would reload the remote + re-run the token
 *   handshake and lose scroll/session state. This now holds across Local<->remote
 *   switches too (the stack is display:none on Local, not unmounted).
 * - **Warm-set cap** (instances.warm_set_cap): keep at most K warm; connecting
 *   beyond the cap evicts (disconnects) the least-recently-used non-active one.
 * - **Origin-validated unread relay** (§5.4): trust postMessage counts only
 *   from a known loopback tunnel origin.
 *
 * Renders nothing only when there are no warm instances at all.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { removeWarm, setUnread, setWarm } from '../store/instancesSlice'
import { resolveTunnelOrigin } from '../lib/tunnelOrigin'
import { isEmbeddedPane } from '../lib/embedded'

// Refresh the embedded token once elapsed reaches this fraction of its TTL
// (mirrors the gateway's default 80% threshold). Proactive refresh reloads the
// out-of-view iframe with a fresh token well before the gateway's TTL cap.
const REFRESH_AT_ELAPSED_FRAC = 0.8
// Don't re-mint the same instance more than once per this window — bounds the
// reactive (auth-expired) path so a persistently-rejecting remote can't spin
// a reconnect/reload storm.
const REFRESH_MIN_INTERVAL_MS = 10_000

/** Parse a ``<int>[hm]`` TTL (e.g. "20h", "30m") to seconds; 0 if unparseable. */
function ttlToSeconds(ttl: string): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

export default function InstancesViewport() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const warm = useAppSelector(s => s.instances.warm)
  const activeId = useAppSelector(s => s.instances.activeId)
  const mru = useAppSelector(s => s.instances.mru)

  // Embedded instance panes never host nested panes (single-level by design),
  // so skip the poll and render nothing — see isEmbeddedPane / InstanceTabBar.
  const embedded = isEmbeddedPane()

  // Poll so token_ttl_remaining (and connection dots) stay current; this also
  // drives the proactive token-refresh effect below.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    refetchInterval: 60_000,
    enabled: !embedded,
  })
  const warmCap = instancesQuery.data?.warm_set_cap || 5

  // Current warm map in a ref so the refresh callback (used by the long-lived
  // postMessage listener) always sees the latest ports without re-subscribing.
  const warmRef = useRef(warm)
  warmRef.current = warm
  const refreshingRef = useRef<Set<string>>(new Set())
  const lastRefreshRef = useRef<Map<string, number>>(new Map())

  // Force a fresh token mint for one instance and reload its iframe by updating
  // warm[id].token (srcFor re-derives the ?token= URL, so changing the token
  // reloads the iframe). Mirrors the gateway's mint-and-load. Concurrency- and
  // rate-guarded so the reactive path can't loop.
  const refreshToken = useCallback(
    async (id: string) => {
      if (refreshingRef.current.has(id)) return
      const last = lastRefreshRef.current.get(id) || 0
      if (Date.now() - last < REFRESH_MIN_INTERVAL_MS) return
      refreshingRef.current.add(id)
      try {
        const res = await api.refreshInstanceToken(id)
        const port = res.local_port || warmRef.current[id]?.port
        if (res.token && port) {
          dispatch(setWarm({ id, conn: { port, token: res.token } }))
        }
      } catch {
        /* transient — the next poll / auth-expired signal retries */
      } finally {
        refreshingRef.current.delete(id)
        lastRefreshRef.current.set(id, Date.now())
      }
    },
    [dispatch],
  )

  // Origin→id map for the relay listener, kept current without re-subscribing.
  const portToIdRef = useRef<Map<number, string>>(new Map())
  useEffect(() => {
    const m = new Map<number, string>()
    for (const [id, w] of Object.entries(warm)) m.set(w.port, id)
    portToIdRef.current = m
  }, [warm])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const id = resolveTunnelOrigin(e.origin, portToIdRef.current)
      if (!id) return
      const data = e.data
      if (!data || typeof data !== 'object') return
      if (data.type === 'mc-unread-slots') {
        const count = Number(data.count)
        if (!Number.isFinite(count) || count < 0) return
        dispatch(setUnread({ id, count }))
      } else if (data.type === 'mc-auth-expired') {
        // Reactive recovery: the embedded dashboard reported an expired session.
        // Force a fresh mint and reload its iframe rather than letting it show
        // the in-pane paste-token banner. No foreground guard here — the active
        // pane is exactly the one the user wants restored.
        void refreshToken(id)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [dispatch, refreshToken])

  // Proactive refresh: when an embedded token passes REFRESH_AT_ELAPSED_FRAC of
  // its TTL, re-mint and reload that iframe ahead of the cap. Skips the active
  // tab so a reload never interrupts the pane in use (the reactive path above
  // covers the active tab). Driven by the 60s instances poll.
  useEffect(() => {
    const data = instancesQuery.data
    if (!data) return
    for (const inst of data.instances) {
      const id = inst.id
      if (!warm[id] || id === activeId) continue
      if (inst.status?.state !== 'connected') continue
      const remaining = inst.status?.token_ttl_remaining
      const total = ttlToSeconds(inst.ttl)
      if (typeof remaining !== 'number' || total <= 0) continue
      if (remaining > total * (1 - REFRESH_AT_ELAPSED_FRAC)) continue
      void refreshToken(id)
    }
  }, [instancesQuery.data, warm, activeId, refreshToken])

  // K-cap eviction: disconnect the least-recently-used non-active warm instance.
  const disconnectMutation = useMutation({
    mutationFn: (id: string) => api.disconnectInstance(id),
    onSettled: (_d, _e, id) => {
      dispatch(removeWarm(id))
      void queryClient.invalidateQueries({ queryKey: ['instances'] })
    },
  })
  const evictRef = useRef<(id: string) => void>(() => {})
  evictRef.current = (id: string) => disconnectMutation.mutate(id)
  useEffect(() => {
    const ids = Object.keys(warm)
    if (ids.length <= warmCap) return
    const victim = [...mru].reverse().find(id => id !== activeId && warm[id])
    if (victim) evictRef.current(victim)
  }, [warm, warmCap, mru, activeId])

  const warmIds = useMemo(() => Object.keys(warm), [warm])
  const srcFor = useCallback(
    (id: string) => {
      const w = warm[id]
      // Use the parent dashboard's OWN hostname (not a hardcoded 127.0.0.1) so the iframe
      // is ALWAYS same-site with the parent. Otherwise SameSite=Lax auth cookies are
      // withheld on the iframe's subrequests (e.g. parent on localhost + iframe on
      // 127.0.0.1 = cross-site -> 403 storm). The hostname resolves to the same loopback
      // the SSH forward binds (127.0.0.1), since the dashboard itself is reached via it.
      return w ? `http://${window.location.hostname}:${w.port}/?token=${encodeURIComponent(w.token)}` : ''
    },
    [warm],
  )

  // Keep warm iframes mounted across Local<->remote switches (hide-not-unmount);
  // only bail when there is nothing warm at all — or when embedded (a pane never
  // hosts its own nested panes).
  if (embedded || warmIds.length === 0) return null

  const nameFor = (id: string) =>
    instancesQuery.data?.instances.find(i => i.id === id)?.name || id

  return (
    <div
      className="absolute inset-0 bg-bg"
      style={{ display: activeId === null ? 'none' : 'block', zIndex: 1 }}
    >
      {warmIds.map(id => (
        <iframe
          key={id}
          title={nameFor(id)}
          src={srcFor(id)}
          className="absolute inset-0 w-full h-full border-0"
          style={{ display: id === activeId ? 'block' : 'none' }}
        />
      ))}
    </div>
  )
}
