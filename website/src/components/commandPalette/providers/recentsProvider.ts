import { createElement, useMemo } from 'react'
import { MessageSquare, Clock } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { useAppDispatch, useAppSelector } from '../../../store'
import { createSlot, resumeFromHistory, switchSlot } from '../../../store/chatSlice'
import type { ChatSlot, ChatFolder, CronJob } from '../../../types'
import type { Result, ResourceProvider } from '../types'

/**
 * Recents / quick-switcher — the unscoped empty-query default view (blended
 * from our palette). Unlike the Sessions tab (content search, min 2 chars,
 * empty-returns-nothing), this assembles three grouped buckets so opening the
 * palette with nothing typed reads like a switcher, and it's obvious whether a
 * row is live vs archived:
 *
 *   • Current  — live open dashboard slots (redux), current first then MRU,
 *                with a status dot (needs-approval / running / unread / current).
 *                Enter switches to the slot.
 *   • Planned  — scheduled cron jobs that surface as sessions, by next run.
 *                Enter opens the Schedule page.
 *   • Older    — archived history (`/api/sessions`), deduped against live slots,
 *                faded, day-bucketed (Today / Yesterday / Earlier). Enter
 *                resumes the archived session.
 *
 * It is NOT registered as a tab; the palette uses it only for the unscoped
 * empty state. The `groupLabel` on each Result drives the section headers.
 */

const RECENTS_STALE_MS = 10_000
const HISTORY_LIMIT = 20
const PLANNED_LIMIT = 6

interface HistorySession {
  key: string
  title?: string
  agent?: string
  modified?: number
  folder_id?: string
  preview?: string
}
interface HistoryResponse {
  sessions?: HistorySession[]
}

function sessionIcon() {
  return createElement(MessageSquare, { className: 'lucide-inline' })
}
function plannedIcon() {
  return createElement(Clock, { className: 'lucide-inline' })
}

/** Strip the `dashboard_` prefix so a live slot and its history key compare equal. */
function normalizeKey(key: string): string {
  return key.startsWith('dashboard_') ? key.slice('dashboard_'.length) : key
}

/** Telegram-style relative time (matches the sidebar's fmtRelativeTime):
 * today → "09:46", "Yesterday 21:12", weekday this week, short/full date. */
function fmtRelativeTime(ts: string | number | undefined): string | undefined {
  if (ts == null) return undefined
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return undefined
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const startOf6DaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d >= startOfToday) return time
  if (d >= startOfYesterday) return `Yesterday ${time}`
  if (d >= startOf6DaysAgo) return d.toLocaleDateString([], { weekday: 'short' })
  if (d.getFullYear() === now.getFullYear())
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
}

/** Recency epoch (ms) for sorting live slots — last activity, else last msg, else created. */
function recencyEpoch(slot: ChatSlot): number {
  const t = slot.last_activity_ts ?? slot.last_ts ?? slot.created
  if (!t) return 0
  const ms = typeof t === 'number' ? (t as number) * 1000 : new Date(t).getTime()
  return isNaN(ms) ? 0 : ms
}

function shortMsg(slot: ChatSlot): string {
  const m = (slot.last_message || slot.prompt_preview || '').replace(/\s+/g, ' ').trim()
  return m.length > 80 ? `${m.slice(0, 80).trimEnd()}…` : m
}

/**
 * Live status for a slot, mirroring the chat sidebar's row treatment. The
 * agent name and last-message live in dedicated Result fields (top metadata
 * line + line-3 preview); this only decides the status line 3 accent:
 *  - needs-approval → amber "Approve" pill + " <action>"
 *  - running        → accent pulsing dot + "Thinking…"
 *  - your-turn      → last-message preview + right blue dot
 *  - idle           → last-message preview
 */
function sessionStatus(
  slot: ChatSlot,
  unread: string[],
): {
  style?: 'pill' | 'dot'
  colorVar?: string
  pulse?: boolean
  label?: string
  detail?: string
  rightDot?: { colorVar: string }
  subtitle?: string
} {
  if (slot.pending_approval) {
    return {
      style: 'pill',
      colorVar: '--warn',
      label: 'Approve',
      detail: shortMsg(slot) || undefined,
    }
  }
  if (slot.running) {
    return { style: 'dot', colorVar: '--accent', pulse: true, label: 'Thinking…' }
  }
  if (unread.includes(slot.key) || slot.waiting_for_input) {
    return { rightDot: { colorVar: '--info' }, subtitle: shortMsg(slot) || undefined }
  }
  return { subtitle: shortMsg(slot) || undefined }
}

/**
 * Live recents provider wired to redux (live slots) + React-Query (history,
 * crons) + the router. Rebuilds when the live slots / active / unread /
 * history-order change so the Current group stays fresh.
 */
export function useRecentsProvider(): ResourceProvider {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const slots = useAppSelector((s) => s.dashboard.slots)
  const unread = useAppSelector((s) => s.dashboard.unreadSlots)

  return useMemo(() => {
    // CURRENT — New Session (untitled) first, then pinned, then recency
    // (matches the sidebar ordering with new-chat pinned to the top).
    // Exact match on the backend's synthetic placeholder title — a prefix
    // test would misclassify user-named sessions like "New Session Planning".
    const isNewSlot = (s: ChatSlot) => (s.title || '') === 'New Session…' || (s.title || '') === 'New Session...'
    const orderedSlots = [...slots].sort((a, b) => {
      const na = isNewSlot(a) ? 0 : 1
      const nb = isNewSlot(b) ? 0 : 1
      if (na !== nb) return na - nb
      const pa = a.pinned ? 0 : 1
      const pb = b.pinned ? 0 : 1
      if (pa !== pb) return pa - pb
      return recencyEpoch(b) - recencyEpoch(a)
    })
    const currentKeys = new Set(orderedSlots.map((s) => normalizeKey(s.key)))

    return {
      id: 'recents',
      label: 'Recent',
      icon: sessionIcon(),
      async search(): Promise<Result[]> {
        const [hist, crons, foldersResp] = await Promise.all([
          queryClient
            .fetchQuery<HistoryResponse>({
              queryKey: ['palette', 'recents', 'history'],
              queryFn: () => api.sessions(HISTORY_LIMIT, 0, true),
              staleTime: RECENTS_STALE_MS,
            })
            .catch(() => ({ sessions: [] as HistorySession[] })),
          queryClient
            .fetchQuery<CronJob[] | { jobs?: CronJob[] }>({
              queryKey: ['palette', 'recents', 'crons'],
              queryFn: () => api.crons(),
              staleTime: RECENTS_STALE_MS,
            })
            .catch(() => [] as CronJob[]),
          queryClient
            .fetchQuery<ChatFolder[]>({
              queryKey: ['chat-folders'],
              queryFn: () => api.chatFolders(),
              staleTime: RECENTS_STALE_MS,
            })
            .catch(() => [] as ChatFolder[]),
        ])
        const folders = Array.isArray(foldersResp) ? foldersResp : []
        const folderName = (fid?: string): string | undefined =>
          fid ? folders.find((f) => f.id === fid)?.name : undefined

        // CURRENT — live slots (folder-labeled), ordered pinned-first + recency.
        const current: Result[] = orderedSlots.map((s) => {
          const isNew = isNewSlot(s)
          // A "New Session…" slot is an untitled live session — it may carry
          // messages, but we render it as a bare "+ New Session…" create
          // affordance: no agent line, status, message preview, or timestamp.
          const st = isNew ? {} : sessionStatus(s, unread)
          return {
            id: `recents:cur:${s.key}`,
            providerId: 'recents',
            title: s.title || s.key,
            subtitle: st.subtitle,
            icon: sessionIcon(),
            score: 0,
            indices: [],
            groupLabel: 'Current',
            statusDot: st.rightDot,
            statusStyle: st.style,
            statusColorVar: st.colorVar,
            statusPulse: st.pulse,
            statusLabel: st.label,
            statusDetail: st.detail,
            pinned: isNew ? undefined : s.pinned || undefined,
            folder: isNew ? undefined : folderName(s.folder_id),
            isNew: isNew || undefined,
            timestamp: isNew ? undefined : fmtRelativeTime(s.last_activity_ts ?? s.last_ts),
            onActivate: () => {
              dispatch(switchSlot(s.key))
              navigate('/chat')
            },
          }
        })

        // "+ New Session" must ALWAYS be available as a create affordance.
        // When an untitled live slot exists it doubles as that row (clicking
        // it lands in the fresh session); otherwise synthesize a create
        // action so the row never disappears with the untitled slot.
        if (!orderedSlots.some(isNewSlot)) {
          current.unshift({
            id: 'recents:new-session',
            providerId: 'recents',
            title: 'New Session…',
            icon: sessionIcon(),
            score: 0,
            indices: [],
            groupLabel: 'Current',
            isNew: true,
            onActivate: () => {
              // Await the create BEFORE navigating: landing on /chat with no
              // active slot triggers ChatPage's auto-create, which would race
              // this thunk into a duplicate session (the kiro-todo stray-slot
              // bug, same mechanism).
              void dispatch(createSlot(undefined))
                .unwrap()
                .catch(() => {})
                .finally(() => navigate('/chat'))
            },
          })
        }

        // PLANNED — enabled crons that surface as sessions, soonest first.
        const jobs = (Array.isArray(crons) ? crons : crons?.jobs ?? []) as CronJob[]
        const planned: Result[] = jobs
          .filter((j) => j.enabled && !j.hide_in_chat)
          .sort((a, b) => (a.next_run_ts ?? Infinity) - (b.next_run_ts ?? Infinity))
          .slice(0, PLANNED_LIMIT)
          .map((j) => ({
            id: `recents:plan:${j.id}`,
            providerId: 'recents',
            title: j.name,
            subtitle: j.schedule || j.agent || undefined,
            icon: plannedIcon(),
            score: 0,
            indices: [],
            groupLabel: 'Scheduled',
            onActivate: () => navigate('/schedule'),
          }))

        // OLDER — archived history not already open, faded, one group
        // ("Older Sessions"), newest first, with a last-message preview.
        const sessions = hist?.sessions ?? []
        const older: Result[] = sessions
          .filter((s) => !currentKeys.has(normalizeKey(s.key)))
          .map((s) => ({
            id: `recents:old:${s.key}`,
            providerId: 'recents',
            title: s.title || s.key,
            subtitle: s.preview || undefined,
            icon: sessionIcon(),
            score: 0,
            indices: [],
            groupLabel: 'Older Sessions',
            faded: true,
            folder: folderName(s.folder_id),
            timestamp: fmtRelativeTime(s.modified),
            onActivate: () => {
              void dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
              navigate('/chat')
            },
          }))

        return [...current, ...planned, ...older]
      },
    }
  }, [dispatch, navigate, queryClient, slots, unread])
}
