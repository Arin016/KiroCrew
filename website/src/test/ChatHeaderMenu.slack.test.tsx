/**
 * Tests for the Slack link/unlink menu actions in ChatHeaderMenu (Mesh-1969).
 *
 * Verifies the symmetric menu contract:
 *  - linked   -> shows "Unlink from Slack" + "Post reminder in Slack", hides "Send to Slack"
 *  - clicking Unlink calls api.unlinkSlack and dispatches updateSlot({slack_linked:false})
 *  - after unlink (slack_linked=false) "Send to Slack" reappears, Unlink is gone
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  api: {
    unlinkSlack: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    slackLink: vi.fn().mockResolvedValue({ ok: true }),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
  },
}))

import { useAppSelector } from '../store'
import { api } from '../api/client'
import { ChatHeaderMenu } from '../pages/ChatPage'

/** Binds currentSlot to the store the way ChatPage does (useAppSelector), so a
 *  post-click updateSlot dispatch actually re-renders the menu branch. */
function ConnectedMenu({ slotKey }: { slotKey: string }) {
  const currentSlot = useAppSelector((s: any) => s.dashboard.slots.find((x: any) => x.key === slotKey))
  return (
    <ChatHeaderMenu
      activeSlot={slotKey}
      currentSlot={currentSlot}
      slackChannels={[]}
      onSlackLink={vi.fn()}
      slotKey={slotKey}
    />
  )
}

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as any

function renderMenu(slot: any) {
  // Seed the slot into the store's slots[] so updateSlot (which only mutates an
  // existing slot) has a target to flip.
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ ...slot }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu
              activeSlot="chat-1-100"
              currentSlot={slot}
              slackChannels={[]}
              onSlackLink={vi.fn()}
              slotKey="chat-1-100"
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Open the ⋯ menu (the only button rendered before opening).
  fireEvent.click(utils.container.querySelector('button')!)
  return { store, ...utils }
}

function renderConnectedMenu(slot: any) {
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ ...slot }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ConnectedMenu slotKey={slot.key} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.click(utils.container.querySelector('button')!)
  return { store, ...utils }
}

beforeEach(() => vi.clearAllMocks())

describe('ChatHeaderMenu — Slack unlink', () => {
  it('linked menu shows Unlink + Post reminder and hides Send to Slack', () => {
    renderMenu({ key: 'chat-1-100', slack_linked: true })
    expect(screen.getByText('Unlink from Slack')).toBeInTheDocument()
    expect(screen.getByText('Post reminder in Slack')).toBeInTheDocument()
    expect(screen.queryByText('Send to Slack')).not.toBeInTheDocument()
  })

  it('unlinked menu shows Send to Slack and hides Unlink', () => {
    renderMenu({ key: 'chat-1-100', slack_linked: false })
    expect(screen.getByText('Send to Slack')).toBeInTheDocument()
    expect(screen.queryByText('Unlink from Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Post reminder in Slack')).not.toBeInTheDocument()
  })

  it('clicking Unlink calls api.unlinkSlack and clears the link in the store', async () => {
    const { store } = renderConnectedMenu({ key: 'chat-1-100', slack_linked: true, slack_channel: 'C-1', slack_thread_ts: 'ts-1' })

    fireEvent.click(screen.getByText('Unlink from Slack'))

    await waitFor(() => expect(api.unlinkSlack).toHaveBeenCalledWith('chat-1-100'))
    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find((s: any) => s.key === 'chat-1-100')
      expect(slot?.slack_linked).toBe(false)
      // All three link fields cleared, not just the flag.
      expect(slot?.slack_channel).toBeUndefined()
      expect(slot?.slack_thread_ts).toBeUndefined()
    })
  })

  it('clicking Unlink live-swaps the menu to Send to Slack on the same tree', async () => {
    // Connected harness: currentSlot is store-derived, so the dispatch re-renders.
    const { container } = renderConnectedMenu({ key: 'chat-1-100', slack_linked: true })
    fireEvent.click(screen.getByText('Unlink from Slack'))
    await waitFor(() => expect(api.unlinkSlack).toHaveBeenCalled())

    // The click also closed the menu (setOpen(false)); reopen via the ⋯ toggle
    // (the first/only button left in the tree) and assert the symmetric swap.
    fireEvent.click(container.querySelector('button')!)
    await waitFor(() => {
      expect(screen.getByText('Send to Slack')).toBeInTheDocument()
      expect(screen.queryByText('Unlink from Slack')).not.toBeInTheDocument()
    })
  })
})
