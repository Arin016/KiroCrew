import { useState, useRef, useEffect, memo, useMemo, useCallback, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGroup, AnimatePresence, motion } from 'framer-motion'
import { Plus, X, Pin, Monitor, EyeOff, VenetianMask, FolderPlus, Folder, ChevronRight, Clock, ArrowDownNarrowWide, Pencil, BrushCleaning, Link, Circle, MoreVertical, Tag as TagIcon, Columns3, GripVertical, Zap, Check, Link2, Copy } from 'lucide-react'
import { DndContext, closestCenter, KeyboardSensor, PointerSensor, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot, createSlot, deleteSlot, fetchHistory, resumeFromHistory, deleteHistorySession } from '../store/chatSlice'
import { sseSlotTitle, updateSlotFolder, updateSlotPin, markSlotUnread, markSlotRead } from '../store/dashboardSlice'
import { api, SEARCH_MIN_CHARS } from '../api/client'
import { computeReorderedFolders } from '../utils/reorderFolders'
import { useProvider } from '../providers'
import { SearchInput, Input, Btn } from '../components/ui'
import InfoTip from '../components/InfoTip'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { copySessionLink } from '../utils/shareUrl'
import { resolveFolderAgent } from '../utils/folderAgent'
import type { ChatFolder, ChatTag, TagColumn, TagColumnMode } from '../types'
import { decideUnreadDrain } from './unreadDrain'
import { loadChatConfig, saveChatConfig } from './chat/ChatSettings'

/** Telegram-style relative time: time today, "Yesterday hh:mm", weekday+time this week,
 *  short date this year, full date otherwise.
 *  Accepts ISO string (active slots) or Unix epoch seconds (history `modified`). */
function fmtRelativeTime(ts: string | number | undefined): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const startOf6DaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d >= startOfToday) return time
  if (d >= startOfYesterday) return `Yesterday ${time}`
  if (d >= startOf6DaysAgo) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
}

/** Sortable wrapper for a folder block — enables drag-to-reorder */
function SortableFolderBlock({ folder, renderFolderBlock }: { folder: ChatFolder; renderFolderBlock: (f: ChatFolder, depth: number) => React.ReactNode[] }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  return (
    <div ref={setNodeRef} style={style} className="group/folder relative" data-folder-sortable={folder.id}>
      <div
        className="absolute -left-3 top-0 h-9 w-3 flex items-center justify-center cursor-grab pointer-events-none group-hover/folder:pointer-events-auto opacity-0 group-hover/folder:opacity-100 hover:!opacity-100 transition-opacity z-10"
        aria-label={`Reorder ${folder.name}`}
        {...attributes}
        {...listeners}
      >
        <GripVertical size={12} className="text-muted" />
      </div>
      {renderFolderBlock(folder, 0)}
    </div>
  )
}

interface Slot {
  key: string
  title?: string
  running: boolean
  agent?: string
  workspace?: string
  created?: string
  last_ts?: string
  last_message?: string
  slack_linked?: boolean
  color_index?: number | null
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  folder_id?: string
  pinned?: boolean
  tags?: string[]
}

interface HistoryItem {
  key: string
  title?: string
  created?: string
  modified?: number  // unix epoch seconds; backend's mtime — used for segmenting + display
  agent?: string  // persisted in JSONL metadata (set on session create + agent switch)
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
}

interface AgentInfo {
  name: string
  source: string
}

/**
 * Debounced backend session-content search.  Returns `null` until the first
 * response arrives (or whenever the query drops below `SEARCH_MIN_CHARS`),
 * and keeps the previous result visible while a new query is in flight so
 * the list doesn't blank out between keystrokes.
 */
function useDebouncedSessionSearch<T>(
  query: string,
  transform: (sessions: { key: string; title?: string; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary' }[]) => T,
): T | null {
  const [result, setResult] = useState<T | null>(null)
  const token = useRef(0)
  useEffect(() => {
    const q = query.trim()
    const myToken = ++token.current
    if (q.length < SEARCH_MIN_CHARS) { setResult(null); return }
    const t = setTimeout(async () => {
      try {
        const d = await api.sessionsSearch(q)
        if (myToken !== token.current) return
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
    }, 250)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])
  return result
}

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
function dateSegment(ts: number | string | undefined): string {
  if (ts == null) return 'Older'
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return 'Older'
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const daysAgo7 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7)
  const daysAgo30 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30)
  if (d >= startOfToday) return 'Today'
  if (d >= startOfYesterday) return 'Yesterday'
  if (d >= daysAgo7) return 'Last 7 Days'
  if (d >= daysAgo30) return 'Last 30 Days'
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'long' })
  return d.toLocaleDateString([], { year: 'numeric', month: 'long' })
}

/** Animated collapsible for unknown-height content (folder bodies).
 *  Uses CSS grid `1fr`/`0fr` trick so we can animate to intrinsic height
 *  without measuring. For fixed-height panels use Framer Motion instead. */
function FolderBody({ open, children }: { open: boolean; children: React.ReactNode }) {
  return (
    <div
      aria-hidden={!open}
      // @ts-expect-error inert is a valid HTML attribute but TS types may lag
      inert={!open ? '' : undefined}
      style={{
        display: 'grid',
        gridTemplateRows: open ? '1fr' : '0fr',
        transition: 'grid-template-rows 0.15s ease-out',
      }}
    >
      <div style={{ overflow: 'hidden', visibility: open ? 'visible' : 'hidden', padding: '2px' }}>{children}</div>
    </div>
  )
}

interface ChatSidebarProps {
  slots: Slot[]
  activeSlot: string | null
  unreadSlots: string[]
  history: HistoryItem[]
  historyHasMore: boolean
  defaultAgent: string
  installedAgents: AgentInfo[]
  mode?: string
  onWidthChange?: (w: number) => void
  onDragChange?: (dragging: boolean) => void
  /** Optional callback fired when the user explicitly clicks a slot.
   *  When provided, this fires AFTER the switchSlot dispatch so consumers
   *  can react to user-driven selection (e.g. to navigate the URL). */
  onSelectSlot?: (key: string) => void
}

type SortKey = 'date-desc' | 'date-asc' | 'name-asc' | 'name-desc'
const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'date-desc', label: 'Newest' },
  { value: 'date-asc', label: 'Oldest' },
  { value: 'name-asc', label: 'A → Z' },
  { value: 'name-desc', label: 'Z → A' },
]
const SORT_LS_KEY = 'mc-session-sort'

function compareSlots(a: Slot, b: Slot, key: SortKey): number {
  if (key === 'name-asc' || key === 'name-desc') {
    const na = (a.title || a.key).toLowerCase()
    const nb = (b.title || b.key).toLowerCase()
    return key === 'name-asc' ? na.localeCompare(nb) : nb.localeCompare(na)
  }
  const ta = a.last_ts || a.created || ''
  const tb = b.last_ts || b.created || ''
  return key === 'date-desc' ? tb.localeCompare(ta) : ta.localeCompare(tb)
}

export const SIDEBAR_MIN = 180
export const SIDEBAR_MAX = 1400
const SIDEBAR_LS_KEY = 'mc-sidebar-width'

function ChatSidebar({
  slots, activeSlot, unreadSlots, history, historyHasMore,
  defaultAgent, installedAgents, mode, onWidthChange, onDragChange, onSelectSlot,
}: ChatSidebarProps) {
  const dispatch = useAppDispatch()
  const provider = useProvider()
  const queryClient = useQueryClient()
  const ime = useImeGuard()
  const isMobile = useIsMobile()

  // Sidebar width (self-managed, reported to parent)
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= SIDEBAR_MIN && n <= SIDEBAR_MAX ? n : 260
  })

  // Sidebar-only state
  const [slotFilter, setSlotFilter] = useState('')
  const [tipDismissed, setTipDismissed] = useState(() => !!localStorage.getItem('mc-sidebar-tip-dismissed'))
  const [historyFilter, setHistoryFilter] = useState('')
  const historySearchResults = useDebouncedSessionSearch(historyFilter, s => s)
  const slotSearchKeys = useDebouncedSessionSearch(
    slotFilter,
    sessions => new Set(sessions.map(s => s.key.replace(/^dashboard_/, ''))),
  )
  const [renamingSlot, setRenamingSlot] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const cancelRenameRef = useRef(false)
  const [ctxMenu, setCtxMenu] = useState<{ key: string; x: number; y: number } | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>(() => {
    const saved = localStorage.getItem(SORT_LS_KEY)
    return SORT_OPTIONS.some(o => o.value === saved) ? saved as SortKey : 'date-desc'
  })
  const [showUnreadOnly, setShowUnreadOnly] = useState<boolean>(() => localStorage.getItem('mc-session-unread-only') === '1')
  // Signal from the SSE/data-fetch layer indicating the initial slot list
  // has arrived. Used by the auto-drain effect to distinguish "data not yet
  // loaded" from "data loaded and genuinely empty".
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  // O(1) lookup set for the filter predicate (mirrors the `pinned` and
  // `slotSearchKeys` patterns elsewhere in this file).
  const unreadSlotSet = useMemo(() => new Set(unreadSlots), [unreadSlots])
  // Ref mirror of `showUnreadOnly` so the auto-drain effect can read the
  // current toggle state without depending on it. Keeps the effect from
  // re-firing on its own setState output.
  const showUnreadRef = useRef(showUnreadOnly)
  showUnreadRef.current = showUnreadOnly
  // Auto-disable the unread filter when the inbox drains, so the user doesn't
  // end up staring at an empty list. Decision logic lives in the pure helper
  // `decideUnreadDrain` so it can be unit-tested in isolation — see
  // `src/test/unreadDrain.test.ts`. The null-sentinel on `prevUnreadCount`
  // distinguishes "data not yet loaded" from "data loaded and genuinely empty"
  // so the persisted=true + loads-empty case fires on the first post-load
  // tick. See the helper's docstring for the known accepted batched-update
  // edge case.
  const prevUnreadCount = useRef<number | null>(null)
  useEffect(() => {
    // Guard the ENTIRE body on slotsLoaded: without this, the unconditional
    // `prevUnreadCount.current = unreadSlots.length` assignment below would
    // destroy the null sentinel on the pre-load effect run, breaking the
    // case-2 "loadedEmpty" branch in `decideUnreadDrain`. The helper's own
    // !slotsLoaded check stays as defense-in-depth.
    if (!slotsLoaded) return
    const action = decideUnreadDrain({
      prev: prevUnreadCount.current,
      current: unreadSlots.length,
      slotsLoaded,
      showUnreadOnly: showUnreadRef.current,
    })
    if (action === 'disable') {
      setShowUnreadOnly(false)
      localStorage.setItem('mc-session-unread-only', '0')
    }
    prevUnreadCount.current = unreadSlots.length
  }, [unreadSlots.length, slotsLoaded])
  const [historyOpen, setHistoryOpen] = useState(false)
  // History pane height (persisted). Drag handle adjusts this while open.
  const HISTORY_HEIGHT_LS_KEY = 'mc-history-height'
  const HISTORY_MIN_HEIGHT = 120
  const HISTORY_MAX_HEIGHT = 800
  const [historyHeight, setHistoryHeight] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem(HISTORY_HEIGHT_LS_KEY) || '', 10)
    return Number.isFinite(saved) && saved >= HISTORY_MIN_HEIGHT && saved <= HISTORY_MAX_HEIGHT ? saved : 240
  })
  useEffect(() => { localStorage.setItem(HISTORY_HEIGHT_LS_KEY, String(historyHeight)) }, [historyHeight])
  const [historyDragging, setHistoryDragging] = useState(false)
  const onHistoryDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = historyHeight
    setHistoryDragging(true)
    const onMove = (ev: MouseEvent) => {
      // Drag handle is ABOVE the pane, so dragging UP grows the pane.
      const next = Math.max(HISTORY_MIN_HEIGHT, Math.min(HISTORY_MAX_HEIGHT, startH - (ev.clientY - startY)))
      setHistoryHeight(next)
    }
    const onUp = () => {
      setHistoryDragging(false)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'ns-resize'
    document.body.style.userSelect = 'none'
  }, [historyHeight])
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [cleanupDays, setCleanupDays] = useState(3)
  const [cleanupExpanded, setCleanupExpanded] = useState(false)
  const [cleanupError, setCleanupError] = useState('')
  const { data: cleanupPreviewData, isLoading: cleanupPreviewLoading, isError: cleanupPreviewError } = useQuery({
    queryKey: ['cleanup-preview', cleanupDays, activeSlot],
    queryFn: () => api.cleanupSessions(cleanupDays, activeSlot || '', true),
    enabled: cleanupOpen,
    gcTime: 0,
  })
  const cleanupPreview = cleanupPreviewData?.keys ?? null
  const activeIsStale = cleanupPreviewData?.active_is_stale ?? false
  const cleanupMutation = useMutation({
    mutationFn: () => api.cleanupSessions(cleanupDays, activeSlot || ''),
    onSuccess: (res) => {
      if (res.keys?.length) {
        for (const key of res.keys) dispatch(deleteSlot(key))
        dispatch(fetchHistory(false))
      }
      if (res.failed?.length) {
        setCleanupError(`${res.failed.length} session(s) failed to archive`)
      } else {
        setCleanupOpen(false)
      }
      queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })
    },
    onError: (e) => setCleanupError(e instanceof Error ? e.message : 'Archive failed'),
  })

  // Pinned: derived from server-persisted slot.pinned
  const pinned = useMemo(() => new Set(slots.filter(s => s.pinned).map(s => s.key)), [slots])

  // Folder editing state
  const [creatingIn, setCreatingIn] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const cancelledRef = useRef(false)
  // Per-folder drag-enter counter: nested enter/leave pairs balance out so
  // the highlight only clears when the drag truly leaves the outermost wrapper.
  const folderDragCount = useRef<Record<string, number>>({})

  // Resize logic
  const sidebarDragging = useRef(false)
  const sidebarStartX = useRef(0)
  const sidebarStartW = useRef(0)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  useEffect(() => { onWidthChangeRef.current?.(sidebarWidth) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!sidebarDragging.current) return
      const newW = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, sidebarStartW.current + e.clientX - sidebarStartX.current))
      setSidebarWidth(newW)
      onWidthChangeRef.current?.(newW)
    }
    const onUp = () => {
      if (!sidebarDragging.current) return
      sidebarDragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      const w = sidebarWidthRef.current
      localStorage.setItem(SIDEBAR_LS_KEY, String(w))
      onWidthChangeRef.current?.(w)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (sidebarDragging.current) {
        sidebarDragging.current = false
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        onDragChangeRef.current?.(false)
      }
    }
  }, [])

  // Pin mutation (optimistic, server-persisted)
  const togglePinMutation = useMutation({
    mutationFn: ({ key, pinned: pin }: { key: string; pinned: boolean }) => api.setSlotPin(key, pin),
    onMutate: ({ key, pinned: pin }) => { dispatch(updateSlotPin({ key, pinned: pin })); return { key, prev: pinned.has(key) } },
    onError: (_err, _vars, ctx) => {
      if (ctx) dispatch(updateSlotPin({ key: ctx.key, pinned: ctx.prev }))
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    },
  })
  const togglePin = useCallback((key: string) => { togglePinMutation.mutate({ key, pinned: !pinned.has(key) }) }, [pinned, togglePinMutation])

  // Folders via React Query
  const { data: folders = [] } = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })

  // Tags via React Query (dynamic vocabulary, defaults seeded server-side)
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })
  const tagById = useMemo(() => {
    const m: Record<string, ChatTag> = {}
    for (const t of tags) m[t.id] = t
    return m
  }, [tags])
  // Sidebar column layout (flat list; empty = legacy single-lane UX)
  const { data: rawColumns = [] } = useQuery<TagColumn[]>({ queryKey: ['tag-columns'], queryFn: () => api.tagColumns() })
  const [tagColumnsEnabled, setTagColumnsEnabled] = useState(() => loadChatConfig().tagColumnsEnabled)
  useEffect(() => {
    const onChange = () => setTagColumnsEnabled(loadChatConfig().tagColumnsEnabled)
    window.addEventListener('mc-config-changed', onChange)
    return () => window.removeEventListener('mc-config-changed', onChange)
  }, [])
  // When feature is disabled, treat it as zero columns → sidebar falls back to legacy layout
  const columns: TagColumn[] = tagColumnsEnabled ? rawColumns : []
  const orderedColumns = useMemo(() => [...columns].sort((a, b) => a.order - b.order), [columns])
  const [tagCtxSlot, setTagCtxSlot] = useState<string | null>(null)
  const [columnEditId, setColumnEditId] = useState<string | null>(null)  // column whose popover is open
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null)
  // Anchor the popover to the edit button's bounding rect so it stays put even
  // though it renders in a portal outside the (overflow-hidden) column ancestor.
  useEffect(() => {
    if (!columnEditId) { setPopoverPos(null); return }
    const updatePos = () => {
      const btn = document.querySelector<HTMLElement>(`[data-testid="column-edit-${columnEditId}"]`)
      if (!btn) return
      const r = btn.getBoundingClientRect()
      setPopoverPos({ top: r.bottom + 4, left: r.left })
    }
    updatePos()
    window.addEventListener('resize', updatePos)
    window.addEventListener('scroll', updatePos, true)
    return () => {
      window.removeEventListener('resize', updatePos)
      window.removeEventListener('scroll', updatePos, true)
    }
  }, [columnEditId])
  // Close column-filter popover on outside click
  useEffect(() => {
    if (!columnEditId) return
    const handler = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (!t) return
      if (t.closest(`[data-column-popover="${columnEditId}"]`)) return
      if (t.closest(`[data-testid="column-edit-${columnEditId}"]`)) return
      setColumnEditId(null)
    }
    // Defer one tick so the same click that opened the popover doesn't immediately close it
    const id = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => { clearTimeout(id); document.removeEventListener('mousedown', handler) }
  }, [columnEditId])

  const setSlotTagsMutation = useMutation({
    mutationFn: ({ slot, nextTags }: { slot: string; nextTags: string[] }) => api.setSlotTags(slot, nextTags),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-slots'] }),
  })
  // Per-slot optimistic tag state. Rapid toggles from `slots` (React props)
  // would each see the same stale snapshot until the SSE round-trip lands,
  // so the last mutation would clobber earlier ones. We keep the pending
  // tag list in component state so the picker UI updates immediately and
  // each click composes onto the prior one synchronously.
  const [pendingSlotTags, setPendingSlotTags] = useState<Record<string, string[]>>({})
  const toggleSlotTag = useCallback((slotKey: string, tagId: string) => {
    setPendingSlotTags(prev => {
      const slot = slots.find(s => s.key === slotKey)
      const current = prev[slotKey] ?? slot?.tags ?? []
      const nextTags = current.includes(tagId) ? current.filter(t => t !== tagId) : [...current, tagId]
      setSlotTagsMutation.mutate({ slot: slotKey, nextTags }, {
        onSettled: () => {
          // Drop the optimistic entry once the server-confirmed slot.tags
          // arrives via SSE. Compare against the just-issued nextTags so a
          // later in-flight mutation (rapid burst) keeps its own optimistic
          // state until its own onSettled fires.
          setPendingSlotTags(p => {
            if (p[slotKey] !== nextTags) return p
            const { [slotKey]: _drop, ...rest } = p
            return rest
          })
        },
      })
      return { ...prev, [slotKey]: nextTags }
    })
  }, [slots, setSlotTagsMutation])

  const createTagMutation = useMutation({
    mutationFn: ({ name, color, status }: { name: string; color?: string; status?: boolean }) => api.createChatTag(name, color, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })
  const updateTagMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; color?: string; status?: boolean } }) => api.updateChatTag(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })
  const deleteTagMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatTag(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    },
  })
  const createColumnMutation = useMutation({
    mutationFn: (body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode }) => api.createTagColumn(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const updateColumnMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode; order?: number; include_untagged?: boolean } }) => api.updateTagColumn(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const deleteColumnMutation = useMutation({
    mutationFn: (id: string) => api.deleteTagColumn(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const reorderColumnsMutation = useMutation({
    mutationFn: (ids: string[]) => api.reorderTagColumns(ids),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const addColumnAfterMutation = useMutation({
    mutationFn: async (afterColId: string) => {
      const created = await api.createTagColumn({ name: '', tag_ids: [], mode: 'any' })
      const ids = orderedColumns.map(c => c.id)
      const idx = ids.indexOf(afterColId)
      ids.splice(idx + 1, 0, created.id)
      const uniqIds: string[] = []
      for (const id of ids) { if (!uniqIds.includes(id)) uniqIds.push(id) }
      await api.reorderTagColumns(uniqIds)
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const dropSlotMutation = useMutation({
    mutationFn: ({ slot, columnId }: { slot: string; columnId: string }) => api.dropSlotToColumn(slot, columnId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-slots'] }),
  })
  // Filter predicate for a single column
  const columnMatches = useCallback((col: TagColumn, slotTags: string[]): boolean => {
    // "include untagged" OR'd on top of any tag filter
    if (col.include_untagged && slotTags.length === 0) return true
    if (!col.tag_ids || col.tag_ids.length === 0) return true
    const set = new Set(slotTags)
    if (col.mode === 'all') return col.tag_ids.every(t => set.has(t))
    if (col.mode === 'none') return !col.tag_ids.some(t => set.has(t))
    return col.tag_ids.some(t => set.has(t))  // 'any'
  }, [])

  const slotFolders = useMemo(() => {
    const valid = new Set(folders.map(f => f.id))
    const m: Record<string, string> = {}
    for (const s of slots) { if (s.folder_id && valid.has(s.folder_id)) m[s.key] = s.folder_id }
    return m
  }, [slots, folders])

  const filteredSlots = useMemo(() =>
    slots
      .filter(s => {
        if (showUnreadOnly && !unreadSlotSet.has(s.key)) return false
        if (!slotFilter) return true
        if (slotFilter.trim().length >= SEARCH_MIN_CHARS) {
          if (slotSearchKeys) return slotSearchKeys.has(s.key)
          return ((s.title || '') + s.key + (s.agent || '')).toLowerCase().includes(slotFilter.toLowerCase())
        }
        return ((s.title || '') + s.key + (s.agent || '')).toLowerCase().includes(slotFilter.toLowerCase())
      })
      .sort((a, b) => {
        const pa = pinned.has(a.key) ? 0 : 1
        const pb = pinned.has(b.key) ? 0 : 1
        if (pa !== pb) return pa - pb
        return compareSlots(a, b, sortKey)
      }),
    [slots, slotFilter, slotSearchKeys, pinned, sortKey, showUnreadOnly, showUnreadOnly ? unreadSlotSet : null]
  )

  // Folder mutations
  const createFolderMutation = useMutation({
    mutationFn: ({ name, parentId }: { name: string; parentId?: string }) => api.createChatFolder(name.trim(), parentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const createFolder = useCallback((name: string, parentId?: string) => {
    if (!name.trim()) return
    createFolderMutation.mutate({ name, parentId })
    setCreatingIn(null); setNewName('')
  }, [createFolderMutation])
  const deleteFolderMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatFolder(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const updateFolderMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: object }) => api.updateChatFolder(id, body),
    onMutate: async ({ id, body }) => {
      await queryClient.cancelQueries({ queryKey: ['chat-folders'] })
      const prev = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])
      queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old => (old ?? []).map(f => f.id === id ? { ...f, ...body } : f))
      return { prev }
    },
    onError: (_err, _vars, ctx) => { if (ctx?.prev) queryClient.setQueryData(['chat-folders'], ctx.prev) },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const forkMutation = useMutation({
    mutationFn: (slot: string) => api.forkChatSlot(slot),
    onSuccess: (data) => {
      if (data?.ok && data.key) {
        queryClient.invalidateQueries({ queryKey: ['slots'] })
        dispatch(switchSlot(data.key))
      }
    },
  })
  const toggleCollapse = useCallback((id: string) => {
    const f = folders.find(x => x.id === id)
    if (f) updateFolderMutation.mutate({ id, body: { collapsed: !f.collapsed } })
  }, [folders, updateFolderMutation])

  // ── Folder drag-to-reorder ──
  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )
  const handleFolderDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    // Read latest from cache to avoid stale-closure ordering on rapid successive drags
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    const rootOnly = current.filter(f => !f.parent_id)
    const changes = computeReorderedFolders(rootOnly, active.id as string, over.id as string)
    if (!changes.length) return
    // Optimistic update
    queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old =>
      (old ?? []).map(f => {
        const c = changes.find(ch => ch.id === f.id)
        return c ? { ...f, order: c.order } : f
      })
    )
    // Persist
    changes.forEach(c => api.updateChatFolder(c.id, { order: c.order }))
  }, [queryClient])

  // Reveal-in-sidebar: expand parent folder(s) then scroll to the slot
  useEffect(() => {
    const handler = (e: Event) => {
      const key = (e as CustomEvent).detail as string
      if (!key) return
      const slot = slots.find(s => s.key === key)
      if (slot?.folder_id) {
        // Expand all ancestor folders
        const expand = (fid: string) => {
          const f = folders.find(x => x.id === fid)
          if (f?.collapsed) updateFolderMutation.mutate({ id: fid, body: { collapsed: false } })
          if (f?.parent_id) expand(f.parent_id)
        }
        expand(slot.folder_id)
      }
      setTimeout(() => {
        const el = document.querySelector(`[data-slot-key="${key}"]`)
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 150)
    }
    window.addEventListener('reveal-slot', handler)
    return () => window.removeEventListener('reveal-slot', handler)
  }, [slots, folders, updateFolderMutation])
  const renameCommit = useCallback((id: string, name: string) => {
    if (name.trim()) updateFolderMutation.mutate({ id, body: { name: name.trim() } })
    setEditingId(null)
  }, [updateFolderMutation])
  const assignFolderMutation = useMutation({
    mutationFn: ({ slotKey, folderId }: { slotKey: string; folderId: string | null }) => api.setSlotFolder(slotKey, folderId),
    onMutate: ({ slotKey, folderId }) => {
      const prevFolderId = slots.find(s => s.key === slotKey)?.folder_id ?? ''
      dispatch(updateSlotFolder({ key: slotKey, folderId: folderId || '' }))
      return { slotKey, prevFolderId }
    },
    onError: (_err, _vars, ctx) => { if (ctx) dispatch(updateSlotFolder({ key: ctx.slotKey, folderId: ctx.prevFolderId })) },
  })
  const assignToFolder = useCallback((slotKey: string, folderId: string | null) => { assignFolderMutation.mutate({ slotKey, folderId }) }, [assignFolderMutation])
  const createChatInFolderMutation = useMutation({
    mutationFn: (folderId: string) => {
      const agent = resolveFolderAgent(folders, folderId, defaultAgent)
      return dispatch(createSlot({ agent, mode })).unwrap()
    },
    onSuccess: (slot: any, folderId: string) => { if (slot?.key) assignToFolder(slot.key, folderId) },
    onError: (err: unknown) => console.error('Failed to create chat in folder:', err),
  })
  const createChatInFolder = useCallback((folderId: string) => { createChatInFolderMutation.mutate(folderId) }, [createChatInFolderMutation])

  // Session colors
  const { paletteColors, boost, colorMode } = useSessionPalette()

  // ── Session row (reference-style: color palette, memory_mode, rename on right-click) ──
  // Does any descendant (direct or nested) of `folderId` contain a slot from `slots`?
  function descendantMatch(fs: ChatFolder[], folderId: string, slots: Slot[], slotFolderMap: Record<string, string>): boolean {
    for (const child of fs) {
      if (child.parent_id !== folderId) continue
      if (slots.some(s => slotFolderMap[s.key] === child.id)) return true
      if (descendantMatch(fs, child.id, slots, slotFolderMap)) return true
    }
    return false
  }

  // Render a folder block scoped to a single column: only slots matching the column predicate.
  // Always render the folder header (even with 0 matches) so users can see + drop into it.
  const renderColumnFolder = (folder: ChatFolder, columnId: string, colSlotKeys: Set<string>): React.ReactNode => {
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => colSlotKeys.has(s.key) && slotFolders[s.key] === folder.id)
    const deepChildren = childFolders
    const count = childSlots.length + deepChildren.filter(cf => {
      const cfSlots = filteredSlots.filter(s => colSlotKeys.has(s.key) && slotFolders[s.key] === cf.id)
      return cfSlots.length > 0 || descendantMatch(folders, cf.id, filteredSlots.filter(s => colSlotKeys.has(s.key)), slotFolders)
    }).length
    return (
      <div key={`col-${columnId}-folder-${folder.id}`}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        className="rounded-md transition-all mb-0.5"
        onDragOver={e => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragLeave={e => { e.stopPropagation(); e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
        onDrop={e => {
          e.preventDefault(); e.stopPropagation()
          e.currentTarget.classList.remove('ring-1', 'ring-accent')
          const k = e.dataTransfer.getData('text/plain')
          if (k) assignToFolder(k, folder.id)
        }}
      >
        <div
          className="group relative flex items-center gap-2 pr-2 py-1 rounded-md cursor-pointer text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-all"
          style={{ paddingLeft: '6px' }}
          role="button"
          tabIndex={0}
          aria-expanded={!folder.collapsed}
          aria-label={`${folder.collapsed ? 'Expand' : 'Collapse'} folder ${folder.name}`}
          onClick={() => toggleCollapse(folder.id)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapse(folder.id) } }}
        >
          <span className="shrink-0 text-muted transition-transform duration-150" style={{ transform: folder.collapsed ? 'rotate(0deg)' : 'rotate(90deg)' }}>
            <ChevronRight size={12} />
          </span>
          <Folder size={11} className="shrink-0 text-muted" />
          <span className="flex-1 truncate">{folder.name}</span>
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          <span className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-0.5">
            <select className="text-[10px] text-muted bg-transparent border-none cursor-pointer outline-none max-w-[60px]" title="Default agent" value={folder.default_agent || ''} onClick={e => e.stopPropagation()} onChange={e => { e.stopPropagation(); updateFolderMutation.mutate({ id: folder.id, body: { default_agent: e.target.value } }) }}>
              <option value="">agent…</option>
              {installedAgents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
            </select>
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-sub`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title="New subfolder" aria-label="New subfolder" onClick={e => { e.stopPropagation(); setCreatingIn(folder.id); setNewName('') }}>
              <FolderPlus size={10} />
            </button>
            <button type="button" className="text-muted hover:text-danger bg-transparent border-none cursor-pointer p-[2px]" title={`Delete folder "${folder.name}"`} aria-label={`Delete folder ${folder.name}`} onClick={e => { e.stopPropagation(); if (confirm(`Delete folder "${folder.name}"? Sessions will be ungrouped.`)) deleteFolderMutation.mutate(folder.id) }}>
              <X size={10} />
            </button>
          </span>
        </div>
        <FolderBody open={!folder.collapsed}>
          <div className="border-l border-border ml-2 pl-1">
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {creatingIn === folder.id && (
              <div className="px-2 py-1">
                <Input className="w-full py-1 text-[12px]" autoFocus placeholder="Subfolder name…"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  {...ime.bindEnter<HTMLInputElement>({
                    onEnter: () => createFolder(newName, folder.id),
                    onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') },
                    onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName, folder.id); else setCreatingIn(null) },
                  })} />
              </div>
            )}
            {childSlots.map((s, i) => {
              const isActive = activeSlot === s.key
              const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
              return renderSessionRow(s, 1, showDivider)
            })}
          </div>
        </FolderBody>
      </div>
    )
  }

  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean) => {
    const agentName = s.agent || defaultAgent || ''
    const agentMeta = installedAgents.find(a => a.name === agentName)
    const isAim = agentMeta?.source === 'aim'
    const isBuiltin = agentMeta?.source === 'builtin'
    const agentColor = isAim ? 'text-[var(--aim)]' : isBuiltin ? 'text-muted' : 'text-accent'
    const isActive = activeSlot === s.key
    const ci = s.color_index != null && s.color_index >= 0 && s.color_index < paletteColors.length ? s.color_index : null
    const rowColor = ci != null ? paletteColors[ci] : null
    const boostStyle: Record<string, string> = {}
    if (rowColor && ci != null) {
      boostStyle['--session-color'] = rowColor
      if (boost.mutedColors[ci]) boostStyle['--session-muted'] = boost.mutedColors[ci]
    }
    return (
      <motion.div key={s.key} layout="position" layoutId={`slot-${s.key}`}
        data-slot-key={s.key}
        initial={{ opacity: 0, x: -12 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ layout: { type: 'spring', stiffness: 500, damping: 35 }, opacity: { duration: 0.2 }, x: { duration: 0.2 } }}>
        <div className={`session-row group relative flex items-start gap-2.5 px-4 py-2 rounded-md cursor-pointer text-sm transition-all select-none ${isActive ? 'session-active text-text-strong bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover'} ${rowColor ? 'session-colored' : ''} ${rowColor && colorMode === 'gradient' ? 'session-gradient' : ''}`}
          style={boostStyle as React.CSSProperties}
          draggable
          onDragStart={e => { e.dataTransfer.setData('text/plain', s.key); e.dataTransfer.effectAllowed = 'move' }}
          onClick={e => { if ((e.target as HTMLElement).closest?.('[data-fork]')) { forkMutation.mutate(s.key); return }; if ((e.target as HTMLElement).closest?.('[data-close]')) { if (!loadChatConfig().confirmCloseSession || confirm('Close this session?')) dispatch(deleteSlot(s.key)); return }; dispatch(switchSlot(s.key)); onSelectSlot?.(s.key) }}
          onContextMenu={e => { e.preventDefault(); setCtxMenu({ key: s.key, x: e.clientX, y: e.clientY }) }}>
          {unreadSlots.includes(s.key) && (
            <span className="absolute right-1.5 top-1/2 -translate-y-1/2 w-2 h-2 rounded-full pointer-events-none" style={{ background: 'var(--info)' }} />
          )}
          <div className="flex-1 min-w-0 overflow-hidden">
            <div className={`text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
              {pinned.has(s.key) && <span className="shrink-0" title="Pinned"><Pin size={10} className="text-accent" /></span>}
              <AnimatePresence mode="wait">
                <motion.span key={agentName || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="truncate">{agentName || '\u00A0'}</motion.span>
              </AnimatePresence>
              {s.slack_linked && <span className="text-[10px]" title="Linked to Slack"><Link size={10} /></span>}
              {s.memory_mode === 'incognito' && <span className="text-muted" title="Incognito — no memory writes"><EyeOff size={10} /></span>}
                {s.memory_mode === 'temporary' && <span className="text-aim" title="Temporary — no memory reads or writes"><VenetianMask size={10} /></span>}
              {(s.last_ts || s.created) && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{fmtRelativeTime(s.last_ts || s.created!)}</span>}
            </div>
            <div className="text-[13px] leading-snug line-clamp-2 break-words" title={s.title && s.title !== s.key ? s.title : s.key}>
              {renamingSlot === s.key ? (
                <Input className="w-full bg-transparent border border-accent rounded px-1 py-0 text-text-strong outline-none text-[13px]" autoFocus value={renameValue} onChange={e => setRenameValue(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => { (document.activeElement as HTMLInputElement)?.blur() }, onEscape: () => { cancelRenameRef.current = true; setRenamingSlot(null) }, onBlur: () => { if (!cancelRenameRef.current && renameValue.trim()) { dispatch(sseSlotTitle({ key: s.key, title: renameValue.trim() })); api.renameSlot(s.key, renameValue.trim()).catch(() => { queryClient.invalidateQueries({ queryKey: ['chat-slots'] }) }) } cancelRenameRef.current = false; setRenamingSlot(null) } })} onMouseDown={e => e.stopPropagation()} />
              ) : (s.title && s.title !== s.key ? s.title : s.key)}
            </div>
            {s.running ? <div className="mt-0.5"><span className="pulse-bar-sm" /></div> : s.last_message && <div className="text-[12px] text-muted leading-snug truncate mt-0.5">{s.last_message}</div>}
            {tagColumnsEnabled && s.tags && s.tags.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {s.tags.map(tid => {
                  const t = tagById[tid]
                  if (!t) return null
                  return (
                    <span key={tid} data-testid={`slot-tag-${t.id}`} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>
                      {t.name}
                    </span>
                  )
                })}
              </div>
            )}
          </div>
          {isMobile ? (
            <div className="absolute top-1/2 -translate-y-1/2 right-1.5 flex items-center gap-0.5">
              <span className="text-muted/50 active:text-text p-1 cursor-pointer" aria-label="More options" onMouseDown={e => { e.stopPropagation(); e.preventDefault(); const rect = (e.currentTarget as HTMLElement).getBoundingClientRect(); setCtxMenu({ key: s.key, x: rect.left, y: rect.bottom + 4 }) }}><MoreVertical size={14} /></span>
              <span data-fork="1" className="text-muted/50 active:text-text p-1 cursor-pointer" title="Duplicate" aria-label="Duplicate"><Copy size={14} /></span>
              <span data-close="1" className="text-muted/50 active:text-danger p-1 cursor-pointer"><X size={14} /></span>
            </div>
          ) : (
            <div className={`absolute top-1/2 -translate-y-1/2 right-1.5 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm ${ctxMenu?.key === s.key ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`}>
              <span className="text-muted cursor-pointer p-[4px] rounded hover:text-text hover:bg-bg-hover transition-all" title="More" aria-label="More options" onMouseDown={e => { e.stopPropagation(); e.preventDefault(); const rect = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect(); setCtxMenu({ key: s.key, x: rect.left, y: rect.bottom + 4 }) }}><MoreVertical size={12} /></span>
              <span data-fork="1" className="text-muted cursor-pointer p-[4px] rounded hover:text-accent hover:bg-bg-hover transition-all" title="Duplicate" aria-label="Duplicate"><Copy size={12} /></span>
              <span data-close="1" className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all" title="Close"><X size={12} /></span>
            </div>
          )}
        </div>
        {showDivider && <div className="mx-3 border-b border-border" />}
      </motion.div>
    )
  }

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  const renderFolderHeader = (folder: ChatFolder) => {
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => slotFolders[s.key] === folder.id)
    const count = childSlots.length + childFolders.length
    return (
      <div key={`folder-header-${folder.id}`}
        className={`group relative flex items-center gap-2 pr-2 py-2 rounded-md cursor-pointer text-sm text-muted hover:text-text hover:bg-bg-hover transition-all`}
        style={{ paddingLeft: '8px' }}
        onClick={() => toggleCollapse(folder.id)}>
        <span data-testid={`folder-collapse-${folder.id}`} className="shrink-0 text-muted transition-transform duration-150" style={{ transform: folder.collapsed ? 'rotate(0deg)' : 'rotate(90deg)' }}>
          <ChevronRight size={14} />
        </span>
        {folder.icon
          ? <span className="text-[14px] leading-none shrink-0">{folder.icon}</span>
          : <Folder size={14} className="text-muted shrink-0" />}
        {editingId === folder.id ? (
          <Input className="flex-1 py-0.5 text-[13px] min-w-0" autoFocus value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
        ) : (
          <span className="flex-1 text-[13px] font-medium text-text truncate" title="Double-click to rename" onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditName(folder.name) }}>{folder.name}</span>
        )}
        <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
        {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={`Default agent: ${folder.default_agent}`}>{folder.default_agent}</span>}
        {editingId !== folder.id && (
        <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
          <select className="text-[10px] text-muted bg-transparent border-none cursor-pointer outline-none max-w-[70px]" title="Default agent for new chats" value={folder.default_agent || ''} onClick={e => e.stopPropagation()} onChange={e => { e.stopPropagation(); updateFolderMutation.mutate({ id: folder.id, body: { default_agent: e.target.value } }) }}>
            <option value="">agent…</option>
            {installedAgents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
          <span className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all" title="Rename folder" aria-label="Rename folder" data-testid={`folder-rename-${folder.id}`} onClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditName(folder.name) }}><Pencil size={12} /></span>
          <span className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all" title="New chat in folder" aria-label="New chat in folder" onClick={e => { e.stopPropagation(); createChatInFolder(folder.id) }}><Plus size={12} /></span>
          <span className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all" title="New subfolder" aria-label="New subfolder" onClick={e => { e.stopPropagation(); setCreatingIn(folder.id); setNewName('') }}><FolderPlus size={12} /></span>
          <span className="cursor-pointer p-[4px] rounded text-muted hover:text-danger hover:bg-danger-subtle transition-all" data-testid={`folder-delete-${folder.id}`} title="Delete folder" aria-label="Delete folder" onClick={e => { e.stopPropagation(); if (confirm(`Delete "${folder.name}"?`)) deleteFolderMutation.mutate(folder.id) }}><X size={12} /></span>
        </div>
        )}
      </div>
    )
  }

  const renderFolderBlock = (folder: ChatFolder, depth: number, visited = new Set<string>()): React.ReactNode[] => {
    if (depth > 10 || visited.has(folder.id)) return []
    visited.add(folder.id)
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => slotFolders[s.key] === folder.id)
    const childNodes: React.ReactNode[] = []
    for (const cf of childFolders) childNodes.push(...renderFolderBlock(cf, depth + 1, visited))
    childSlots.forEach((s, i) => {
      const isActive = activeSlot === s.key
      const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
      const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
      childNodes.push(renderSessionRow(s, depth + 1, showDivider))
    })
    if (creatingIn === folder.id) {
      childNodes.push(
        <div key={`new-sub-${folder.id}`} className="py-1 pr-2" style={{ paddingLeft: '8px' }}>
          <Input className="w-full py-1 text-[13px]" autoFocus placeholder="Folder name…" value={newName} onChange={e => setNewName(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => createFolder(newName, folder.id), onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') }, onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName, folder.id); else setCreatingIn(null) } })} />
        </div>
      )
    }
    // Hide folders with no matching children when searching or filtering unreads
    if ((slotFilter || showUnreadOnly) && childNodes.length === 0) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    const wrapped = childNodes.length > 0 ? (
      <div key={`folder-children-${folder.id}`} className="border-l border-border mb-1 ml-3 pl-1 rounded-bl-md">
        {childNodes}
      </div>
    ) : null
    // Outer container wraps header + body so the entire folder block is a
    // single drag-drop target. Dropping anywhere inside (header, children,
    // empty space) assigns the dragged session to this folder.
    // Uses a dragEnter counter instead of contains() checks — nested child
    // folders fire enter/leave pairs that balance to zero when the drag
    // moves into a subfolder, so the parent highlight clears correctly.
    return [
      <div key={`folder-drop-${folder.id}`}
        className="rounded-md transition-all mb-0.5"
        onDragEnter={e => { e.preventDefault(); e.stopPropagation(); folderDragCount.current[folder.id] = (folderDragCount.current[folder.id] || 0) + 1; if (folderDragCount.current[folder.id] === 1) e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragOver={e => { e.preventDefault(); e.stopPropagation() }}
        onDragLeave={e => { e.stopPropagation(); folderDragCount.current[folder.id] = (folderDragCount.current[folder.id] || 0) - 1; if (folderDragCount.current[folder.id] <= 0) { folderDragCount.current[folder.id] = 0; e.currentTarget.classList.remove('ring-1', 'ring-accent') } }}
        onDrop={e => { e.preventDefault(); e.stopPropagation(); folderDragCount.current[folder.id] = 0; e.currentTarget.classList.remove('ring-1', 'ring-accent'); const k = e.dataTransfer.getData('text/plain'); if (k) assignToFolder(k, folder.id) }}>
        {renderFolderHeader(folder)}
        <FolderBody key={`folder-body-${folder.id}`} open={!folder.collapsed}>{wrapped}</FolderBody>
      </div>,
    ]
  }

  const rootFolders = useMemo(() => folders.filter(f => !f.parent_id).sort((a, b) => a.order - b.order), [folders])
  const rootFolderIds = useMemo(() => rootFolders.map(f => f.id), [rootFolders])
  const ungroupedSlots = filteredSlots.filter(s => !slotFolders[s.key])

  return (
    <div className="sidebar-inner bg-bg-elevated border border-border rounded-xl flex flex-col shrink-0 relative h-full" style={{ width: sidebarWidth }}>
      {/* Drag handle */}
      <div
        className="sidebar-resize-handle absolute top-0 -right-[2px] w-[5px] h-full cursor-col-resize z-10 group/drag flex items-center justify-center"
        onMouseDown={e => { e.preventDefault(); sidebarDragging.current = true; sidebarStartX.current = e.clientX; sidebarStartW.current = sidebarWidth; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'; onDragChange?.(true) }}
      >
        <div className="w-[2px] h-full bg-transparent group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-colors duration-200" />
      </div>

      {/* Header */}
      <div className="flex justify-between items-center px-3 h-12 mt-1">
        <span className="text-[13px] font-medium text-muted uppercase tracking-[.04em] flex items-center gap-1.5">Sessions <InfoTip text={`Each tab is an independent ${provider.labels.sessionProcess} session with its own context. Group sessions into folders via drag-drop.`} /></span>
        <div className="flex items-center gap-1.5">
          <button className="w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" onClick={() => { setCreatingIn('__root__'); setNewName('') }} title="New folder" aria-label="New folder"><FolderPlus size={14} /></button>
          <button className="w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }} title="Clean up inactive sessions" aria-label="Clean up inactive sessions"><BrushCleaning size={14} /></button>
          <button className="w-7 h-7 rounded-md bg-accent text-accent-fg border-none cursor-pointer flex items-center justify-center hover:bg-accent-hover hover:shadow-[0_0_16px_var(--accent-glow)] hover:rotate-90 hover:scale-110 active:scale-95 transition-all" onClick={() => dispatch(createSlot({ agent: defaultAgent || undefined, mode })).then(() => requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()))} title="New chat" aria-label="New chat session"><Plus size={16} /></button>
        </div>
      </div>

      {/* Clean Up dialog */}
      {cleanupOpen && (() => {
        const archivable = cleanupPreview ? cleanupPreview.map(k => slots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> Clean Up Sessions</div>
            <div className="text-muted text-[12px] mb-2">Archive sessions with no activity in the last:</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{d} day{d > 1 ? 's' : ''}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? 'Checking…'
                : cleanupPreviewError
                  ? <>Failed to load preview. <button className="text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>Retry</button></>
                  : noStale
                    ? 'No inactive sessions to archive.'
                    : cleanupPreview != null && <>
                      {archivable.length} session{archivable.length !== 1 ? 's' : ''} will be moved to history.{activeIsStale ? ' (1 skipped — currently selected)' : ''} Pinned sessions are kept.
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? 'Hide' : 'Show'} {archivable.length} session{archivable.length !== 1 ? 's' : ''} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {(s.last_ts || s.created) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(s.last_ts || s.created!)}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            <div className="flex items-center gap-2 justify-end">
              {cleanupError && <span className="text-[11px] text-danger flex-1">{cleanupError}</span>}
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>Cancel</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? 'Archiving…' : `Archive ${archivable.length} session${archivable.length !== 1 ? 's' : ''}`}</Btn>
            </div>
          </div>
        )
      })()}

      {/* Search + sort (reference layout) */}
      <div className="px-3 pt-2 pb-1 flex items-center gap-1.5">
        <div className="relative flex-1">
          <SearchInput className="w-full" placeholder="Search sessions…" value={slotFilter} onChange={e => setSlotFilter(e.target.value)} />
          {slotFilter && (
            <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setSlotFilter('')} aria-label="Clear search"><X size={13} /></button>
          )}
        </div>
        <button
          type="button"
          className={`relative w-7 h-7 rounded-md border bg-transparent flex items-center justify-center cursor-pointer transition-all ${showUnreadOnly ? 'border-[var(--info)] text-[var(--info)] shadow-[0_0_8px_rgba(59,130,246,.35)]' : 'border-border text-muted hover:border-border-strong hover:text-text'}`}
          onClick={() => {
            const nv = !showUnreadOnly
            setShowUnreadOnly(nv)
            localStorage.setItem('mc-session-unread-only', nv ? '1' : '0')
          }}
          title={showUnreadOnly ? `Showing only ${unreadSlots.length} session${unreadSlots.length === 1 ? '' : 's'} with unread activity — click to show all` : unreadSlots.length > 0 ? `Show only sessions with unread activity (${unreadSlots.length})` : 'No unread sessions — toggle filter anyway'}
          aria-label={`Toggle unread filter${unreadSlots.length > 0 ? ` (${unreadSlots.length} unread)` : ''}`}
          aria-pressed={showUnreadOnly}
        >
          <Circle
            size={10}
            {...(showUnreadOnly ? { strokeWidth: 0, fill: 'var(--info)' } : {})}
          />
          {unreadSlots.length > 0 && (
            <span
              aria-hidden="true"
              className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-[var(--info)] text-white text-[10px] font-semibold leading-[14px] text-center pointer-events-none shadow-[0_0_4px_rgba(59,130,246,.5)]"
            >
              {unreadSlots.length > 99 ? '99+' : unreadSlots.length}
            </span>
          )}
        </button>
        {/* Board/list view toggle — single control flips between flat-list mode and column-strip mode. Columns persist across toggles. */}
        <div className="relative shrink-0">
          <button
            type="button"
            data-testid="board-toggle"
            className={`w-7 h-7 rounded-md border bg-transparent cursor-pointer flex items-center justify-center transition-all ${tagColumnsEnabled && rawColumns.length > 0 ? 'border-accent text-accent' : 'border-border text-muted hover:border-border-strong hover:text-text'}`}
            onClick={() => {
              const isActive = tagColumnsEnabled && rawColumns.length > 0
              const next = !isActive
              const cfg = loadChatConfig()
              saveChatConfig({ ...cfg, tagColumnsEnabled: next })
              // First-time board mode with no columns → seed a default unfiltered column
              if (next && rawColumns.length === 0) {
                createColumnMutation.mutate({ name: '', tag_ids: [], mode: 'any' })
              }
            }}
            title={tagColumnsEnabled && rawColumns.length > 0 ? 'Switch to list view' : 'Switch to board view'}
            aria-pressed={tagColumnsEnabled && rawColumns.length > 0}
            aria-label={tagColumnsEnabled && rawColumns.length > 0 ? 'Switch to list view' : 'Switch to board view'}
          >
            <Columns3 size={13} />
          </button>
        </div>
        <div className="relative shrink-0">
          <div className="w-7 h-7 rounded-md border border-border bg-transparent text-muted flex items-center justify-center cursor-pointer hover:border-border-strong hover:text-text transition-all" aria-hidden="true">
            <ArrowDownNarrowWide size={14} />
          </div>
          <select className="absolute inset-0 opacity-0 cursor-pointer" value={sortKey} onChange={e => { const v = e.target.value as SortKey; setSortKey(v); localStorage.setItem(SORT_LS_KEY, v) }} aria-label="Sort sessions">
            {SORT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      </div>
      <LayoutGroup id="chat-slots">
        {orderedColumns.length === 0 ? (
          // Legacy single-lane layout (identical to pre-columns behavior)
          <motion.div layoutScroll className="flex-1 overflow-y-auto pl-5 pr-2 py-2 flex flex-col"
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); const k = e.dataTransfer.getData('text/plain'); if (k) assignToFolder(k, null) }}>
            {/* Root folders — drag to reorder */}
            <DndContext sensors={dndSensors} collisionDetection={closestCenter} onDragEnd={handleFolderDragEnd}>
              <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                {rootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} renderFolderBlock={renderFolderBlock} />)}
              </SortableContext>
            </DndContext>
            {creatingIn === '__root__' && (
              <div className="px-2 py-1">
                <Input className="w-full py-1 text-[13px]" autoFocus placeholder="Folder name…" value={newName} onChange={e => setNewName(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => createFolder(newName), onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') }, onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName); else setCreatingIn(null) } })} />
              </div>
            )}
            {ungroupedSlots.map((s, i) => {
              const nextIsActive = i < ungroupedSlots.length - 1 && activeSlot === ungroupedSlots[i + 1].key
              const isActive = activeSlot === s.key
              const showDivider = i < ungroupedSlots.length - 1 && !isActive && !nextIsActive
              return renderSessionRow(s, 0, showDivider)
            })}
          </motion.div>
        ) : (
          // Trello-style horizontal column strip
          <div className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              const colSlots = filteredSlots.filter(s => columnMatches(col, s.tags || []))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const isStatusLane = colTags.length === 1 && !!colTags[0].status
              return (
                <div key={col.id} data-testid={`column-${col.id}`} className="flex flex-col flex-1 min-w-[220px] bg-card border border-border rounded-md overflow-hidden"
                  onDragOver={e => {
                    const types = e.dataTransfer.types
                    // Accept column reorder on the entire column surface
                    if (types.includes('application/mc-column')) {
                      e.preventDefault()
                      return
                    }
                    // Accept session-card drop only on status lanes
                    if (isStatusLane && types.includes('text/plain')) {
                      e.preventDefault()
                      e.currentTarget.classList.add('ring-1', 'ring-accent')
                    }
                  }}
                  onDragLeave={e => { e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
                  onDrop={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    // Column reorder takes priority
                    const draggedCol = e.dataTransfer.getData('application/mc-column')
                    if (draggedCol && draggedCol !== col.id) {
                      e.preventDefault()
                      const ids = orderedColumns.map(c => c.id).filter(id => id !== draggedCol)
                      ids.splice(colIdx, 0, draggedCol)
                      reorderColumnsMutation.mutate(ids)
                      return
                    }
                    if (!isStatusLane) return
                    e.preventDefault()
                    const k = e.dataTransfer.getData('text/plain')
                    if (k) dropSlotMutation.mutate({ slot: k, columnId: col.id })
                  }}>
                  <div className="flex items-center gap-1 p-2 border-b border-border bg-bg-elevated">
                    {/* Reorder handle: drag columns to reorder */}
                    <span draggable
                      className="cursor-grab text-muted hover:text-text shrink-0"
                      onDragStart={e => { e.dataTransfer.setData('application/mc-column', col.id); e.dataTransfer.effectAllowed = 'move' }}
                      title="Drag to reorder">
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? 'Untagged' : 'All sessions')}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title="Also shows untagged sessions">+ untagged</span>}
                        </>
                      )}
                      {col.name && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                    </div>
                    <span className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title="New folder" aria-label="New folder" onClick={() => { setCreatingIn(`__col_${col.id}__`); setNewName('') }}><FolderPlus size={12} /></button>
                    <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title="Filter & manage tags" aria-label="Filter & manage tags" onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title="Add column after this one"
                      aria-label="Add column after this one"
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title="Delete column"
                      aria-label="Delete column"
                      onClick={() => { if (confirm('Delete this column?')) deleteColumnMutation.mutate(col.id) }}
                    ><X size={12} /></button>
                  </div>
                  {/* Column filter popover — portaled to <body> so the column's
                      overflow-hidden ancestor cannot clip it; viewport-anchored
                      to the edit button via popoverPos. */}
                  {columnEditId === col.id && popoverPos && createPortal(
                    <div data-column-popover={col.id}
                      className="fixed z-[9100] bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px]"
                      style={{ top: popoverPos.top, left: popoverPos.left }}
                      onClick={e => e.stopPropagation()}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">Column filter</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => setColumnEditId(null)} aria-label="Close"><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder="Column name (optional)" defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label="Match mode">
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      <label className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title="Also show sessions that have no tags at all">
                        <input
                          type="checkbox"
                          data-testid={`column-include-untagged-${col.id}`}
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        Include untagged sessions
                      </label>
                      <div className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto">
                        {[...tags].sort((a, b) => a.order - b.order).map(t => {
                          const on = col.tag_ids.includes(t.id)
                          const nextIds = on ? col.tag_ids.filter(x => x !== t.id) : [...col.tag_ids, t.id]
                          return (
                            <div key={t.id} data-testid={`tag-row-${t.id}`} className={`group/tag flex items-center gap-1.5 px-1.5 py-1 rounded transition-all ${on ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}>
                              {/* Filter toggle — the color swatch is the click target */}
                              <button type="button" role="menuitemcheckbox" aria-checked={on} aria-label={`${on ? 'Remove' : 'Include'} ${t.name} from filter`}
                                className="w-4 h-4 rounded-sm border border-border shrink-0 cursor-pointer relative"
                                style={{ background: t.color }}
                                onClick={() => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}>
                                {on && <span className="absolute inset-0 flex items-center justify-center" style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }}><Check size={10} /></span>}
                              </button>
                              {/* Inline rename */}
                              <input
                                type="text"
                                data-testid={`tag-name-${t.id}`}
                                defaultValue={t.name}
                                className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text py-0 px-0.5 rounded focus:bg-bg-elevated focus:border focus:border-accent/50"
                                onBlur={e => { const v = e.target.value.trim(); if (v && v !== t.name) updateTagMutation.mutate({ id: t.id, body: { name: v } }) }}
                                onKeyDown={e => { if (e.key === 'Enter') (e.currentTarget as HTMLInputElement).blur(); else if (e.key === 'Escape') (e.currentTarget as HTMLInputElement).blur() }}
                                onClick={e => e.stopPropagation()}
                              />
                              {/* Status lightning — filled for status tags, muted ghost for non-status on hover */}
                              <button type="button" data-testid={`tag-status-${t.id}`}
                                className={`shrink-0 cursor-pointer bg-transparent border-none p-[2px] transition-all ${t.status ? 'text-accent hover:text-accent-hover' : 'text-transparent group-hover/tag:text-muted hover:!text-text'}`}
                                title={t.status ? 'Status tag (mutually exclusive on cards) — click to make regular' : 'Make status tag'}
                                aria-pressed={!!t.status}
                                aria-label={t.status ? `Remove status flag from ${t.name}` : `Make ${t.name} a status tag`}
                                onClick={() => updateTagMutation.mutate({ id: t.id, body: { status: !t.status } })}>
                                <Zap size={11} fill={t.status ? 'currentColor' : 'none'} />
                              </button>
                              {/* Delete */}
                              <button type="button" data-testid={`tag-delete-${t.id}`}
                                className="shrink-0 cursor-pointer bg-transparent border-none p-[2px] text-transparent group-hover/tag:text-muted hover:!text-danger transition-all"
                                title={`Delete tag "${t.name}"`}
                                aria-label={`Delete tag ${t.name}`}
                                onClick={() => { if (confirm(`Delete tag "${t.name}"?`)) deleteTagMutation.mutate(t.id) }}>
                                <X size={11} />
                              </button>
                            </div>
                          )
                        })}
                      </div>
                      {/* Create new tag */}
                      <div className="mt-2 border-t border-border pt-2 flex items-center gap-1.5">
                        <span className="w-4 h-4 rounded-sm border border-dashed border-border shrink-0 flex items-center justify-center text-muted"><Plus size={10} /></span>
                        <input
                          type="text"
                          data-testid={`tag-create-${col.id}`}
                          placeholder="New tag… ↵"
                          className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text py-0 px-0.5 placeholder:text-muted/60"
                          onKeyDown={e => {
                            if (e.key === 'Enter') {
                              const el = e.currentTarget as HTMLInputElement
                              const v = el.value.trim()
                              if (!v) return
                              createTagMutation.mutate({ name: v })
                              el.value = ''
                            }
                          }}
                          onClick={e => e.stopPropagation()}
                        />
                      </div>
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>Clear filter</button>
                      </div>
                    </div>,
                    document.body
                  )}
                  <div className="flex-1 overflow-y-auto p-1.5 flex flex-col">
                    {/* No onDrop here: folder assignment only changes via folder-header drop.
                        Cross-column drops are handled by the OUTER column onDrop
                        (which only mutates status tags, keeping folder_id intact). */}
                    {(() => {
                      const colSlotKeys = new Set(colSlots.map(s => s.key))
                      // Show ALL root folders as drop targets, not only those with matching slots.
                      // Empty folders render with "0" count so users see the structure they built.
                      const relevantFolders = folders.filter(f => !f.parent_id)
                      const ungrouped = colSlots.filter(s => !slotFolders[s.key] || !folders.find(f => f.id === slotFolders[s.key]))
                      const hasAny = colSlots.length > 0 || folders.length > 0
                      return (
                        <>
                          {relevantFolders.map(f => renderColumnFolder(f, col.id, colSlotKeys))}
                          {(creatingIn === `__col_${col.id}__` || (creatingIn === '__root__' && colIdx === 0)) && (
                            <div className="px-2 py-1">
                              <Input className="w-full py-1 text-[12px]" autoFocus placeholder="Folder name…"
                                value={newName}
                                onChange={e => setNewName(e.target.value)}
                                {...ime.bindEnter<HTMLInputElement>({
                                  onEnter: () => createFolder(newName),
                                  onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') },
                                  onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName); else setCreatingIn(null) },
                                })} />
                            </div>
                          )}
                          {ungrouped.map((s, i) => {
                            const isActive = activeSlot === s.key
                            const nextIsActive = i < ungrouped.length - 1 && activeSlot === ungrouped[i + 1].key
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                            return renderSessionRow(s, 0, showDivider)
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">No sessions</div>}
                        </>
                      )
                    })()}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </LayoutGroup>

      {/* Sidebar-hide tip */}
      {!tipDismissed && <div className="sidebar-toggle-tip mx-2 mb-1 mt-1 px-3 py-2 rounded-lg bg-accent/8 border border-dashed border-accent/10 text-[12px] text-muted leading-relaxed flex items-start gap-2 animate-rise">
        <span className="flex-1">You can now toggle this sidebar<br/>Enable in <strong>Settings → Chat → Sidebar</strong>.</span>
        <Btn className="shrink-0 text-muted hover:text-text cursor-pointer bg-transparent border-none text-[14px] leading-none p-0" onClick={() => { localStorage.setItem('mc-sidebar-tip-dismissed', '1'); setTipDismissed(true) }}><X className="lucide-inline" /></Btn>
      </div>}

      {/* When expanded: doubles as the resize handle (accent on hover, drag to resize, dbl-click to collapse).
          When collapsed: just a static 1px divider between sessions and the History header. */}
      {historyOpen ? (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize history pane"
          onMouseDown={onHistoryDragStart}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* History header row — whole row is the click target; Clear-all button stops propagation. */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) }}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) } }}
        className="flex justify-between items-center px-3 py-3 cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <ChevronRight size={16} className={`shrink-0 transition-transform duration-200 ${historyOpen ? 'rotate-90' : '-rotate-90'}`} />
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">History</span>
        </span>
        {historyOpen && history.length > 0 && (
          <button
            className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
            onClick={async e => { e.stopPropagation(); if (confirm('Clear closed history sessions? Active tabs and pinned sessions will be kept.')) { await api.clearSessions(); dispatch(fetchHistory(false)) } }}
          >Clear history</button>
        )}
      </div>
      <AnimatePresence initial={false}>
        {historyOpen && (
          <motion.div
            id="history-pane"
            key="history-pane"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-2 pb-1">
              <div className="relative">
                <SearchInput className="w-full" placeholder="Search history…" value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label="Clear search"><X size={13} /></button>
                )}
              </div>
            </div>
            <div className="overflow-y-auto p-2 scroll-shadow" style={{ height: `${historyHeight}px` }}>
              {(() => {
                const filteredHistory = (historySearchResults ?? history).filter(s => {
                  if (!historyFilter) return true
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS) {
                    if (historySearchResults) return true
                    return ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                  }
                  return ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                })
                // Hide date segments when the user has an active search — results are
                // score-ranked by backend relevance, not date-ranked, so segments are
                // misleading (items within a bucket appear out of recency order).
                const showSegments = !(historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults)
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = installedAgents.find(a => a.name === agentName)
                  if (meta?.source === 'aim') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-accent'
                }
                return filteredHistory.map((s, idx) => {
                  const tsForSegment = s.modified ?? s.created
                  const seg = dateSegment(tsForSegment)
                  const showHeader = showSegments && seg !== prevSeg
                  prevSeg = seg
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  // Divider between consecutive rows — but not before a segment header
                  // (the header itself separates), and not after the last row.
                  const isLast = idx === filteredHistory.length - 1
                  const nextSeg = !isLast ? dateSegment(filteredHistory[idx + 1].modified ?? filteredHistory[idx + 1].created) : seg
                  const showDivider = !isLast && (!showSegments || nextSeg === seg)
                  return (
                    <Fragment key={s.key}>
                      {showHeader && (
                        <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {/* Row mirrors session-row two-line layout: agent+timestamp header, title body.
                          Platform glyph (Monitor for dashboard, Slack logo for Slack sessions) occupies
                          the same left column that session rows use for the unread dot — keeps widths aligned. */}
                      <div className="group relative flex items-start gap-2.5 pr-4 py-2 rounded-md cursor-pointer text-sm text-muted hover:text-text hover:bg-bg-hover transition-all select-none" style={{ paddingLeft: '10px' }} title={s.title || s.key} onMouseDown={e => {
                        e.preventDefault()
                        if ((e.target as HTMLElement).closest?.('[data-close]')) { if (confirm('Are you sure you want to delete this history session?')) dispatch(deleteHistorySession(s.key)); return }
                        dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                      }}>
                        {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                        <span className="shrink-0 flex items-center justify-center self-center text-muted" title={isDashboard ? 'Dashboard session' : 'Slack session'} aria-label={isDashboard ? 'Dashboard session' : 'Slack session'}>
                          {isDashboard
                            ? <Monitor size={12} />
                            : <svg className="w-3 h-3 block" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 15a2 2 0 1 1 0-4h4v4a2 2 0 1 1-4 0Zm4-4V5a2 2 0 1 1 4 0v6h-4Z" fill="#E01E5A"/><path d="M18 9a2 2 0 1 1 0 4h-4V9a2 2 0 1 1 4 0Zm-4 4v6a2 2 0 1 1-4 0v-6h4Z" fill="#36C5F0"/><path d="M10 5a2 2 0 0 1 4 0v4h-4V5Z" fill="#2EB67D"/><path d="M14 19a2 2 0 0 1-4 0v-4h4v4Z" fill="#ECB22E"/></svg>
                          }
                        </span>
                        <div className="flex-1 min-w-0 overflow-hidden">
                          <div className={`text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                            <span className="truncate">{agentName || '\u00A0'}</span>
                            {s.memory_mode === 'incognito' && <span className="text-muted" title="Incognito — no memory writes"><EyeOff size={10} /></span>}
                            {s.memory_mode === 'temporary' && <span className="text-aim" title="Temporary — no memory reads or writes"><VenetianMask size={10} /></span>}
                            {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                          </div>
                          <div className="text-[13px] leading-snug line-clamp-2 break-words">{s.title || s.key}</div>
                        </div>
                        {/* Floating hover button group — matches session-row pattern */}
                        <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                          <span data-close="1" title="Delete history session" aria-label="Delete history session" className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all"><X size={12} /></span>
                        </div>
                      </div>
                      {showDivider && <div className="mx-3 border-b border-border" />}
                    </Fragment>
                  )
                })
              })()}
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>Load more…</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Context menu */}
      {ctxMenu && (
        <div className="fixed inset-0 z-[9999]" onClick={() => setCtxMenu(null)} onContextMenu={e => { e.preventDefault(); setCtxMenu(null) }} onKeyDown={e => { if (e.key === 'Escape') setCtxMenu(null) }}>
          <div role="menu" className="absolute bg-bg-elevated border border-border rounded-lg shadow-lg py-1 min-w-[160px] text-[13px]"
            style={{ left: Math.min(ctxMenu.x, window.innerWidth - 170), top: Math.min(ctxMenu.y, window.innerHeight - 140) }}
            onClick={e => e.stopPropagation()}>
            <button role="menuitem" autoFocus className="w-full px-3 py-1.5 text-left text-text hover:bg-bg-hover cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
              onClick={() => { const s = slots.find(sl => sl.key === ctxMenu.key); setRenamingSlot(ctxMenu.key); setRenameValue(s?.title && s.title !== s.key ? s.title : ''); setCtxMenu(null) }}>
              <Pencil size={13} /> Rename
            </button>
            <button role="menuitem" className="w-full px-3 py-1.5 text-left text-text hover:bg-bg-hover cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
              onClick={() => { const s = slots.find(sl => sl.key === ctxMenu.key); if (s) copySessionLink(s.key, s.title, undefined, mode); setCtxMenu(null) }}>
              <Link2 size={13} /> Copy link
            </button>
            <button role="menuitem" className="w-full px-3 py-1.5 text-left text-text hover:bg-bg-hover cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
              onClick={() => { dispatch(unreadSlots.includes(ctxMenu.key) ? markSlotRead(ctxMenu.key) : markSlotUnread(ctxMenu.key)); setCtxMenu(null) }}>
              <Circle size={13} /> {unreadSlots.includes(ctxMenu.key) ? 'Mark as read' : 'Mark as unread'}
            </button>
            <button role="menuitem" className="w-full px-3 py-1.5 text-left text-text hover:bg-bg-hover cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
              onClick={() => { togglePin(ctxMenu.key); setCtxMenu(null) }}>
              <Pin size={13} /> {pinned.has(ctxMenu.key) ? 'Unpin' : 'Pin'}
            </button>
            <div className="mx-2 my-1 border-b border-border" />
            <button role="menuitem" className="w-full px-3 py-1.5 text-left text-danger hover:bg-danger-subtle cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
              onClick={() => { if (!loadChatConfig().confirmCloseSession || confirm('Close this session?')) dispatch(deleteSlot(ctxMenu.key)); setCtxMenu(null) }}>
              <X size={13} /> Close session
            </button>
            {tagColumnsEnabled && (
              <button role="menuitem" className="w-full px-3 py-1.5 text-left text-text hover:bg-bg-hover cursor-pointer flex items-center gap-2 bg-transparent border-none font-body"
                onClick={() => { setTagCtxSlot(ctxMenu.key); setCtxMenu(null) }}>
                <TagIcon size={13} /> Tags…
              </button>
            )}
          </div>
        </div>
      )}
      {/* Per-slot tag assignment popover (opened from context menu) */}
      {tagCtxSlot && (() => {
        const slot = slots.find(s => s.key === tagCtxSlot)
        // Prefer the in-flight optimistic snapshot so a burst of rapid
        // toggles shows the updated checkmarks immediately instead of
        // waiting for the SSE round-trip back from the server.
        const pending = pendingSlotTags[tagCtxSlot]
        const currentTags = new Set(pending ?? slot?.tags ?? [])
        return (
          <div role="button" tabIndex={0} aria-label="Close tag picker"
            className="fixed inset-0 z-[9999]"
            onClick={e => { if (e.target === e.currentTarget) setTagCtxSlot(null) }}
            onKeyDown={e => {
              // Only handle keys originating directly on the backdrop —
              // events bubbling up from inner dialog buttons/inputs must not
              // dismiss the picker.
              if (e.target !== e.currentTarget) return
              if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') {
                e.preventDefault()
                setTagCtxSlot(null)
              }
            }}>
            <div role="dialog" aria-modal="true" aria-label="Assign tags" data-testid="slot-tag-picker"
              className="absolute bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px]"
              style={{ left: '50%', top: '30%', transform: 'translate(-50%, 0)' }}
              onClick={e => e.stopPropagation()}
              onKeyDown={e => { if (e.key === 'Escape') setTagCtxSlot(null) }}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[11px] font-semibold text-muted uppercase tracking-wider px-1">Assign tags</span>
                <button type="button" className="text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none" onClick={() => setTagCtxSlot(null)} aria-label="Close"><X size={13} /></button>
              </div>
              <div className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto">
                {tags.length === 0 && <div className="text-muted px-2 py-1 text-[12px]">No tags yet. Create one below.</div>}
                {[...tags].sort((a, b) => a.order - b.order).map(t => {
                  const on = currentTags.has(t.id)
                  return (
                    <button key={t.id} role="menuitemcheckbox" aria-checked={on} type="button"
                      className={`flex items-center gap-2 px-2 py-1 rounded text-left cursor-pointer bg-transparent border-none transition-all ${on ? 'bg-accent-subtle text-text-strong' : 'text-text hover:bg-bg-hover'}`}
                      onClick={() => toggleSlotTag(tagCtxSlot, t.id)}>
                      <span className="w-3 h-3 rounded-sm border border-border shrink-0" style={{ background: t.color }} />
                      <span className="flex-1 truncate">{t.name}</span>
                      {on && <span className="text-accent"><Check size={11} /></span>}
                    </button>
                  )
                })}
              </div>
              <div className="mt-2 border-t border-border pt-2 flex items-center gap-1">
                <Input
                  className="flex-1 text-[12px] py-1"
                  placeholder="New tag…"
                  {...ime.bindEnter<HTMLInputElement>({
                    onEnter: () => {
                      const el = document.activeElement as HTMLInputElement | null
                      const name = (el?.value || '').trim()
                      if (!name) return
                      createTagMutation.mutate({ name })
                      if (el) el.value = ''
                    },
                    onEscape: () => { setTagCtxSlot(null) },
                    onBlur: () => {},
                  })}
                />
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}

export default memo(ChatSidebar)
