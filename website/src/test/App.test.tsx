import { describe, it, expect, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import App from '../App'
import SegmentedControl from '../components/SegmentedControl'

// Mock all page components to isolate routing
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div data-testid="system-page">SystemPage</div> }))
vi.mock('../pages/AgentsPage', () => ({ default: () => <div data-testid="agents-page">AgentsPage</div> }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => <div data-testid="projects-page">ProjectsPage</div> }))
vi.mock('../pages/LogsPage', () => ({ default: () => <div data-testid="logs-page">LogsPage</div> }))
vi.mock('../pages/KiroClawAgentsPage', () => ({ default: () => <div data-testid="mc-agents-page">MCAgentsPage</div> }))
vi.mock('../pages/OrchestratedChatPage', () => ({ default: () => <div data-testid="orch-chat-page">OrchestratedChatPage</div> }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => <div data-testid="notifications-page">NotificationsPage</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">SchedulePage</div> }))
vi.mock('../pages/BoardPage', () => ({ default: () => <div data-testid="board-page">BoardPage</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kiroclaw' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kiroclaw' })) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
  },
}))

// Mock matchMedia for useTheme and useIsMobile (jsdom doesn't provide it)
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})

// ResizeObserver stub for jsdom (used by SegmentedControl)
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any

describe('App routing', () => {
  it('renders chat page at /chat', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('renders agents page at /agents', () => {
    renderWithProviders(<App />, { route: '/agents' })
    expect(screen.getByTestId('mc-agents-page')).toBeInTheDocument()
  })

  it('renders projects page at /projects', () => {
    renderWithProviders(<App />, { route: '/projects' })
    expect(screen.getByTestId('projects-page')).toBeInTheDocument()
  })

  it('redirects /tasks to /projects', () => {
    renderWithProviders(<App />, { route: '/tasks' })
    expect(screen.getByTestId('projects-page')).toBeInTheDocument()
  })

  it('renders logs page at /logs', () => {
    renderWithProviders(<App />, { route: '/logs' })
    expect(screen.getByTestId('logs-page')).toBeInTheDocument()
  })

  it('renders board page at /board', async () => {
    renderWithProviders(<App />, { route: '/board' })
    expect(await screen.findByTestId('board-page')).toBeInTheDocument()
  })

  it('redirects unknown routes to /chat', () => {
    renderWithProviders(<App />, { route: '/nonexistent' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('renders nav items', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Chat')).toBeInTheDocument()
    expect(screen.getByText('Agents')).toBeInTheDocument()
    expect(screen.getByText('Projects')).toBeInTheDocument()
    expect(screen.getByText('Settings')).toBeInTheDocument()
  })

  it('does not double-render Secretary when the builtin Secretary app is enabled', async () => {
    // Regression for the Surface registry refactor: Secretary registers a
    // surface (so its attention badge wires through `selectSurfaceBadgeCount`)
    // but is rendered as a nav item by `appNavItems` from `api.listApps()`,
    // not by NAV_ITEMS. With `appOnly: true` on the Secretary surface,
    // `getBuiltinSurfaces()` excludes it from NAV_ITEMS so it should appear
    // exactly once even when api.listApps() returns it.
    const { api } = await import('../api/client')
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      {
        name: 'secretary',
        displayName: 'Secretary',
        enabled: true,
        origin: 'builtin',
        manifest: { ui: { pages: [{ route: '/secretary', icon: 'Inbox', label: 'Secretary' }] } },
      },
    ])
    renderWithProviders(<App />, { route: '/chat' })
    // Wait for refreshAppNav() to complete and merge into the rail.
    await screen.findByText('Secretary')
    // Exactly one nav entry — never two. The duplicate-key React warning
    // would silently fire if both NAV_ITEMS and appNavItems contributed an
    // entry; this assertion catches the visible regression.
    expect(screen.getAllByText('Secretary')).toHaveLength(1)
  })

  it('renders KIROCLAW branding', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getAllByText('KIROCLAW').length).toBeGreaterThan(0)
  })

  it('renders connection status', () => {
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Offline')).toBeInTheDocument()
  })

  it('renders theme toggle', () => {
    renderWithProviders(<App />, { route: '/chat' })
    // Default preference is 'system', button shows "Auto"
    expect(screen.getAllByText(/Auto|Light|Dark/).length).toBeGreaterThan(0)
  })

  it('renders approval mode buttons with tooltips', () => {
    // Mock clientWidth so SegmentedControl renders in full mode (not dropdown)
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 500 })
    const segments = [
      { key: 'normal' as const, label: 'Normal', tooltip: 'Prompt for approval' },
      { key: 'trust' as const, label: 'Trust', tooltip: 'Auto-approve all tools' },
    ]
    const { container } = render(
      <SegmentedControl segments={segments} value="normal" onChange={() => {}} />
    )
    const buttons = container.querySelectorAll('button')
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toHaveAttribute('title', 'Prompt for approval')
    expect(buttons[1]).toHaveAttribute('title', 'Auto-approve all tools')
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 0 })
  })
})

describe('TopbarMetrics widget', () => {
  it('shows only the Activity toggle button when metricsOpen is not set', () => {
    localStorage.removeItem('mc-topbar-metrics')
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTitle('System metrics')).toBeInTheDocument()
    expect(screen.queryByText(/CPU /)).not.toBeInTheDocument()
    expect(screen.queryByText(/MEM /)).not.toBeInTheDocument()
  })

  it('persists toggle open state in localStorage and renders the metrics pill', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/CPU 25%/)).toBeInTheDocument()
    expect(screen.getByText(/MEM 25%/)).toBeInTheDocument()
    expect(screen.getByText(/DSK 40%/)).toBeInTheDocument()
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders placeholder dashes instead of NaN when memTotal or diskTotal is 0', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    sysMock.mockResolvedValueOnce({ mem_used_gb: 4.0, mem_total_gb: 0, cpu_pct: 25.0, disk_total_gb: 0, disk_free_gb: 0 } as never)
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/MEM —/)).toBeInTheDocument()
    expect(screen.getByText(/DSK —/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })

  it('renders "metrics unavailable" pill when api.system rejects', async () => {
    const { api } = await import('../api/client')
    const sysMock = vi.mocked(api.system)
    sysMock.mockRejectedValueOnce(new Error('boom'))
    localStorage.setItem('mc-topbar-metrics', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByText(/metrics unavailable/)).toBeInTheDocument()
    sysMock.mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 } as never)
    localStorage.removeItem('mc-topbar-metrics')
  })
})

describe('onCycleAgent keyboard shortcut', () => {
  it('cycles to next agent when Alt+Shift+A is pressed', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    // Set up the real singleton store state that onCycleAgent reads via store.getState()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'kiroclaw' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', 'reviewer')
  })

  it('does not call api.chatSlotAgent when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })
})

describe('onCycleAgent edge cases', () => {
  it('does not cycle agent when installedAgents is empty', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    const useAgentsMod = await import('../hooks/useAgents')
    const useAgentsMock = vi.mocked(useAgentsMod).useAgents
    useAgentsMock.mockReturnValue({ agents: [], defaultAgent: '' })
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'A', code: 'KeyA', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    useAgentsMock.mockReturnValue({ agents: [{ name: 'kiroclaw' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kiroclaw' })
  })
})

describe('onCyclePrevAgent edge cases', () => {
  it('does not cycle prev agent when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
  })

  it('does not cycle prev agent when installedAgents is empty', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    const useAgentsMod = await import('../hooks/useAgents')
    const useAgentsMock = vi.mocked(useAgentsMod).useAgents
    useAgentsMock.mockReturnValue({ agents: [], defaultAgent: '' })
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).not.toHaveBeenCalled()
    useAgentsMock.mockReturnValue({ agents: [{ name: 'kiroclaw' }, { name: 'reviewer' }, { name: 'oracle' }], defaultAgent: 'kiroclaw' })
  })
})

describe('onCycleApprovalMode and onCyclePrevApprovalMode no-slot cases', () => {
  it('does not cycle approval mode when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('does not cycle prev approval mode when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).not.toHaveBeenCalled()
  })
})

describe('onCycleApprovalMode and onCyclePrevAgent shortcuts', () => {
  it('cycles approval mode forward on Alt+Shift+D', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).toHaveBeenCalledWith('trust_reads', 'slot-1')
  })

  it('cycles agent backward on Alt+Shift+Z', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, agent: 'reviewer' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Z', code: 'KeyZ', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-1', 'kiroclaw')
  })

  it('cycles approval mode backward on Alt+Shift+C', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    // Force approvalMode to 'yolo' via fulfilled thunk action
    store.dispatch({ type: 'dashboard/changeApprovalMode/fulfilled', payload: 'yolo' })
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).toHaveBeenCalledWith('trust', 'slot-1')
  })
})
