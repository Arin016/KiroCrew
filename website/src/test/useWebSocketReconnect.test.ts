import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

// Track markSlotUnread dispatches
const markSlotUnreadCalls: string[] = []

vi.mock('../store/dashboardSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/dashboardSlice')>()
  return {
    ...actual,
    markSlotUnread: (slot: string) => {
      markSlotUnreadCalls.push(slot)
      return actual.markSlotUnread(slot)
    },
  }
})

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
  },
}))

// Mock WebSocket
const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket reconnect unread suppression', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    markSlotUnreadCalls.length = 0
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { activeSlot: 'chat-active' } as any,
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

  it('suppresses markSlotUnread during reconnect catch-up window', async () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })

    // First connect
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })

    // Verify normal messages DO mark unread
    act(() => {
      ws1.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'hi', ts: '1' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')
    markSlotUnreadCalls.length = 0

    // Simulate disconnect + wait for reconnect timer
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) }) // reconnect backoff

    const ws2 = WS_INSTANCES[1]
    expect(ws2).toBeDefined()
    act(() => { ws2.simulateOpen() }) // triggers reconnect path (wasConnectedRef = true)

    // Messages during reconnect window should NOT mark unread
    act(() => {
      ws2.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'catch-up', ts: '2' } })
      ws2.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'chunk', seq: 1 } })
    })
    expect(markSlotUnreadCalls).toEqual([])

    // After fetchSlots resolves, messages SHOULD mark unread again
    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) }) // flush fetchSlots promise
    act(() => {
      ws2.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'real', ts: '3' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')

    unmount()
  })
})

describe('Mesh-2093: unread fires on chat_done not chat_chunk', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    markSlotUnreadCalls.length = 0
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { activeSlot: 'chat-active' } as any,
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

  it('chat_chunk on non-active slot does NOT mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'thinking...', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'more thinking', seq: 2 } })
    })
    expect(markSlotUnreadCalls).toEqual([])
    unmount()
  })

  it('chat_done on non-active slot DOES mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-other' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')
    unmount()
  })

  it('chat_done on active slot does NOT mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-active' } })
    })
    expect(markSlotUnreadCalls).toEqual([])
    unmount()
  })
})
