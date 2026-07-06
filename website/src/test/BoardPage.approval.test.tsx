import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import BoardPage from '../pages/BoardPage'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => ({
  api: {
    approveChatSlot: vi.fn(() => Promise.resolve({})),
    health: vi.fn(() => Promise.resolve({ stalled: {} })),
    deleteSession: vi.fn(() => Promise.resolve({})),
  },
}))

function stateWithApprovalSlot(): Partial<RootState> {
  return {
    dashboard: {
      slots: [{
        key: 'slot-1',
        title: 'Test Session',
        messages: 5,
        running: true,
        pending_approval: true,
        pending_approval_info: {
          tool: 'Running: ls /tmp',
          tool_input: '{"command":"ls /tmp"}',
          tool_kind: 'bash',
          request_id: 'req-board-1',
        },
        waiting_for_input: false,
        last_activity_ts: new Date().toISOString(),
        last_ts: new Date().toISOString(),
      }],
      approvalMode: 'normal',
      connected: true,
      channelTrusted: false,
      refreshTrigger: 0,
      unreadSlots: [],
      updateProgress: null,
    } as RootState['dashboard'],
    chat: {
      activeSlot: null,
      messages: [],
      toolLog: [],
      slotStatusDetail: {},
    } as RootState['chat'],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('BoardPage approval', () => {
  it('renders slot in approval lane', () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getByText('Test Session')).toBeInTheDocument()
    expect(screen.getByText('Needs approval')).toBeInTheDocument()
  })

  it('shows tool name in approval card', () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getAllByText(/ls \/tmp/).length).toBeGreaterThan(0)
  })

  it('shows Approve button', () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getByText('Approve')).toBeInTheDocument()
  })

  it('shows Trust dropdown button', () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getByText('Trust')).toBeInTheDocument()
  })

  it('shows Reject button', () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('Approve calls approveChatSlot with request_id', async () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'approved', { request_id: 'req-board-1' }
      )
    })
  })

  it('Reject calls approveChatSlot with request_id', async () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByText('Reject'))
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'rejected', { request_id: 'req-board-1' }
      )
    })
  })

  it('Trust dropdown trust_command calls approveChatSlot with pattern', async () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('button')
    const cmdBtn = buttons.find(b => b.textContent?.includes('ls /tmp'))!
    fireEvent.click(cmdBtn)
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { request_id: 'req-board-1', pattern: 'ls /tmp' }
      )
    })
  })

  it('Trust dropdown trust_base calls approveChatSlot with glob', async () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('button')
    const baseBtn = buttons.find(b => b.textContent?.includes('commands'))!
    fireEvent.click(baseBtn)
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_base', { request_id: 'req-board-1', pattern: 'ls *' }
      )
    })
  })

  it('Trust dropdown entire tool calls approveChatSlot with trust', async () => {
    const store = createTestStore(stateWithApprovalSlot())
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust', { request_id: 'req-board-1' }
      )
    })
  })

  it('shows empty state when no slots', () => {
    const store = createTestStore({
      dashboard: { slots: [], approvalMode: 'normal', connected: true, channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null } as RootState['dashboard'],
      chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as RootState['chat'],
    })
    renderWithProviders(<BoardPage />, { store, route: '/board' })
    expect(screen.getByText(/No active sessions/)).toBeInTheDocument()
  })
})
