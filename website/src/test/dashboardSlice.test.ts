import { describe, it, expect, vi } from 'vitest'
import reducer, {
  sseStatus,
  sseConnected,
  sseDisconnected,
  sseSlots,
  sseSlotTitle,
  addSlotOptimistic,
  removeSlotOptimistic,
  triggerRefresh,
  markSlotUnread,
  markSlotRead,
  fetchSlots,
  selectUnreadByMode,
} from '../store/dashboardSlice'
import type { StatusData, ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

const slot1: ChatSlot = { key: 'chat-1', title: 'Chat 1', messages: 5, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }
const slot2: ChatSlot = { key: 'chat-2', title: 'Chat 2', messages: 3, running: true, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }

describe('dashboardSlice', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('has correct initial state', () => {
    expect(initial.status).toBeNull()
    expect(initial.connected).toBe(false)
    expect(initial.slots).toEqual([])
    expect(initial.approvalMode).toBe('normal')
    expect(initial.refreshTrigger).toBe(0)
    expect(initial.unreadSlots).toEqual([])
  })

  describe('sseStatus', () => {
    it('sets status and connected', () => {
      const status = { uptime: '1h', sessions: 2, messages: 10, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData
      const state = reducer(initial, sseStatus(status))
      expect(state.status).toEqual(status)
      expect(state.connected).toBe(true)
    })

    it('syncs yolo mode from backend', () => {
      const status = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo: true } as StatusData
      const state = reducer(initial, sseStatus(status))
      expect(state.approvalMode).toBe('yolo')
    })

    it('reverts from yolo when backend says false', () => {
      const yoloState = { ...initial, approvalMode: 'yolo' }
      const status = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo: false } as StatusData
      const state = reducer(yoloState, sseStatus(status))
      expect(state.approvalMode).toBe('normal')
    })
  })

  it('sseConnected sets connected true', () => {
    expect(reducer(initial, sseConnected()).connected).toBe(true)
  })

  it('sseDisconnected sets connected false', () => {
    const connected = { ...initial, connected: true }
    expect(reducer(connected, sseDisconnected()).connected).toBe(false)
  })

  it('sseSlots replaces slots', () => {
    const state = reducer(initial, sseSlots([slot1, slot2]))
    expect(state.slots).toHaveLength(2)
  })

  it('sseSlotTitle updates matching slot title', () => {
    const withSlots = reducer(initial, sseSlots([slot1, slot2]))
    const state = reducer(withSlots, sseSlotTitle({ key: 'chat-1', title: 'Renamed' }))
    expect(state.slots[0].title).toBe('Renamed')
    expect(state.slots[1].title).toBe('Chat 2')
  })

  it('addSlotOptimistic adds if not present', () => {
    const state = reducer(initial, addSlotOptimistic(slot1))
    expect(state.slots).toHaveLength(1)
    // Adding same key again should not duplicate
    const state2 = reducer(state, addSlotOptimistic(slot1))
    expect(state2.slots).toHaveLength(1)
  })

  it('removeSlotOptimistic removes by key', () => {
    const withSlots = reducer(initial, sseSlots([slot1, slot2]))
    const state = reducer(withSlots, removeSlotOptimistic('chat-1'))
    expect(state.slots).toHaveLength(1)
    expect(state.slots[0].key).toBe('chat-2')
  })

  it('removeSlotOptimistic also clears unread for removed slot', () => {
    let state = reducer(initial, sseSlots([slot1, slot2]))
    state = reducer(state, markSlotUnread('chat-1'))
    state = reducer(state, removeSlotOptimistic('chat-1'))
    expect(state.unreadSlots).toEqual([])
  })

  it('fetchSlots.fulfilled reconciles unreadSlots against live slots', () => {
    let state = reducer(initial, sseSlots([slot1, slot2]))
    state = reducer(state, markSlotUnread('chat-1'))
    state = reducer(state, markSlotUnread('chat-2'))
    // Simulate fetchSlots returning only slot2 (slot1 was deleted remotely)
    state = reducer(state, fetchSlots.fulfilled([slot2], 'requestId'))
    expect(state.unreadSlots).toEqual(['chat-2'])
  })

  it('triggerRefresh increments counter', () => {
    const state = reducer(initial, triggerRefresh())
    expect(state.refreshTrigger).toBe(1)
    const state2 = reducer(state, triggerRefresh())
    expect(state2.refreshTrigger).toBe(2)
  })

  describe('unread slots', () => {
    it('markSlotUnread adds slot key', () => {
      const state = reducer(initial, markSlotUnread('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-1'])
    })

    it('markSlotUnread does not duplicate', () => {
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-1'])
    })

    it('markSlotRead removes slot key', () => {
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-2'))
      state = reducer(state, markSlotRead('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-2'])
    })

    it('markSlotRead is a no-op for unknown key', () => {
      const state = reducer(initial, markSlotRead('nonexistent'))
      expect(state.unreadSlots).toEqual([])
    })
  })

  describe('selectUnreadByMode', () => {
    // The bigger surface-level coverage (orchestrator-leaks-into-Chat
    // regression, orphan-key fallback, appOnly visibility) lives in
    // src/test/surfaces.test.tsx where the registry under test is.
    // Here we pin the underlying factory's contract: surface-key resolution
    // (slot.surface ?? slot.mode) and per-mode memoization.
    const buildState = (slots: ChatSlot[], unread: string[]) =>
      ({ dashboard: { ...initial, slots, unreadSlots: unread } } as unknown as Parameters<ReturnType<typeof selectUnreadByMode>>[0])

    it('honors slot.surface over slot.mode when both are present', () => {
      // Forward-compat: backend now emits an explicit `surface` field that
      // mirrors `mode` today but is allowed to diverge later. A slot whose
      // `mode === ''` but `surface === 'orchestrator'` counts toward the
      // unified chat badge (both '' and 'orchestrator' are chat-like).
      const slot: ChatSlot = { key: 'orch-1', title: 'O', messages: 0, running: false, mode: '', surface: 'orchestrator' }
      const state = buildState([slot], ['orch-1'])
      // Unified: chat badge includes orchestrator slots
      expect(selectUnreadByMode('')(state)).toBe(1)
      expect(selectUnreadByMode('orchestrator')(state)).toBe(1)
    })

    it('falls back to slot.mode when slot.surface is absent (back-compat)', () => {
      // Older backend payloads without a `surface` field must still route
      // via `mode` so this refactor doesn't require a coupled deploy.
      const slot: ChatSlot = { key: 'orch-1', title: 'O', messages: 0, running: false, mode: 'orchestrator' }
      const state = buildState([slot], ['orch-1'])
      // Unified: chat badge includes orchestrator slots
      expect(selectUnreadByMode('')(state)).toBe(1)
      expect(selectUnreadByMode('orchestrator')(state)).toBe(1)
    })

    it('returns the same selector instance on repeated calls (memoization)', () => {
      // Stable reference matters because consumers (selectSurfaceBadgeCount,
      // selectAllSurfacesAttention) call this on every render — recreating
      // the selector would defeat both useAppSelector's referential-equality
      // fast path and reselect's input-equality memoization.
      expect(selectUnreadByMode('orchestrator')).toBe(selectUnreadByMode('orchestrator'))
    })
  })

  describe('reconnect unread suppression', () => {
    // The useWebSocket hook uses a reconnectingRef (set directly in onopen,
    // cleared on fetchSlots resolve) to suppress markSlotUnread during the
    // post-reconnect catch-up window. These reducer-level tests verify the
    // store invariants the hook relies on; the actual guard is pinned by
    // useWebSocketReconnect.test.ts at the hook level.
    it('sseConnected resets slotsLoaded to false (reconnect signal)', () => {
      let state = reducer(initial, sseSlots([slot1, slot2]))
      expect(state.slotsLoaded).toBe(true)
      state = reducer(state, sseConnected())
      expect(state.slotsLoaded).toBe(false)
    })

    it('fetchSlots.fulfilled after reconnect does not spuriously add unreads', () => {
      // Simulates: reconnect → fetchSlots returns slots → no unreads added
      // (markSlotUnread is guarded by reconnectingRef in the hook, not reducer)
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, sseConnected()) // reconnect
      state = reducer(state, fetchSlots.fulfilled([slot1, slot2], 'requestId'))
      // Existing unread preserved, no new ones spuriously added
      expect(state.unreadSlots).toEqual(['chat-1'])
      expect(state.slotsLoaded).toBe(true)
    })
  })
})
