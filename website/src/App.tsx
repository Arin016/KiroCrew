import { useEffect, useState, useCallback, useRef, useMemo, createContext, type ReactNode } from 'react'
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch, store } from './store'
import { fetchSlots, sseStatus, setUpdateProgress, setEnabledAppIds, changeApprovalMode } from './store/dashboardSlice'
// Side-effect: registers every built-in surface in the registry. MUST run
// before `getBuiltinSurfaces()` is invoked below to compute `NAV_ITEMS`.
import './surfaces/builtins'
import { getBuiltinSurfaces, getBuiltinSurface, selectSurfaceBadgeCount, selectAllSurfacesAttention } from './surfaces/registry'
import { createSlot, appendMessage, setSlotRunning } from './store/chatSlice'
import { fetchNotifications } from './store/notificationsSlice'
import { useWebSocket } from './hooks/useWebSocket'
import { useTheme } from './hooks/useTheme'
import { useBranding } from './hooks/useBranding'
import { useRumPageView } from './hooks/useRumPageView'
import { useIsMobile } from './hooks/useIsMobile'
import { useNativeNotification } from './hooks/useNativeNotification'
import { useNotificationSound } from './hooks/useNotificationSound'
import { recordSessionStart } from './rum'
import { ZoomProvider } from './hooks/ZoomProvider'
import { api } from './api/client'
import { Rocket, Menu, Users, BookOpen, MessageSquareDot, Settings, Code, RefreshCw, Palette, Package, Loader2, Sun, Moon, Monitor, Download, Hammer, XCircle, Check, AlertTriangle, CheckCircle, Sparkles, X, Inbox, Gamepad2, KanbanSquare, Activity, TerminalSquare, ClipboardCheck, Keyboard, Brain } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ChatPage from './pages/ChatPage'
import OrchestratedChatPage from './pages/OrchestratedChatPage'
import ErrorBoundary from './components/ErrorBoundary'
import MarkdownRenderer, { Lightbox } from './components/MarkdownRenderer'
import NotificationsPage from './pages/NotificationsPage'
import KiroClawAgentsPage from './pages/KiroClawAgentsPage'
import ProjectsPage from './pages/ProjectsPage'
import LogsPage from './pages/LogsPage'
import HooksPage from './pages/HooksPage'
import CapabilitiesPage from './pages/CapabilitiesPage'
import KnowledgePage from './pages/KnowledgePage'
import ArtifactsPage from './pages/ArtifactsPage'
import ArtifactDetailPage from './pages/ArtifactDetailPage'
import SettingsPage from './pages/SettingsPage'
import EmbedSettingsPage from './pages/EmbedSettingsPage'
import KiroClawNavBridge from './components/KiroClawNavBridge'
import EmbedTabStrip from './components/EmbedTabStrip'
import DeveloperPage from './pages/DeveloperPage'
import SchedulePage from './pages/SchedulePage'
import CliPanel from './components/CliPanel'
import { toggleCliPanel } from './store/terminalSlice'
import { setTerminalEnabledFlag } from './utils/terminalRegistry'
import AppsPage from './pages/AppsPage'
import AppPage from './pages/AppPage'
import AppDetailPage from './pages/AppDetailPage'
import MigrationPage from './pages/MigrationPage'
import MigrationCheck from './components/MigrationCheck'
import BuiltinAppRoute from './apps/BuiltinAppRoute'
import { FEATURE_REQUEST_PROMPT } from './prompts/featureRequest'
import { useKeyboardShortcuts } from './hooks/useKeyboardShortcuts'
import { useAgents } from './hooks/useAgents'
import ShortcutsModal from './components/ShortcutsModal'

type LogSubscribeFn = (cb: ((data: { level: string; msg: string }) => void) | null) => void
export const WsContext = createContext<{ subscribeLogs: LogSubscribeFn; subscribeSubagents: (s: boolean) => void }>({ subscribeLogs: () => {}, subscribeSubagents: () => {} })

/**
 * Built-in nav items. Sourced from the surface registry (see
 * `src/surfaces/builtins.tsx`) so each item is registered exactly once and
 * its badge wiring lives next to its registration. Adding a new built-in
 * destination is a single registry entry — no code change needed here.
 *
 * Shape and order are preserved for back-compat with the rest of `App.tsx`
 * (group filtering, sortedAppGroup merge with dynamic apps, settings lookup).
 */
const NAV_ITEMS = getBuiltinSurfaces().map(s => ({
  path: s.route,
  id: s.navId,
  label: s.label,
  group: s.group,
  icon: s.icon,
}))

/** Usage color class: green (<70%), yellow (70-90%), red (>90%). */
export function metricColor(pct: number): string {
  return pct > 0.9 ? 'text-danger' : pct > 0.7 ? 'text-warn' : 'text-accent'
}
export const memColorClass = metricColor

// Icon mapping for builtin apps (manifest icon name → React element)
const BUILTIN_ICONS: Record<string, React.ReactElement> = {
  Users: <Users size={16} />,
  Inbox: <Inbox size={16} />,
  Gamepad2: <Gamepad2 size={16} />,
  MessageSquareDot: <MessageSquareDot size={16} />,
  KanbanSquare: <KanbanSquare size={16} />,
  ClipboardCheck: <ClipboardCheck size={16} />,
  BookOpen: <BookOpen size={16} />,
  Brain: <Brain size={16} />,
}

const UPDATE_STEPS: Record<string, { icon: ReactNode; label: string }> = {
  pulling:    { icon: <Download className="lucide-inline" />, label: 'Pulling latest changes' },
  syncing:    { icon: <RefreshCw className="lucide-inline" />, label: 'Syncing workspace' },
  building:   { icon: <Hammer className="lucide-inline" />, label: 'Rebuilding package' },
  installing: { icon: <Package className="lucide-inline" />, label: 'Installing packages' },
  restarting: { icon: <Rocket className="lucide-inline" />, label: 'Restarting server' },
  failed:     { icon: <XCircle className="lucide-inline" />, label: 'Update failed' },
}

const STEP_ORDER = ['pulling', 'syncing', 'building', 'installing', 'restarting']
const STUCK_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes

function UpdateOverlay({ onCancel }: { onCancel: () => void }) {
  const progress = useAppSelector(s => s.dashboard.updateProgress)
  const dispatch = useAppDispatch()
  const step = progress?.step || ''
  const detail = progress?.detail || ''
  const info = UPDATE_STEPS[step]
  const currentIdx = STEP_ORDER.indexOf(step)
  const isFailed = step === 'failed'
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef(Date.now())

  // Track elapsed time for stuck detection
  useEffect(() => {
    startRef.current = Date.now()
    const timer = setInterval(() => setElapsed(Date.now() - startRef.current), 1000)
    return () => clearInterval(timer)
  }, [])

  // Reset timer when step changes (progress is being made)
  const stepRef = useRef(step)
  useEffect(() => {
    if (step !== stepRef.current) {
      startRef.current = Date.now()
      setElapsed(0)
      stepRef.current = step
    }
  }, [step])

  const isStuck = elapsed > STUCK_THRESHOLD_MS && !isFailed
  const elapsedSec = Math.floor(elapsed / 1000)
  const elapsedStr = elapsedSec >= 60 ? `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s` : `${elapsedSec}s`

  const handleCancel = useCallback(async () => {
    try { await api.cancelUpdate() } catch { /* ignore */ }
    dispatch(setUpdateProgress(null))
    onCancel()
  }, [dispatch, onCancel])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise">
      <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
        <div className="text-4xl mb-4 animate-pulse">{info?.icon || <RefreshCw className="lucide-inline" />}</div>
        <div className="text-lg font-bold text-text-strong mb-2">Updating KiroClaw…</div>
        <div className="text-sm text-muted mb-5">{detail || 'Starting update…'}</div>
        {/* Step progress */}
        <div className="flex flex-col gap-2 text-left mb-5">
          {STEP_ORDER.map((s, i) => {
            const si = UPDATE_STEPS[s]
            const done = currentIdx > i
            const active = currentIdx === i && !isFailed
            return (
              <div key={s} className={`flex items-center gap-2.5 text-[13px] transition-colors ${done ? 'text-ok' : active ? 'text-accent font-medium' : 'text-muted/40'}`}>
                <span className="w-5 text-center">{done ? <Check className="lucide-inline" /> : active ? si.icon : '○'}</span>
                <span>{si.label}</span>
                {active && <span className="ml-auto text-[11px] text-muted animate-pulse">{elapsedStr}</span>}
              </div>
            )
          })}
        </div>
        {isFailed ? (
          <div className="flex flex-col gap-3 items-center">
            <div className="text-sm text-danger">{detail || 'Check logs for details.'}</div>
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors" onClick={handleCancel}>
              Dismiss
            </button>
          </div>
        ) : isStuck ? (
          <div className="flex flex-col gap-3 items-center">
            <div className="text-sm text-warn">This step seems to be taking longer than expected.</div>
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-danger/10 border border-danger/30 text-danger hover:bg-danger/20 transition-colors" onClick={handleCancel}>
              Cancel Update
            </button>
          </div>
        ) : (
          <div className="text-[13px] text-muted">Page will reconnect when ready…</div>
        )}
      </div>
    </div>
  )
}

function BadgeIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0) return null
  const ariaLabel = `${count} ${label}`
  return collapsed
    ? <span className="absolute top-1 right-1 w-2 h-2 bg-danger rounded-full z-10" role="status" aria-label={ariaLabel} />
    : <span className="absolute right-2 top-1/2 -translate-y-1/2 bg-danger text-danger-fg text-[12px] font-bold px-1 py-[2px] rounded-full min-w-[18px] text-center inline-block leading-[12px]" aria-label={ariaLabel}>{count}</span>
}

/**
 * Badge slot for a nav item. Resolves the count from the surface registry
 * (built-in surfaces) and falls back to the `mc:app:badge`-driven `appBadges`
 * map (dynamic apps + bridges from non-Redux sources like global approvals)
 * when the surface itself doesn't declare a badge source. This preserves the
 * prior two-pipeline behavior without leaving per-id branches in the
 * renderer.
 */
function NavBadge({ navId, collapsed, appBadges }: { navId: string; collapsed: boolean; appBadges: Record<string, number> }) {
  const surface = getBuiltinSurface(navId)
  // selectSurfaceBadgeCount caches per-navId so this stays referentially
  // stable across renders inside a `.map()`.
  const builtinCount = useAppSelector(selectSurfaceBadgeCount(navId))
  // Dynamic-app badges live outside Redux (set via a window event or a
  // direct setAppBadges sync). Consult them whenever the surface itself
  // doesn't own a badge source — including stub surfaces that only exist to
  // declare nav metadata. Surfaces with their own badge source (slotMode or
  // unreadSelector) skip the fallback to avoid double-counting.
  const surfaceHasBadgeSource = surface !== undefined && (surface.unreadSelector !== undefined || surface.slotMode !== undefined)
  const appName = navId.startsWith('app-') ? navId.slice(4) : navId
  const dynamicCount = surfaceHasBadgeSource ? 0 : (appBadges[appName] || 0)
  const builtinLabel = surface?.badgeLabel ?? 'updates'
  return (
    <>
      <BadgeIndicator count={builtinCount} collapsed={collapsed} label={builtinLabel} />
      <BadgeIndicator count={dynamicCount} collapsed={collapsed} label={builtinLabel} />
    </>
  )
}

function NavItem({ path, label, icon, active, collapsed, badge, onClickOverride, onClick }: {
  path: string; label: string; icon: React.ReactNode; active: boolean; collapsed: boolean; badge?: React.ReactNode; onClickOverride?: () => void; onClick?: () => void
}) {
  const navigate = useNavigate()
  const iconEl = <span className={`w-4 h-4 flex items-center justify-center shrink-0 transition-opacity ${active ? 'opacity-100 text-accent' : 'opacity-70'}`}>{icon}</span>
  return (
    <motion.div layout="position"
      whileHover={collapsed ? undefined : { scale: 1.02 }}
      whileTap={{ scale: 0.97 }}
      transition={{ duration: 0.15 }}
      className={`group/nav relative flex items-center rounded-md cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 py-2 pl-3 pr-3 transition-colors duration-200 ${collapsed ? '' : 'overflow-hidden'} ${active ? 'text-text-strong bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
      onClick={() => { onClick?.(); (onClickOverride || (() => navigate(path)))() }}
    >
      {badge}
      {iconEl}
      {!collapsed && <span className="whitespace-nowrap overflow-hidden">{label}</span>}
      {collapsed && (
        <div className="absolute left-0 top-0 bottom-0 flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text opacity-0 pointer-events-none group-hover/nav:opacity-100 group-hover/nav:pointer-events-auto transition-opacity duration-150 z-50 whitespace-nowrap">
          <span className={`w-4 h-4 flex items-center justify-center shrink-0 ${active ? 'text-accent' : ''}`}>{icon}</span>
          {label}
        </div>
      )}
    </motion.div>
  )
}

function TasksRedirect() { const { search } = useLocation(); return <Navigate to={'/projects' + search} replace /> }
function ChatRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + search} replace /> }

export default function App() {
  const location = useLocation()
  const isEmbed = location.pathname.startsWith('/embed/')
  const dispatch = useAppDispatch()
  const { connected, updateProgress } = useAppSelector(s => s.dashboard)
  const updateAvailable = useAppSelector(s => s.dashboard.status?.update_available)
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'
  // Sum across every registered built-in surface — Chat (slot-based),
  // Autopilot (slot-based), Notifications (notifications slice), Secretary
  // (attention slice), etc. App badges (dynamic, via `mc:app:badge` and the
  // global-approvals query below) are added below since they live outside
  // the Redux store and outside the registry.
  const builtinAttention = useAppSelector(selectAllSurfacesAttention)
  // Global approvals (project task-gates) — sourced from React Query, not
  // Redux, so it can't go through `selectAllSurfacesAttention` directly.
  // Routed through `appBadges` (the existing dynamic-app channel) so the
  // Projects nav item picks it up via `NavBadge`'s app-badge fallback path.
  const { data: pendingApprovals = [] } = useQuery({
    queryKey: ['global-approvals'],
    queryFn: () => api.approvals(),
    staleTime: 0,
    refetchInterval: 30_000,
  })
  const approvalCount = pendingApprovals.filter((a: { id?: string }) => a.id?.startsWith('task-gate-')).length
  const terminalOpen = useAppSelector(s => s.terminal.open)
  const terminalPosition = useAppSelector(s => s.terminal.position)
  const { data: terminalConfig } = useQuery({
    queryKey: ['terminal-enabled'],
    queryFn: async () => {
      const r = await fetch('/api/terminal/sessions')
      if (!r.ok) return { enabled: false }
      return r.json()
    },
    staleTime: 60_000,
  })
  const terminalEnabled = terminalConfig?.enabled === true
  useEffect(() => { setTerminalEnabledFlag(terminalEnabled) }, [terminalEnabled])
  const navigate = useNavigate()
  const { colorTheme, setColorTheme, allThemes, preference: modePref, cycle: cycleMode, setTheme: setModePref } = useTheme()
  const [showOnboarding, setShowOnboarding] = useState(() => !localStorage.getItem('mc-onboarded'))
  const { botName: _botName, avatar: _avatar } = useBranding()
  const isLumon = colorTheme === 'lumon'
  const botName = isLumon ? 'LumonClaw' : _botName
  const avatar = isLumon ? '/static/lumon-logo.png' : _avatar
  useRumPageView()
  useNotificationSound()
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('mc-nav') === '1')
  const isMobile = useIsMobile()
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  // Dynamic app nav items — all apps (builtin + installed) with UI pages
  const [appNavItems, setAppNavItems] = useState<Array<{ path: string; id: string; label: string; group: string; icon: React.ReactElement }>>([])
  const [appNavOrder, setAppNavOrder] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem('mc-app-nav-order') || '[]') } catch { return [] } })
  const appDragRef = useRef<string | null>(null)
  const sortedAppGroup = useMemo(() => {
    const items = [...NAV_ITEMS.filter(n => n.group === 'Apps'), ...appNavItems]
    if (appNavOrder.length === 0) return items
    const orderMap = new Map(appNavOrder.map((id, i) => [id, i]))
    return items.sort((a, b) => (orderMap.get(a.id) ?? 999) - (orderMap.get(b.id) ?? 999))
  }, [appNavItems, appNavOrder])
  const handleAppDrop = useCallback((targetId: string) => {
    const srcId = appDragRef.current
    if (!srcId || srcId === targetId) return
    const ids = sortedAppGroup.map(n => n.id)
    const srcIdx = ids.indexOf(srcId)
    const tgtIdx = ids.indexOf(targetId)
    if (srcIdx < 0 || tgtIdx < 0) return
    ids.splice(srcIdx, 1)
    ids.splice(tgtIdx, 0, srcId)
    setAppNavOrder(ids)
    localStorage.setItem('mc-app-nav-order', JSON.stringify(ids))
  }, [sortedAppGroup])
  const refreshAppNav = useCallback(() => {
    api.listApps()
      .then((apps: any[]) => {
        const items = apps
          .filter(a => a.enabled && a.manifest?.ui?.pages?.length > 0)
          .flatMap(a => {
            const page = a.manifest.ui.pages[0]
            const isBuiltin = a.origin === 'builtin'
            const isOrphaned = !!a.orphaned
            // Orphaned apps route to migration page; builtin apps use native route; installed apps use /apps/{name}
            const path = isOrphaned
              ? `/apps/migrate/${a.name}`
              : isBuiltin ? page.route : `/apps/${a.name}`
            const iconName = page.icon || ''
            const baseIcon = isBuiltin && BUILTIN_ICONS[iconName]
              ? BUILTIN_ICONS[iconName]
              : page.iconUrl
                ? <img src={'/apps/' + a.name + '/ui/' + page.iconUrl} alt="" className="w-4 h-4 rounded-sm object-contain" />
                : <Package size={16} />
            // Orphaned apps get a warn-colored icon to signal migration needed
            const icon = isOrphaned
              ? <span className="text-warn">{baseIcon}</span>
              : baseIcon
            return [{
              path,
              id: isBuiltin ? a.name : `app-${a.name}`,
              label: page.label || a.displayName || a.name,
              group: 'Apps',
              icon,
            }]
          })
        setAppNavItems(items)
        dispatch(setEnabledAppIds(items.map(i => i.id)))
      })
      .catch(() => {})
  }, [dispatch])
  useEffect(() => { refreshAppNav() }, [refreshAppNav])
  useEffect(() => {
    const handler = () => refreshAppNav()
    window.addEventListener('mc:apps-changed', handler)
    return () => window.removeEventListener('mc:apps-changed', handler)
  }, [refreshAppNav])

  // App badge counts — apps call useNavBadge() to push counts
  const [appBadges, setAppBadges] = useState<Record<string, number>>({})
  useEffect(() => {
    const handler = (e: Event) => {
      const { appName, count } = (e as CustomEvent).detail || {}
      if (appName) setAppBadges(prev => ({ ...prev, [appName]: count || 0 }))
    }
    window.addEventListener('mc:app:badge', handler)
    return () => window.removeEventListener('mc:app:badge', handler)
  }, [])
  // Surface the global-approvals count on the Projects nav item via the same
  // `appBadges` channel external apps use. The `projects` surface declares no
  // slotMode/unreadSelector, so `NavBadge` falls back to `appBadges['projects']`.
  useEffect(() => {
    setAppBadges(prev => prev.projects === approvalCount ? prev : { ...prev, projects: approvalCount })
  }, [approvalCount])

  const [updating, setUpdating] = useState(false)
  const [showUpdateModal, setShowUpdateModal] = useState(false)
  const [changes, setChanges] = useState('')
  const [checking, setChecking] = useState(false)
  const [showChangelog, setShowChangelog] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState(true)
  const [fullChangelog, setFullChangelog] = useState('')
  const [showFull, setShowFull] = useState(false)
  const [devMode, setDevMode] = useState(() => localStorage.getItem('mc-dev-mode') === '1')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const toggleShortcutsModal = useCallback(() => setShortcutsOpen(p => !p), [])
  const newChatMutation = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onSuccess: () => {
      navigate('/chat')
      requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus())
    },
  })
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger)
  useKeyboardShortcuts({ onToggleShortcutsModal: toggleShortcutsModal, onNewChat: () => newChatMutation.mutate(), disabled: shortcutsOpen,
    onCycleAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentAgent = currentSlot?.agent || defaultAgent
      const idx = installedAgents.findIndex((a: { name: string }) => a.name === currentAgent)
      const nextIdx = (idx + 1) % installedAgents.length
      api.chatSlotAgent(activeSlot, installedAgents[nextIdx].name)
    },
    onCycleApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const modes = ['normal', 'trust_reads', 'trust', 'yolo']
      const current = state.dashboard.approvalMode || 'normal'
      const idx = modes.indexOf(current)
      const next = modes[(idx + 1) % modes.length]
      store.dispatch(changeApprovalMode({ mode: next, slot: activeSlot }))
    },

    onCyclePrevAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentAgent = currentSlot?.agent || defaultAgent
      const idx = installedAgents.findIndex((a: { name: string }) => a.name === currentAgent)
      const prevIdx = (idx - 1 + installedAgents.length) % installedAgents.length
      api.chatSlotAgent(activeSlot, installedAgents[prevIdx].name)
    },

    onCyclePrevApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const modes = ['normal', 'trust_reads', 'trust', 'yolo']
      const current = state.dashboard.approvalMode || 'normal'
      const idx = modes.indexOf(current)
      const prevIdx = (idx - 1 + modes.length) % modes.length
      store.dispatch(changeApprovalMode({ mode: modes[prevIdx], slot: activeSlot }))
    },
  })
  const settingsMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!settingsOpen) return
    const onClick = (e: MouseEvent) => { if (settingsMenuRef.current && !settingsMenuRef.current.contains(e.target as Node)) setSettingsOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSettingsOpen(false) }
    const t = setTimeout(() => { document.addEventListener('click', onClick); document.addEventListener('keydown', onKey) }, 0)
    return () => { clearTimeout(t); document.removeEventListener('click', onClick); document.removeEventListener('keydown', onKey) }
  }, [settingsOpen])

  const [metricsOpen, setMetricsOpen] = useState(() => localStorage.getItem('mc-topbar-metrics') === '1')
  const { data: sysMetrics, isError: sysMetricsError, dataUpdatedAt: sysMetricsUpdatedAt } = useQuery({ queryKey: ['system-metrics'], queryFn: () => api.system().then(d => ({ memUsed: d.mem_used_gb, memTotal: d.mem_total_gb, cpuPct: d.cpu_pct, diskTotal: d.disk_total_gb, diskFree: d.disk_free_gb })), refetchInterval: metricsOpen ? 30_000 : false, enabled: metricsOpen })
  // Tick every 10s while widget is open so `sysMetricsStale` re-evaluates even when the query stops refetching (backgrounded tab, network drop).
  const [, setStaleTick] = useState(0)
  useEffect(() => {
    if (!metricsOpen) return
    const id = setInterval(() => setStaleTick(t => t + 1), 10_000)
    return () => clearInterval(id)
  }, [metricsOpen])
  // Consider metrics stale if last successful fetch was > 90s ago (3x the 30s poll interval) while the widget is open.
  const sysMetricsStale = metricsOpen && (sysMetricsError || (sysMetricsUpdatedAt > 0 && Date.now() - sysMetricsUpdatedAt > 90_000))

  // Listen for dev mode changes from Settings > Developer
  useEffect(() => {
    const handler = (e: Event) => setDevMode((e as CustomEvent).detail)
    window.addEventListener('mc-dev-mode-changed', handler)
    return () => window.removeEventListener('mc-dev-mode-changed', handler)
  }, [])

  useEffect(() => {
    dispatch(fetchSlots()); dispatch(fetchNotifications())
    // Fetch status immediately to sync YOLO state (WS status push is periodic)
    api.status().then(s => { dispatch(sseStatus(s)); recordSessionStart(s) }).catch(() => {})
  }, [dispatch])
  const { subscribeLogs, subscribeSubagents } = useWebSocket()

  // Close update modal when progress clears (simulation complete or cancelled)
  useEffect(() => {
    if (!updateProgress && (updating || showUpdateModal)) {
      setUpdating(false)
      setShowUpdateModal(false)
    }
  }, [updateProgress]) // eslint-disable-line react-hooks/exhaustive-deps

  // Show changelog on first load after version change (auto-update)
  useEffect(() => {
    if (!version || version === '—') return
    const lastSeen = localStorage.getItem('mc-last-version')
    if (lastSeen === version) return
    // First visit — no baseline to diff, just record current version
    if (!lastSeen) { localStorage.setItem('mc-last-version', version); return }
    // Version changed — show only new entries since lastSeen
    api.changelog().then(d => {
      if (!d.content) return
      const lines = d.content.split('\n')
      const filtered: string[] = []
      let include = false
      for (const line of lines) {
        if (line.startsWith('## [')) {
          const v = line.match(/## \[([^\]]+)\]/)?.[1]
          if (v && lastSeen && v === lastSeen) break
          include = true
        }
        if (include) filtered.push(line)
      }
      const text = filtered.join('\n').trim()
      if (text) { setChanges(text); setShowChangelog(true) }
    }).catch(() => {}).finally(() => localStorage.setItem('mc-last-version', version))
  }, [version]) // eslint-disable-line react-hooks/exhaustive-deps

  // Browser tab title badge — sums every built-in surface's badge (chat,
  // orchestrated, notifications, secretary, ...) plus the orthogonal
  // `mc:app:badge`-driven dynamic app counts. Secretary used to be added
  // separately; it now flows through the surface registry.
  const totalAttention = builtinAttention + Object.values(appBadges).reduce((a, b) => a + b, 0)
  useEffect(() => {
    document.title = totalAttention > 0 ? `(${totalAttention}) ${botName}` : botName
  }, [totalAttention, botName])

  // Browser push notification on new notification — see src/hooks/useNativeNotification.ts
  useNativeNotification(botName, avatar)

  const checkForUpdate = useCallback(async () => {
    setChecking(true)
    try {
      const info = await api.checkUpdate()
      if (info.auto_update !== undefined) setAutoUpdate(info.auto_update)
      setChanges(info.changes || '')
      setShowChangelog(true)
    } catch { /* ignore */ }
    setChecking(false)
  }, [])

  const [updateError, setUpdateError] = useState('')

  const handleUpdate = useCallback(async () => {
    setShowChangelog(false)
    setUpdateError('')
    setUpdating(true)
    try {
      await api.applyUpdate()
    } catch (err: any) {
      setUpdating(false)
      let msg = 'Update failed'
      try {
        const parsed = JSON.parse(err?.message || '')
        if (parsed.error) msg = parsed.error
      } catch { if (err?.message) msg = err.message }
      setUpdateError(msg)
    }
  }, [])

  const requestFeature = useCallback(async () => {
    const result = await dispatch(createSlot(undefined)).unwrap()
    const slot = result.key
    navigate('/chat')
    const msg = FEATURE_REQUEST_PROMPT
    dispatch(appendMessage({ role: 'user', content: '\u{1F4A1} I\u2019d like to request a feature!', cls: '', ts: new Date().toISOString() }))
    dispatch(setSlotRunning(true))
    try {
      await api.sendChat(msg, slot, colorTheme)
    } catch { /* WS will handle response */ }
  }, [dispatch, navigate, colorTheme])

  const toggleNav = () => {
    if (isMobile) { setMobileNavOpen(p => !p) }
    else { setNavCollapsed(prev => { const next = !prev; localStorage.setItem('mc-nav', next ? '1' : '0'); return next }) }
  }
  // Close mobile nav on route change
  useEffect(() => { if (isMobile) setMobileNavOpen(false) }, [location.pathname]) // eslint-disable-line react-hooks/exhaustive-deps
  // Reset mobile nav state when leaving mobile viewport
  useEffect(() => { if (!isMobile) setMobileNavOpen(false) }, [isMobile])
  const effectiveCollapsed = navCollapsed && !isMobile
  const closeMobileNav = isMobile ? () => setMobileNavOpen(false) : undefined
  const activePath = location.pathname
  const isChat = activePath === '/chat' || activePath.startsWith('/chat/') || activePath === '/' || activePath === '/orchestrated' || activePath.startsWith('/orchestrated/')
  const needsFixedHeight = isChat || activePath === '/settings' || activePath === '/developer' || activePath === '/capabilities'

  return (
    <ZoomProvider>
    <WsContext.Provider value={{ subscribeLogs, subscribeSubagents }}>
    {isEmbed ? (
      <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col">
        <KiroClawNavBridge />
        <EmbedTabStrip />
        <div className="flex-1 min-h-0">
          <Routes>
            <Route path="/embed/chat/:slug?" element={<ErrorBoundary><ChatPage embedded embedMode="chat" /></ErrorBoundary>} />
            <Route path="/embed/sessions" element={<ErrorBoundary><ChatPage embedded embedMode="sessions" /></ErrorBoundary>} />
            <Route path="/embed/settings" element={<ErrorBoundary><EmbedSettingsPage /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/embed/sessions" replace />} />
          </Routes>
        </div>
      </div>
    ) : (
    <div className={`relative z-[1] h-screen grid animate-rise overflow-hidden bg-bg ${isMobile ? 'grid-cols-[minmax(0,1fr)] grid-rows-[52px_minmax(0,1fr)]' : 'grid-cols-[auto_minmax(0,1fr)] grid-rows-[52px_1fr]'}`}
      style={{ gridTemplateAreas: isMobile ? '"topbar" "content"' : '"topbar topbar" "nav content"' }}>

      {/* Skip to content — visible only on focus for keyboard users */}
      <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[9999] focus:px-4 focus:py-2 focus:rounded-lg focus:bg-accent focus:text-accent-fg focus:text-sm focus:font-medium">Skip to content</a>

      {/* Topbar */}
      <header className={`topbar-glass flex justify-between items-center ${isMobile ? 'pl-2 pr-2' : 'pl-5 pr-3'} z-[45]`} style={{ gridArea: 'topbar' }}>
        <div className="flex items-center gap-3">
          {isMobile && (
            <button className="p-2 rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text" onClick={toggleNav} aria-label="Open menu">
              <Menu size={20} />
            </button>
          )}
          <div className={`flex items-center gap-2.5 opacity-100 ${isMobile ? 'w-auto' : 'w-40'} transition-all duration-300 ease-in-out`}>
            <img src={avatar} alt={botName} className={`${isLumon ? 'w-auto h-6' : 'w-7 h-7'} rounded-sm shrink-0 hover:rotate-[-8deg] hover:scale-110 transition-transform duration-300 object-contain`} style={{ filter: 'drop-shadow(0 2px 8px var(--accent-glow))' }} />
            <span className="text-sm font-bold tracking-[.08em] text-text-strong whitespace-nowrap">{botName.toUpperCase()}</span>
          </div>
        </div>
        <div className="flex items-center gap-1.5 relative">
          {!isMobile && <button className="top-bar-pill bg-transparent text-[12px]" onClick={requestFeature}>Request a Feature</button>}
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 transition-colors duration-300 ${connected ? 'bg-ok shadow-[0_0_8px_rgba(34,197,94,.4)]' : 'bg-danger'}`} />
          {!connected && <span className="text-[11px] text-danger font-medium">Offline</span>}
          {!isMobile && (() => {
            if (!metricsOpen) return <button className="top-bar-pill bg-card text-muted hover:text-text !gap-0" onClick={() => { setMetricsOpen(true); localStorage.setItem('mc-topbar-metrics', '1') }} title="System metrics" aria-label="System metrics"><Activity size={13} /><span className="w-0 overflow-hidden">{'\u200B'}</span></button>
            const m = sysMetrics; if (!m) return sysMetricsError ? (<button className="top-bar-pill bg-card text-danger flex items-center gap-1 text-[11px] cursor-pointer" title="Click to hide" onClick={() => { setMetricsOpen(false); localStorage.setItem('mc-topbar-metrics', '0') }}><Activity size={11} /> metrics unavailable</button>) : null
            const memPct = m.memTotal > 0 ? m.memUsed / m.memTotal : 0
            const dskUsed = m.diskTotal - m.diskFree
            const dskPct = m.diskTotal > 0 ? dskUsed / m.diskTotal : 0
            const memValid = m.memTotal > 0
            const dskValid = m.diskTotal > 0
            const staleTitle = sysMetricsStale ? ' (stale: fetch failing)' : ''
            return (<button className={`top-bar-pill bg-card flex items-center gap-2 text-[11px] font-mono cursor-pointer ${sysMetricsStale ? 'opacity-60 ring-1 ring-danger' : ''}`} title={sysMetricsStale ? 'Metrics are stale, latest fetch failed' : 'Click to hide'} onClick={() => { setMetricsOpen(false); localStorage.setItem('mc-topbar-metrics', '0') }}>
              <span className={metricColor(m.cpuPct / 100)} title={`CPU: ${m.cpuPct.toFixed(0)}%${staleTitle}`}>CPU {m.cpuPct.toFixed(0)}%</span>
              <span className={memValid ? metricColor(memPct) : 'text-muted'} title={memValid ? `Memory: ${m.memUsed.toFixed(1)}/${m.memTotal.toFixed(1)} GB${staleTitle}` : 'Memory: unavailable'}>MEM {memValid ? `${(memPct * 100).toFixed(0)}%` : '—'}</span>
              <span className={dskValid ? metricColor(dskPct) : 'text-muted'} title={dskValid ? `Disk: ${dskUsed.toFixed(0)}/${m.diskTotal.toFixed(0)} GB${staleTitle}` : 'Disk: unavailable'}>DSK {dskValid ? `${(dskPct * 100).toFixed(0)}%` : '—'}</span>
            </button>)
          })()}
          {terminalEnabled && <button
            className={`top-bar-pill bg-card ${terminalOpen ? 'text-accent' : ''}`}
            onClick={() => dispatch(toggleCliPanel())}
            title="Toggle terminal"
          >
            <TerminalSquare size={13} /> {!isMobile && 'Terminal'}
          </button>}
          <button className="top-bar-pill bg-card hover:text-text" onClick={() => setSettingsOpen(!settingsOpen)}>
            {updateProgress ? <><Loader2 size={12} className="animate-spin" />{!isMobile && <span className="font-mono text-[13px]">Updating…</span>}</> : updateAvailable ? <span className="font-mono text-[13px] text-accent">{isMobile ? '↑' : `v${version} ↑`}</span> : !isMobile ? <span className="font-mono text-[13px]">v{version}</span> : <Settings size={13} />}
          </button>
          {settingsOpen && (
            <div ref={settingsMenuRef} className="absolute right-0 top-full mt-1 z-50 bg-bg-elevated border border-border rounded-xl shadow-lg min-w-[200px] p-0.5 gap-0.5 flex flex-col animate-slide-up">
              <button className="w-full text-left px-3 py-2 rounded-md text-[12px] font-medium text-muted hover:text-text hover:bg-bg-hover cursor-pointer transition-colors border-none bg-transparent flex items-center gap-2" onClick={() => { setSettingsOpen(false); checkForUpdate() }}>{checking ? <><Loader2 size={13} className="animate-spin" /> Checking…</> : updateAvailable ? <><Package size={13} /> Update Available</> : <><RefreshCw size={13} /> Check for Updates</>}</button>
              <div className="px-3 py-2">
                <div className="text-[12px] font-medium text-muted flex items-center gap-1.5 mb-1.5"><Palette size={13} /> Theme</div>
                <div className="flex items-center gap-2 pl-1">
                  <select className="flex-1 min-w-0 bg-bg-hover border border-border rounded-md px-2 py-1 text-[12px] text-text cursor-pointer outline-none appearance-none" value={colorTheme} onChange={e => setColorTheme(e.target.value)}>
                    {allThemes.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                  <button className="shrink-0 w-7 h-7 rounded-md bg-transparent text-muted flex items-center justify-center cursor-pointer hover:text-text transition-all" onClick={e => { e.stopPropagation(); cycleMode() }} title={modePref}>
                    {modePref === 'dark' ? <Moon size={14} /> : modePref === 'light' ? <Sun size={14} /> : <Monitor size={14} />}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </header>

      {/* Update error modal */}
      {updateError && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label="Update error">
          <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
            <div className="text-4xl mb-4"><AlertTriangle className="lucide-inline" /></div>
            <div className="text-lg font-bold text-text-strong mb-2">Update Failed</div>
            <div className="text-sm text-danger mb-6">{updateError}</div>
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors" onClick={() => setUpdateError('')}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Changelog modal */}
      {showChangelog && !updating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label="Changelog" onClick={() => { setShowChangelog(false); setShowFull(false) }}>
          <div className={`bg-card border border-border rounded-xl p-6 w-full mx-4 shadow-xl transition-all duration-300 ${showFull ? 'max-w-2xl' : 'max-w-md'}`} onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <div className="text-sm font-bold text-text-strong"><Package className="lucide-inline" /> v{version}</div>
              <button className="text-muted text-[13px] cursor-pointer hover:text-text" onClick={() => { setShowChangelog(false); setShowFull(false) }}><X className="lucide-inline" /></button>
            </div>
            {updateAvailable ? (
              <>
                {changes ? (
                  <>
                    <div className="text-[13px] font-medium text-muted uppercase tracking-wider mb-2">What's new</div>
                    <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4">
                      <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={changes} /></div>
                    </div>
                  </>
                ) : (
                  <div className="p-3 bg-bg rounded-lg border border-border mb-4">
                    <div className="text-[13px] text-muted leading-relaxed">A newer version is available. No changelog entry was added for this release.</div>
                  </div>
                )}
                <button className="w-full py-2 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity" onClick={handleUpdate}>
                  Update Now
                </button>
              </>
            ) : (
              <div className="text-sm text-muted py-4 text-center"><CheckCircle className="lucide-inline" /> You're on the latest version</div>
            )}
            <div className="flex items-center justify-between mt-4 pt-3 border-t border-border">
              <span className="text-[13px] text-muted">Auto-update on restart</span>
              <button className={`w-9 h-5 rounded-full transition-colors cursor-pointer border-none ${autoUpdate ? 'bg-accent' : 'bg-border'}`}
                onClick={async () => { const next = !autoUpdate; setAutoUpdate(next); await api.setAutoUpdate(next) }}>
                <span className={`block w-3.5 h-3.5 rounded-full bg-white shadow transition-transform ${autoUpdate ? 'translate-x-4' : 'translate-x-0.5'}`} />
              </button>
            </div>
            <div className="mt-3 pt-3 border-t border-border">
              <button className="text-[13px] text-muted cursor-pointer hover:text-text transition-colors bg-transparent border-none p-0 font-body" onClick={async () => {
                if (!showFull) { if (!fullChangelog) { const d = await api.changelog(); setFullChangelog(d.content || '') }; setShowFull(true) } else { setShowFull(false) }
              }}>{showFull ? '▾ Hide Full Changelog' : '▸ View Full Changelog'}</button>
              {showFull && fullChangelog && (
                <div className="mt-2 p-3 bg-bg rounded-lg border border-border max-h-72 overflow-y-auto">
                  <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={fullChangelog} /></div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Updating overlay */}
      {(updating || showUpdateModal) && <UpdateOverlay onCancel={() => { setUpdating(false); setShowUpdateModal(false) }} />}

      {/* Theme onboarding for new users */}
      {showOnboarding && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise">
          <div className="bg-card border border-border rounded-xl p-6 max-w-sm w-full mx-4 shadow-xl">
            <div className="text-center mb-4">
              <div className="text-lg font-bold text-text-strong">Choose your look</div>
              <div className="text-[13px] text-muted mt-1">Pick a color theme and mode. You can change this anytime.</div>
            </div>
            <div className="flex justify-center gap-2 mb-4">
              {(['dark', 'light', 'system'] as const).map(m => (
                <button key={m} className={`px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer border transition-colors flex items-center gap-1.5 ${modePref === m ? 'bg-accent-subtle text-accent border-accent/30' : 'bg-transparent text-muted border-border hover:text-text hover:bg-bg-hover'}`} onClick={() => setModePref(m)}>{m === 'dark' ? <><Moon size={13} /> Dark</> : m === 'light' ? <><Sun size={13} /> Light</> : <><Monitor size={13} /> System</>}</button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-1 max-h-[240px] overflow-y-auto mb-4">
              {allThemes.map(t => (
                <button key={t.value} className={`text-left px-2.5 py-1.5 rounded-md text-[12px] cursor-pointer border-none transition-colors ${colorTheme === t.value ? 'bg-accent-subtle text-accent font-medium' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'}`} onClick={() => setColorTheme(t.value)}>{t.label}</button>
              ))}
            </div>
            <button className="w-full py-2 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity" onClick={() => { localStorage.setItem('mc-onboarded', '1'); setShowOnboarding(false) }}>
              Let's go
            </button>
          </div>
        </div>
      )}

      {/* Mobile backdrop */}
      <AnimatePresence>
        {isMobile && mobileNavOpen && (
          <motion.div
            key="nav-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-sm"
            onClick={() => setMobileNavOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Nav */}
      <AnimatePresence>
      {(!isMobile || mobileNavOpen) && (
      <motion.nav
        initial={isMobile ? { x: -240 } : false}
        animate={{ width: isMobile ? 220 : effectiveCollapsed ? 58 : 220, x: 0 }}
        exit={{ x: -240 }}
        transition={{ duration: 0.25, ease: [0.32, 0.72, 0, 1] }}
        className={`bg-bg-elevated border border-border rounded-xl flex flex-col scrollbar-none px-2 pb-4 m-2 shadow-sm z-50 ${isMobile ? 'fixed top-0 left-0 bottom-0' : ''} ${effectiveCollapsed ? 'overflow-visible' : 'overflow-y-auto'}`}
        style={isMobile ? { scrollbarWidth: 'none' } : { gridArea: 'nav', scrollbarWidth: 'none' }}
        aria-label="Main navigation"
      >
        {!isMobile && (
        <button className="flex items-center w-full mt-2 py-2.5 pl-3 rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors mb-1 shrink-0" onClick={toggleNav} title={effectiveCollapsed ? 'Expand sidebar' : 'Collapse sidebar'} aria-label={effectiveCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
          <Menu size={16} />
        </button>
        )}

        {(['Main', 'Platform', 'Apps'] as const).map(group => (
          <motion.div
            className="grid gap-0.5"
            key={group}
            animate={{ marginBottom: effectiveCollapsed ? 32 : 12 }}
            transition={{ duration: 0.2 }}
          >
            <AnimatePresence initial={false}>
              {!effectiveCollapsed && (
                <motion.div
                  key={`header-${group}`}
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.15 }}
                  className="overflow-hidden"
                >
                  <div className="flex items-center gap-2 px-2.5 py-1.5 text-[13px] font-medium text-muted">{group}</div>
                </motion.div>
              )}
            </AnimatePresence>
            {(group === 'Apps' ? sortedAppGroup : NAV_ITEMS.filter(n => n.group === group)).map(n => {
              const iconEl = n.icon
              const isAppsGroup = group === 'Apps'
              return (
                <div key={n.id} draggable={isAppsGroup} onDragStart={isAppsGroup ? () => { appDragRef.current = n.id } : undefined} onDragOver={isAppsGroup ? e => e.preventDefault() : undefined} onDrop={isAppsGroup ? () => handleAppDrop(n.id) : undefined}>
                <NavItem
                  path={n.path}
                  label={n.label}
                  icon={iconEl}
                  active={n.path === '/apps' ? activePath === '/apps' : (activePath === n.path || activePath.startsWith(n.path + '/'))}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={isChat && (activePath === n.path || activePath.startsWith(n.path + '/')) ? () => window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) : undefined}
                  badge={<NavBadge navId={n.id} collapsed={effectiveCollapsed} appBadges={appBadges} />}
                />
                </div>
              )
            })}
          </motion.div>
        ))}

        {/* Developer & Settings — pushed to bottom */}
        {(() => {
          const s = NAV_ITEMS.find(n => n.id === 'settings')!
          const devPath = '/developer'
          return (
            <div className="mt-auto grid gap-0.5">
              {devMode && (
                <NavItem
                  path={devPath}
                  label="Developer"
                  icon={<Code size={16} />}
                  active={activePath === devPath}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                />
              )}
              <NavItem
                path="#"
                label="Shortcuts"
                icon={<Keyboard size={16} />}
                active={false}
                collapsed={effectiveCollapsed}
                onClick={closeMobileNav}
                onClickOverride={toggleShortcutsModal}
              />
              <NavItem
                path={s.path}
                label={s.label}
                icon={s.icon}
                active={activePath === s.path}
                collapsed={effectiveCollapsed}
                onClick={closeMobileNav}
              />
            </div>
          )
        })()}

        {/* Watermark */}
        <div {...(effectiveCollapsed ? { inert: '' } : {})} className={`overflow-hidden transition-all duration-200 ${effectiveCollapsed ? 'max-h-0 opacity-0 mt-0' : 'max-h-40 opacity-100 mt-2'}`}>
              <div className="border-t pt-2 pb-2 px-1 whitespace-nowrap" style={{ borderColor: 'var(--muted)' }}>
                <div className="text-[13px] font-medium text-muted tracking-wide italic"><Sparkles className="lucide-inline" /> Vibed with passion!</div>
                <a href="https://github.com/YOUR_ORG/kiroclaw" target="_blank" rel="noopener noreferrer" className="text-[13px] text-muted/60 hover:text-accent transition-colors mt-1 inline-block">GitHub</a>
              </div>
        </div>
      </motion.nav>
      )}
      </AnimatePresence>

      {/* Content + Terminal */}
      <div className={`flex min-h-0 min-w-0 ${terminalPosition === 'right' ? 'flex-row' : 'flex-col'}`} style={{ gridArea: 'content' }}>
        <main id="main-content" tabIndex={-1} className={`flex flex-col min-h-0 min-w-0 flex-1 overflow-x-hidden ${needsFixedHeight ? 'overflow-hidden p-0' : 'overflow-y-auto'}`}>
          <MigrationCheck />
          <Routes>
            <Route path="/chat/:slug?" element={<ErrorBoundary><ChatPage /></ErrorBoundary>} />
            <Route path="/orchestrated/:slug?" element={<ErrorBoundary><OrchestratedChatPage /></ErrorBoundary>} />
            <Route path="/notifications" element={<ErrorBoundary><NotificationsPage /></ErrorBoundary>} />
            <Route path="/knowledge" element={<ErrorBoundary><KnowledgePage /></ErrorBoundary>} />
            <Route path="/overview" element={<Navigate to="/settings?tab=overview" replace />} />
            <Route path="/schedule" element={<SchedulePage />} />
            <Route path="/agents" element={<KiroClawAgentsPage />} />
            <Route path="/mc-agents" element={<Navigate to="/agents" replace />} />
            <Route path="/tasks" element={<TasksRedirect />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/hooks" element={<HooksPage />} />
            <Route path="/capabilities" element={<CapabilitiesPage />} />
            <Route path="/apps" element={<AppsPage />} />
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps/migrate/:name" element={<MigrationPage />} />
            <Route path="/apps/:name" element={<AppPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/developer" element={<DeveloperPage />} />
            <Route path="/artifacts" element={<ArtifactsPage />} />
            <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
            {/* Builtin app routes — auto-discovered from registry. React Router v6
                ranks static paths higher than parameterized ones, so /settings, /agents
                etc. still match first. Unrecognized paths fall through to /chat. */}
            <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
            <Route path="*" element={<ChatRedirect />} />
          </Routes>
        </main>
        <AnimatePresence>
          {terminalEnabled && terminalOpen && <CliPanel />}
        </AnimatePresence>
      </div>
    </div>
    )}
    </WsContext.Provider>
    {shortcutsOpen && <ShortcutsModal onClose={() => setShortcutsOpen(false)} />}
    <Lightbox />
    </ZoomProvider>
  )
}
