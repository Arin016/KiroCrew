import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import BoardPage from '../pages/BoardPage'
import type { RootState } from '../store'

const mockNavigate = vi.hoisted(() => vi.fn())

vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useNavigate: () => mockNavigate,
}))

vi.mock('../api/client', () => ({
  api: {
    sessionsHealth: vi.fn(() => Promise.resolve({ stalled: {} })),
    chatSlotDetail: vi.fn(() => Promise.resolve({ slot: 'k', messages: [] })),
  },
}))

function stateWithIdleSlot(mode?: string): Partial<RootState> {
  return {
    dashboard: {
      slots: [{
        key: 'slot-idle',
        title: 'Idle Session',
        messages: 2,
        running: false,
        mode,
        last_ts: new Date().toISOString(),
      }],
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
  }
}

beforeEach(() => vi.clearAllMocks())

describe('BoardPage card open', () => {
  it('navigates to /chat when the card open control is clicked', () => {
    const store = createTestStore(stateWithIdleSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByRole('button', { name: 'Open session Idle Session' }))
    expect(mockNavigate).toHaveBeenCalledWith('/chat')
  })

  it('navigates to /chat for orchestrator sessions (unified view)', () => {
    const store = createTestStore(stateWithIdleSlot('orchestrator'))
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByRole('button', { name: 'Open session Idle Session' }))
    expect(mockNavigate).toHaveBeenCalledWith('/chat')
  })
})
