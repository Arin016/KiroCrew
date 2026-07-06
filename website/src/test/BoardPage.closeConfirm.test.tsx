import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import BoardPage from '../pages/BoardPage'
import type { RootState } from '../store'

vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useNavigate: () => vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    sessionsHealth: vi.fn(() => Promise.resolve({ stalled: {} })),
    chatSlotDetail: vi.fn(() => Promise.resolve({ slot: 'k', messages: [] })),
    // Never resolves: the close is observable via the synchronous optimistic
    // removal (removeSlotOptimistic) before the await, so we don't need the
    // delete round-trip to complete. This keeps the test focused on the close
    // gate + card removal, not chatSlice's full delete lifecycle.
    deleteChatSlot: vi.fn(() => new Promise<void>(() => {})),
  },
}))

function stateWithIdleSlot(): Partial<RootState> {
  return {
    dashboard: {
      slots: [{
        key: 'slot-idle',
        title: 'Idle Session',
        messages: 2,
        running: false,
        last_ts: new Date().toISOString(),
      }],
      unreadSlots: [],
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
  }
}

// The board card is identified by its "Open session <title>" control.
const cardQuery = () => screen.queryByRole('button', { name: 'Open session Idle Session' })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('BoardPage close session confirmation', () => {
  it('closes without a confirm dialog when confirmCloseSession is off (default)', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<BoardPage />, { store: createTestStore(stateWithIdleSlot()), route: '/board' })

    expect(cardQuery()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close session' }))

    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(cardQuery()).toBeNull())
  })

  it('shows a confirm dialog and closes when confirmCloseSession is on and accepted', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<BoardPage />, { store: createTestStore(stateWithIdleSlot()), route: '/board' })

    fireEvent.click(screen.getByRole('button', { name: 'Close session' }))

    expect(confirmSpy).toHaveBeenCalledOnce()
    expect(confirmSpy.mock.calls[0][0]).toContain('Close session "Idle Session"?')
    await waitFor(() => expect(cardQuery()).toBeNull())
  })

  it('keeps the session when confirmCloseSession is on and the dialog is dismissed', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<BoardPage />, { store: createTestStore(stateWithIdleSlot()), route: '/board' })

    fireEvent.click(screen.getByRole('button', { name: 'Close session' }))

    expect(confirmSpy).toHaveBeenCalledOnce()
    expect(cardQuery()).toBeTruthy()
  })
})
