import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  ;(isEmbeddedPane as any).mockReturnValue(false)
})

describe('InstancesViewport', () => {
  it('renders nothing when embedded (a pane never hosts nested panes)', () => {
    ;(isEmbeddedPane as any).mockReturnValue(true)
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    } as any)
    const { container } = renderWithProviders(<InstancesViewport />, { store })
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('renders nothing when there are no warm instances', () => {
    const { container } = renderWithProviders(<InstancesViewport />)
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('keeps warm iframes mounted but hidden while on the Local tab', async () => {
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: null, mru: ['cd-1'], unread: {} },
    } as any)
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    // Mounted (so switching back to it is instant) but hidden, and the whole
    // stack is hidden on Local so the native dashboard shows through.
    expect(frame.style.display).toBe('none')
    expect((frame.parentElement as HTMLElement).style.display).toBe('none')
  })

  it('renders the active instance iframe with the loopback token URL', async () => {
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    } as any)
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    expect(frame.getAttribute('src')).toBe(`http://${window.location.hostname}:7778/?token=tok`)
    // Active frame is visible.
    expect(frame.style.display).toBe('block')
  })
})
