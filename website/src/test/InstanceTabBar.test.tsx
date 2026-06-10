import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstanceTabBar from '../components/InstanceTabBar'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      connectInstance: vi.fn(),
    },
  }
})
import { api } from '../api/client'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

const conn = (over = {}) => ({
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
  ...over,
})

const okMidway = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

beforeEach(() => {
  vi.clearAllMocks()
  ;(isEmbeddedPane as any).mockReturnValue(false)
})

describe('InstanceTabBar', () => {
  it('renders nothing when embedded as an instance pane (no recursive nesting)', async () => {
    ;(isEmbeddedPane as any).mockReturnValue(true)
    ;(api.listInstances as any).mockResolvedValue({ instances: [conn()], warm_set_cap: 5, midway: okMidway })
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    } as any)
    const { container } = renderWithProviders(<InstanceTabBar />, { store })
    // No switcher, and the instances poll is disabled while embedded.
    expect(container.querySelector('[role="tablist"]')).toBeNull()
    expect(api.listInstances).not.toHaveBeenCalled()
  })

  it('renders nothing when no instance is connected (single-instance experience)', async () => {
    ;(api.listInstances as any).mockResolvedValue({ instances: [], warm_set_cap: 5, midway: okMidway })
    const { container } = renderWithProviders(<InstanceTabBar />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[role="tablist"]')).toBeNull()
  })

  it('renders Local + a tab per connected instance and switches to Local', async () => {
    ;(api.listInstances as any).mockResolvedValue({ instances: [conn()], warm_set_cap: 5, midway: okMidway })
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    } as any)
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />, { store })

    expect(await screen.findByRole('tab', { name: /Local/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Cloud One/i })).toBeInTheDocument()

    await u.click(screen.getByRole('tab', { name: /Local/i }))
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('connects a not-yet-warm instance when its tab is clicked', async () => {
    ;(api.listInstances as any).mockResolvedValue({ instances: [conn()], warm_set_cap: 5, midway: okMidway })
    ;(api.connectInstance as any).mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
    const u = userEvent.setup()
    const { store } = renderWithProviders(<InstanceTabBar />)

    await u.click(await screen.findByRole('tab', { name: /Cloud One/i }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'tok' }))
    expect(store.getState().instances.activeId).toBe('cd-1')
  })

  it('shows the active tunnel connection status with a token auto-refresh countdown', async () => {
    ;(api.listInstances as any).mockResolvedValue({
      instances: [conn({ status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777, token_ttl_remaining: 72000 } })],
      warm_set_cap: 5,
      midway: okMidway,
    })
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    } as any)
    renderWithProviders(<InstanceTabBar />, { store })
    // ttl 20h (72000s), 72000s remaining -> refresh fires at 80% elapsed (20% left),
    // so untilRefresh = 72000 - 14400 = 57600s ≈ 16h.
    expect(await screen.findByText(/connected · refresh/i)).toBeInTheDocument()
    expect(screen.getByTitle(/Tunnel connected.*auto-refresh in/i)).toBeInTheDocument()
  })
})
