import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: any) => <div data-testid="virtuoso">{data?.map((d: any, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
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
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: any) => <span>{content}</span> }))
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

function makeStore(activeSlot: string, slots: { key: string; mode?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: s.mode || '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as any,
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as any,
      notifications: { items: [] } as any,
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>, mode?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter><ChatPage mode={mode} /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
  return result!
}

async function renderAndWaitForInput(store: ReturnType<typeof makeStore>, mode?: string) {
  const result = await renderPage(store, mode)
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return result
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage draft persistence', { timeout: 15_000 }, () => {
  it('preserves draft when switching sessions', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'draft for A' } })

    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(saved['slot-a']).toBe('draft for A')

    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('draft for A')
  })

  it('persists draft to localStorage on every keystroke', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'live' } })

    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(saved['slot-x']).toBe('live')
    })
  })

  it('removes draft when input is cleared', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'temp' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBe('temp')
    })

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: '' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBeUndefined()
    })
  })

  it('keeps drafts for multiple sessions independently', async () => {
    const store = makeStore('s1', [{ key: 's1' }, { key: 's2' }, { key: 's3' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'one' } })

    act(() => { store.dispatch(setActiveSlot('s2')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'two' } })

    act(() => { store.dispatch(setActiveSlot('s3')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'three' } })

    const saved = await waitFor(() => {
      const s = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
      expect(s['s3']).toBe('three')
      return s
    })
    expect(saved['s1']).toBe('one')
    expect(saved['s2']).toBe('two')
    expect(saved['s3']).toBe('three')

    act(() => { store.dispatch(setActiveSlot('s1')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('one')
  })

  it('does not overwrite target draft with source input on slot switch (race condition)', async () => {
    // Pre-seed a draft for slot-b
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-b': 'B draft' }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'A text' } })

    // Switch to slot-b — should restore "B draft", NOT "A text"
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('B draft')

    // Verify slot-a draft was saved correctly
    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
    expect(saved['slot-a']).toBe('A text')
  })

  it('localStorage rehydration does not clobber in-memory draft (regression)', async () => {
    // Scenario: type in slot-a, localStorage is stale (doesn't have the draft yet),
    // switch to slot-b — the in-memory draft for slot-a must survive rehydration.
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'fresh text' } })

    // Simulate stale localStorage (e.g. another tab wrote an older version)
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'stale' }))

    // Switch to slot-b
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Switch back to slot-a — should have 'fresh text', not 'stale'
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('fresh text')
  })

  it('draft survives round-trip through three slots', async () => {
    const store = makeStore('a', [{ key: 'a' }, { key: 'b' }, { key: 'c' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'alpha' } })

    act(() => { store.dispatch(setActiveSlot('b')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'beta' } })

    act(() => { store.dispatch(setActiveSlot('c')) })
    // Don't type anything in c

    act(() => { store.dispatch(setActiveSlot('a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('alpha')

    act(() => { store.dispatch(setActiveSlot('b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('beta')

    act(() => { store.dispatch(setActiveSlot('c')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('pre-seeded per-slot file drafts survive slot switches without cross-leak', async () => {
    // Regression guard for screenshot-leak bug: pendingFiles was a single shared
    // useState, so files attached in slot-a appeared in slot-b's compose box
    // when the user switched tabs before sending.
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({
      'slot-a': ['/tmp/screenshot-a.png'],
      'slot-b': ['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'],
    }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Switch to slot-b, then back to slot-a. The slot-switch effect flushes
    // fileDrafts on each transition; the pre-seeded per-slot entries must
    // round-trip unchanged (no cross-leak, no reset-to-empty).
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts')!)
      expect(saved['slot-a']).toEqual(['/tmp/screenshot-a.png'])
      expect(saved['slot-b']).toEqual(['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'])
    })
  })

  it('async upload resolving after slot switch lands in the request slot', async () => {
    // Regression guard for the async-upload race flagged in CR-272843488 review:
    // user starts an upload in slot-a, switches to slot-b before the promise
    // resolves, and the uploaded file must land in slot-a's persisted draft —
    // not silently appear in slot-b's live state.
    const { api } = await import('../api/client')
    let resolveUpload!: (v: { paths: string[] }) => void
    const deferred = new Promise<{ paths: string[] }>(r => { resolveUpload = r })
    vi.mocked(api.uploadFiles).mockReturnValueOnce(deferred)

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Fire a drop event on the chat input area to trigger uploadFiles.
    const input = screen.getByLabelText('Message input')
    const dropTarget = input.closest('div') as HTMLElement
    const file = new File(['x'], 'test.png', { type: 'image/png' })
    await act(async () => {
      fireEvent.drop(dropTarget, { dataTransfer: { files: [file], types: ['Files'] } })
    })

    // Switch to slot-b while the upload is still pending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Now resolve the upload — the file must be diverted to slot-a.
    await act(async () => {
      resolveUpload({ paths: ['/tmp/uploaded.png'] })
      await deferred
    })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
      expect(saved['slot-a']).toEqual(['/tmp/uploaded.png'])
      expect(saved['slot-b']).toBeUndefined()
    })
  })

  it('collapsed paste survives slot switch and sends expanded, not literal token', async () => {
    // Regression for the dead-token bug: a collapsed paste becomes a
    // `[ Paste #N · M lines ]` chip backed by an in-memory PasteBlock. Switching
    // slots used to clear the blocks while the token text was restored from the
    // text draft, so the chip went dead and the literal token was sent.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockClear()

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'line1\nline2\nline3\nline4\nline5'  // >= PASTE_THRESHOLD_LINES

    // Fire a text paste — ChatInput collapses it into a token + PasteBlock.
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    // The textarea now holds the token, not the raw content.
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Switch away and back WITHOUT sending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    // Token text is restored AND still backed by its block.
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Send — the LLM must receive the EXPANDED content, never the literal token.
    await act(async () => { fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' }) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const llmText = vi.mocked(api.sendChat).mock.calls[0][0] as string
    expect(llmText).toContain('line1\nline2\nline3\nline4\nline5')
    expect(llmText).not.toContain('[ Paste #1 · 5 lines ]')
  })

  it('restores paste blocks to the active slot on connection error', async () => {
    // The Mesh-1468 restore path puts the token text back in the input; the
    // backing blocks must come back too, or the restored draft shows a dead token.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockRejectedValueOnce(new Error('Network error'))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'alpha\nbeta\ngamma\ndelta'
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 4 lines \]/))

    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // After the failed send, the paste draft must be persisted for the slot so a
    // subsequent reload/switch can re-pair the token (not just left in the text).
    await waitFor(() => {
      const pasteDrafts = JSON.parse(localStorage.getItem('mc-chat-paste-drafts') || '{}')
      expect(pasteDrafts['slot-a']).toBeTruthy()
      expect(pasteDrafts['slot-a'][0].content).toBe(pasted)
    })
  })

  it('restores draft to localStorage on connection error (Mesh-1468)', async () => {
    // Override sendChat to simulate network failure for this test only
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockRejectedValueOnce(new Error('Network error'))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'precious prompt' } }) })

    // Send triggers connection error (sendChat rejects)
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // Draft should be restored to localStorage after error
    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['slot-a']).toBe('precious prompt')
    })
  })
})
