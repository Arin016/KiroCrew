import { safeSetItem } from '../../utils/safeStorage'
import { useState, useMemo, useCallback, useEffect } from 'react'
import { Bell, Check, X } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { deleteNotification, clearNotifications, ackAllNotifications } from '../../store/notificationsSlice'
import { EmptyState, SearchInput } from '../ui'
import Clickable from '../Clickable'
import { disintegrate } from '../../lib/disintegrate'
import type { Notification } from '../../types'
import {
  type Kind, type Category, KIND_KEYS, CATEGORIES, KINDS_STORAGE_KEY, loadActiveKinds,
  parseTs, dateGroup, KIND_META, DEFAULT_META, fmtTime, stripMd,
} from './notifMeta'

/**
 * Notification activity feed. Extracted verbatim from NotificationsPage so the
 * full page and the topbar bell popover share one implementation: multi-select
 * kind filter (persisted to localStorage), search, ack-all/clear, and a
 * date-grouped list whose rows disintegrate on delete. Selection state is owned
 * by the host (passed via selectedTs/onSelect) so the host renders the matching
 * detail panel; deleting the selected row clears it naturally because the host
 * derives `selected` from the items list by ts.
 */
export default function NotificationFeed({ selectedTs, onSelect }: {
  selectedTs: string | null
  onSelect: (n: Notification) => void
}) {
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const [activeKinds, setActiveKinds] = useState<Set<Kind>>(loadActiveKinds)
  const [filter, setFilter] = useState('')

  const allActive = activeKinds.size === KIND_KEYS.length
  const noneActive = activeKinds.size === 0

  // Persist filter selection across reloads
  useEffect(() => {
    try { safeSetItem(KINDS_STORAGE_KEY, JSON.stringify(Array.from(activeKinds))) } catch { /* ignore quota errors */ }
  }, [activeKinds])

  const toggleCategory = useCallback((key: Category) => {
    if (key === 'all') {
      // "All" is a meta-toggle: if everything is on, clear; otherwise select all.
      setActiveKinds(prev => prev.size === KIND_KEYS.length ? new Set<Kind>() : new Set<Kind>(KIND_KEYS))
      return
    }
    setActiveKinds(prev => {
      const next = new Set<Kind>(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const filtered = useMemo(() => {
    let list = [...items].reverse()
    // When every known kind is selected, behave like the old "All" state and
    // include notifications with unknown kinds too. Otherwise filter strictly.
    if (!allActive) list = list.filter(n => activeKinds.has(n.kind as Kind))
    if (filter) {
      const q = filter.toLowerCase()
      list = list.filter(n => ((n.title || '') + (n.body || '')).toLowerCase().includes(q))
    }
    return list
  }, [items, activeKinds, allActive, filter])

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

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Category tabs (multi-select). "All" toggles every kind on/off; individual chips toggle that kind. */}
      <div className="flex gap-1 mb-2 flex-wrap shrink-0" role="group" aria-label="Filter notifications by kind">
        {CATEGORIES.map(c => {
          const isActive = c.key === 'all' ? allActive : activeKinds.has(c.key as Kind)
          return (
            <button
              key={c.key}
              type="button"
              aria-pressed={isActive}
              title={c.key === 'all' ? (allActive ? 'Clear all filters' : 'Select all categories') : `Toggle ${c.label}`}
              className={`px-2 py-1 rounded-md text-[12px] font-medium cursor-pointer border transition-all font-body ${isActive ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'}`}
              onClick={() => toggleCategory(c.key)}
            >
              {c.icon} {c.label}
            </button>
          )
        })}
      </div>

      {/* Search + actions. This row sits ABOVE the scroll list (the list is its
          own overflow container below), so nothing scrolls behind it — it needs
          no occluding background. Keep it transparent and make the input
          translucent so the glass surface shows through instead of a white block. */}
      <div className="flex gap-2 mb-2 items-center shrink-0">
        <div className="flex-1"><SearchInput className="[&>input]:!bg-bg-elevated/40 [&>input]:!border-border/60" placeholder="Search…" value={filter} onChange={e => setFilter(e.target.value)} /></div>
        {unread > 0 && <button className="px-2 py-1 rounded-md border border-ok/40 bg-ok/10 text-ok text-[12px] font-semibold cursor-pointer hover:bg-ok/20 transition-all font-body whitespace-nowrap" onClick={() => dispatch(ackAllNotifications())}><Check className="lucide-inline" /> All</button>}
        {items.length > 0 && <button className="px-2 py-1 rounded-md border border-danger/40 bg-transparent text-danger text-[12px] font-medium cursor-pointer hover:bg-danger/10 transition-all font-body whitespace-nowrap" onClick={() => { if (confirm('Clear all notifications?')) dispatch(clearNotifications()) }}><X className="lucide-inline" /> Clear</button>}
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto scroll-shadow">
        {filtered.length === 0 ? (
          <EmptyState icon={<Bell className="lucide-inline" />} title="No notifications" subtitle={noneActive ? 'No categories selected — click a category above' : filter ? 'Try a different search' : 'Activity will appear here'} />
        ) : (
          Array.from(groups.entries()).map(([group, notes]) => (
            <div key={group} className="mb-3">
              <div className="text-[11px] font-semibold text-muted uppercase tracking-[.04em] mb-1.5 px-1">{group}</div>
              {notes.map(n => {
                const km = KIND_META[n.kind] || DEFAULT_META
                const active = selectedTs === n.ts
                return (
                  <div key={n.ts} data-notif-row
                    className={`group flex items-center gap-2 px-2.5 py-2 rounded-md mb-1 transition-all border-l-[3px] ${km.borderColor} ${active ? 'bg-accent-subtle border border-accent' : 'border border-transparent hover:bg-bg-hover hover:border-border'} ${n.acked && !active ? 'opacity-50' : ''}`}
                  >
                    <Clickable
                      onClick={() => onSelect(n)}
                      aria-label={`Open notification: ${n.title}`}
                      className="flex items-center gap-2 flex-1 min-w-0 text-left cursor-pointer"
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
                    </Clickable>
                    <Clickable
                      aria-label="Dismiss notification"
                      className="opacity-0 group-hover:opacity-40 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0"
                      onClick={async e => { e?.stopPropagation(); const row = (e?.currentTarget as HTMLElement | undefined)?.closest('[data-notif-row]') as HTMLElement | null; await disintegrate(row); dispatch(deleteNotification(n.ts)) }}
                    ><X className="lucide-inline" /></Clickable>
                  </div>
                )
              })}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
