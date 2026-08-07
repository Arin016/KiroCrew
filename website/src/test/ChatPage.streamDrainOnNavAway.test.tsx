/**
 * End-to-end regression test for the STREAMING nav-away drain
 * (GPT 5.6 round-8 blocking finding on fix/voice-session-above-router).
 *
 * BUG: ChatPage's unmount cleanup stopped a hot STREAMING mic with `cancel()`.
 * That is the Esc/discard path — `useStreamingStt.cancel()` marks the session
 * cancelled so `ws.onclose` returns WITHOUT firing `onFinal`. Navigating away
 * mid-dictation therefore discarded whatever was spoken after the last emitted
 * partial, plus Transcribe's corrections to it. `stop()` is the path that sends
 * `{"type":"stop"}` and deliberately leaves the socket open so the backend can
 * flush its in-flight finals.
 *
 * FIX: unmount takes the graceful stop for BOTH modes, and streaming additionally
 * ARMS a `PendingStreamFinal` so the drained final REPLACES the hypothesis
 * (already in the composer and the draft) instead of appending after it.
 *
 * This drives the real thing: the provider stays mounted (it lives ABOVE the
 * router) while ChatPage unmounts underneath it — exactly what a route change to
 * /schedule does — then the final arrives with no sink and must land replaced.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import { VoiceSessionProvider } from '../providers/VoiceSessionProvider'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { loadDrafts, __resetForTests } from '../utils/chatDrafts'

const SLOT = 'chat-main'
const OTHER_SLOT = 'chat-second'

// Mutable hook state so a test can flip "recording started" AFTER mount, which is
// what the real flow does (the user clicks the mic while already on /chat).
// Starting streamEnabled=false also keeps ChatPage's mount-time slot effect from
// pre-disarming the session, which only fires for streaming.
const hook = vi.hoisted(() => ({
  recording: false,
  streamEnabled: false,
  sessionOwner: null as string | null,
  toggle: vi.fn(),
  cancel: vi.fn(),
  onText: undefined as ((t: string, s: string | null) => void) | undefined,
  onPartial: undefined as ((t: string, s: string | null) => void) | undefined,
  listeners: new Set<() => void>(),
}))

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    sttConfig: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', async () => {
  // The PROVIDER is what calls useVoiceInput, and it does not re-render just
  // because ChatPage does. A real recording start re-renders it via the hook's
  // own useState, so the mock needs the same push: tests flip `hook.*` then call
  // `pushHookUpdate()` to make the provider observe it.
  const { useState, useEffect } = await import('react')
  return {
    useVoiceInput: (
      onText: (t: string, s: string | null) => void,
      opts?: { onPartial?: (t: string, s: string | null) => void },
    ) => {
      const [, bump] = useState(0)
      useEffect(() => {
        const listener = () => bump(n => n + 1)
        hook.listeners.add(listener)
        return () => { hook.listeners.delete(listener) }
      }, [])
      hook.onText = onText
      hook.onPartial = opts?.onPartial
      return {
        recording: hook.recording, transcribing: false,
        sessionOwner: hook.sessionOwner, streamEnabled: hook.streamEnabled,
        error: null, level: 0, deviceLabel: '', partial: 'hello wor', deviceSwitchIsLive: false,
        sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
        toggle: hook.toggle, cancel: hook.cancel,
        prewarm: () => {}, clearError: () => {}, switchDevice: async () => {},
      }
    },
    voiceInputSupported: true,
  }
})
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: OTHER_SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
                { key: SLOT, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: SLOT, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

/**
 * Renders the provider ABOVE a conditionally-mounted ChatPage, so `rerender`
 * with show=false unmounts ONLY the chat surface — the hoisted voice session
 * survives, which is the whole point of the fix under test.
 */
async function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore()
  const tree = (show: boolean) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter><VoiceSessionProvider>{show ? <ChatPage /> : <div>elsewhere</div>}</VoiceSessionProvider></MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree(true))
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return {
    ...utils,
    navAway: async () => { await act(async () => { utils.rerender(tree(false)) }) },
    navBack: async () => { await act(async () => { utils.rerender(tree(true)) }) },
    // A GENUINE slot switch: ChatPage stays mounted, activeSlot changes.
    switchSlot: async (key: string) => {
      await act(async () => { store.dispatch(setActiveSlot(key)) })
    },
  }
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  __resetForTests?.()
  hook.recording = false
  hook.streamEnabled = false
  hook.sessionOwner = null
  hook.listeners.clear()
  hook.toggle.mockClear()
  hook.cancel.mockClear()
  hook.onText = undefined
  hook.onPartial = undefined
  vi.mocked(api.sttConfig).mockResolvedValue({
    enabled: true, streaming: true, dictation_panel: true,
    provider: 'whisper', available: true,
  } as unknown as Awaited<ReturnType<typeof api.sttConfig>>)
})

describe('ChatPage nav-away mid-streaming-dictation — drain, then replace', () => {
  it('takes the graceful stop (drain) instead of cancel, and the final replaces the hypothesis', async () => {
    const { navAway } = await renderApp()
    await waitFor(() => expect(hook.onPartial).toBeTypeOf('function'))

    // The user starts dictating: recording goes hot in streaming mode. Push the
    // update so the PROVIDER re-renders and ChatPage's voiceRef sees it (a real
    // recording start does this via the hook's own state).
    hook.recording = true
    hook.streamEnabled = true
    hook.sessionOwner = SLOT
    await act(async () => { hook.listeners.forEach(l => l()) })
    // A live hypothesis lands, filling the composer the same way onPartial does.
    await act(async () => { hook.onPartial!('hello wor', SLOT) })
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('hello wor')

    // Navigate away — ChatPage unmounts, the hoisted session does not.
    await navAway()

    // THE FINDING: cancel() skips the drain and no final ever fires. The fix must
    // take the draining stop() path instead.
    expect(hook.toggle).toHaveBeenCalled()
    expect(hook.cancel).not.toHaveBeenCalled()

    // The hypothesis was flushed to the draft on unmount...
    expect(loadDrafts()[SLOT]).toContain('hello wor')

    // ...and now the backend's drained, corrected final arrives with no sink.
    await act(async () => { hook.onText!('hello world today', SLOT) })

    // It REPLACED the hypothesis rather than appending after it.
    expect(loadDrafts()[SLOT]).toBe('hello world today')
    expect(loadDrafts()[SLOT]).not.toContain('hello wor hello')
  })

  it('delivers the drained final to the composer when the user navigates BACK first', async () => {
    // GPT round-9 finding: the slot effect (dep [activeSlot]) also runs on MOUNT,
    // and its `sttDisarmedRef = true` made applyVoiceText's first line drop the
    // arriving final — so the descriptor handoff to a remounted sink was dead and
    // the composer kept the stale partial. The provider consumes the descriptor
    // before dispatching, so the text could not even fall back to the draft.
    const { navAway, navBack } = await renderApp()
    await waitFor(() => expect(hook.onPartial).toBeTypeOf('function'))

    hook.recording = true
    hook.streamEnabled = true
    hook.sessionOwner = SLOT
    await act(async () => { hook.listeners.forEach(l => l()) })
    await act(async () => { hook.onPartial!('hello wor', SLOT) })

    // Away and straight back — the drain has NOT landed yet.
    await navAway()
    await navBack()
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())

    // Now the corrected final arrives, with a live sink mounted again.
    await act(async () => { hook.onText!('hello world today', SLOT) })

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('hello world today')
    expect(loadDrafts()[SLOT]).toBe('hello world today')
  })

  it('keeps a suffix typed after the last partial when the drained final lands', async () => {
    // GPT round-10 finding: splicing the final into the FROZEN base deletes
    // anything typed after the last partial, because the base predates it.
    const { navAway, navBack } = await renderApp()
    await waitFor(() => expect(hook.onPartial).toBeTypeOf('function'))

    hook.recording = true
    hook.streamEnabled = true
    hook.sessionOwner = SLOT
    await act(async () => { hook.listeners.forEach(l => l()) })
    await act(async () => { hook.onPartial!('hello wor', SLOT) })

    // The user types a suffix AFTER the last partial.
    const ta0 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(ta0, { target: { value: 'hello wor and typed' } }) })

    await navAway()
    await navBack()
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())

    await act(async () => { hook.onText!('hello world today', SLOT) })

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('hello world today and typed')
  })

  it('STILL disarms on a genuine slot switch (the duplication guard is intact)', async () => {
    // The mount-time gate must not weaken the real reason the disarm exists: on a
    // true switch the outgoing slot's draft already holds the flushed partial, so
    // a late streaming final would duplicate it.
    const { switchSlot } = await renderApp()
    await waitFor(() => expect(hook.onPartial).toBeTypeOf('function'))

    hook.recording = true
    hook.streamEnabled = true
    hook.sessionOwner = SLOT
    await act(async () => { hook.listeners.forEach(l => l()) })
    await act(async () => { hook.onPartial!('hello wor', SLOT) })

    await switchSlot(OTHER_SLOT)
    // A late streaming final for the ORIGINATING slot must be dropped, not
    // appended onto the draft that already carries its partial.
    await act(async () => { hook.onText!('hello world today', SLOT) })
    expect(loadDrafts()[SLOT] ?? '').not.toContain('hello world today')
  })
})
