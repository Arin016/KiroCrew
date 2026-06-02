/**
 * Built-in surface registrations. Imported as a side-effect from `App.tsx`
 * (above the `getBuiltinSurfaces()` call that builds NAV_ITEMS) so that by
 * the time `App.tsx` evaluates, every static nav destination is already in
 * the registry.
 *
 * Order in this file = order in the rail (within each group). Add new
 * built-in surfaces here; do not add hardcoded badge logic to `App.tsx`.
 */
import { MessageSquare, Bell, BookOpen, BookOpenText, Bookmark, CalendarDays, MessageSquareDot, Users, Plug, Store, Settings } from 'lucide-react'
import { createSelector } from '@reduxjs/toolkit'
import { registerBuiltinSurface } from './registry'
import type { RootState } from '../store'

// Memoized at the source so `selectAllSurfacesAttention`'s per-dispatch
// invocation only re-runs the .filter().length when the items array changes
// reference (which is the standard Redux Toolkit pattern).
const selectUnacknowledgedNotificationCount = createSelector(
  (s: RootState) => s.notifications.items,
  items => items.filter(n => !n.acked).length,
)

// ── Main ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'chat',
  route: '/chat',
  label: 'Chat',
  icon: <MessageSquare size={16} />,
  group: 'Main',
  // Slot-bearing: default chat slots have surface === '' (or no mode set).
  slotMode: '',
  badgeLabel: 'unread conversations',
})

registerBuiltinSurface({
  navId: 'notifications',
  route: '/notifications',
  label: 'Notifications',
  icon: <Bell size={16} />,
  group: 'Main',
  // Non-slot: count comes from the notifications panel.
  unreadSelector: selectUnacknowledgedNotificationCount,
  badgeLabel: 'notifications',
})

registerBuiltinSurface({
  navId: 'projects',
  route: '/projects',
  label: 'Projects',
  icon: <BookOpenText size={16} />,
  group: 'Main',
  // Stub surface — no slotMode and no unreadSelector. The Projects badge
  // (global task-gate approval count) comes from a React Query result that
  // lives outside Redux; App.tsx mirrors it into `appBadges['projects']`
  // and `NavBadge` picks it up via the appBadges fallback. The label here
  // is what the fallback path's aria-label uses.
  badgeLabel: 'approvals needed',
})

registerBuiltinSurface({
  navId: 'schedule',
  route: '/schedule',
  label: 'Schedule',
  icon: <CalendarDays size={16} />,
  group: 'Main',
})

// ── Apps ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'orchestrated',
  route: '/orchestrated',
  label: 'Autopilot',
  icon: <MessageSquareDot size={16} />,
  group: 'Apps',
  // Slot-bearing: orchestrator slots route here.
  slotMode: 'orchestrator',
  badgeLabel: 'unread autopilot conversations',
})

registerBuiltinSurface({
  navId: 'knowledge',
  route: '/knowledge',
  label: 'Knowledge',
  icon: <BookOpen size={16} />,
  group: 'Apps',
})

registerBuiltinSurface({
  navId: 'apps',
  route: '/apps',
  label: 'App Store',
  icon: <Store size={16} />,
  group: 'Apps',
})

// ── Platform ───────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'agents',
  route: '/agents',
  label: 'Agents',
  icon: <Users size={16} />,
  group: 'Platform',
})

registerBuiltinSurface({
  navId: 'capabilities',
  route: '/capabilities',
  label: 'Capabilities',
  icon: <Plug size={16} />,
  group: 'Platform',
})

registerBuiltinSurface({
  navId: 'artifacts',
  route: '/artifacts',
  label: 'Artifacts',
  icon: <Bookmark size={16} />,
  group: 'Main',
})

// ── Bottom ─────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'settings',
  route: '/settings',
  label: 'Settings',
  icon: <Settings size={16} />,
  group: 'Bottom',
})
