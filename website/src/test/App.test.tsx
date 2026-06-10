import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
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
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
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
    expect(screen.getByText('Settings')).toBeInTheDocument()
  })

  it('renders the registry-derived Artifacts and Knowledge nav items', () => {
    // Regression guard for the aaf7cfe stale-branch merge, which reverted the
    // registry-driven rail (`NAV_ITEMS = getBuiltinSurfaces().map(...)`) back
    // to a hardcoded array that omitted Artifacts and Knowledge. Both are
    // registered unconditionally in `surfaces/builtins.tsx`, so they must
    // always appear in the rail. Asserting them by label catches a future
    // hardcoded-array regression that the isolated surfaces.test.tsx cannot.
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByText('Artifacts')).toBeInTheDocument()
    expect(screen.getByText('Knowledge')).toBeInTheDocument()
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

  it('collapses a long Apps list behind a "more" toggle so the nav cannot grow unbounded', async () => {
    // Regression for the nav-overflow bug: with many enabled apps the rail used
    // to grow past the viewport. The Apps group now shows up to APPS_NAV_LIMIT
    // (6) and hides the rest behind a "show more" toggle.
    const { api } = await import('../api/client')
    const manyApps = Array.from({ length: 10 }, (_, i) => ({
      name: `app${i}`,
      displayName: `App ${i}`,
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: `/apps/app${i}`, icon: 'Package', label: `App ${i}` }] } },
    }))
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce(manyApps)
    localStorage.setItem('mc-apps-expanded', '0')
    renderWithProviders(<App />, { route: '/chat' })
    // The "more" toggle appears once the list overflows.
    const moreToggle = await screen.findByTitle(/more app/i)
    expect(moreToggle).toBeInTheDocument()
    // Some later app is hidden while collapsed...
    expect(screen.queryByText('App 9')).not.toBeInTheDocument()
    // ...and revealed after expanding.
    act(() => { moreToggle.click() })
    expect(await screen.findByText('App 9')).toBeInTheDocument()
    // Toggle now offers to collapse again.
    expect(screen.getByTitle(/show fewer apps/i)).toBeInTheDocument()
  })

  it('keeps the overflow toggle visible while expanded (no disappear / layout shift)', async () => {
    // Regression for the toggle-disappears bug: the toggle must render whenever
    // the Apps list is collapsible (length > APPS_NAV_LIMIT), not only when
    // hiddenCount > 0 — otherwise it vanishes (e.g. when the active app is the
    // sole overflow item, pulled into the visible set), causing a layout shift.
    const { api } = await import('../api/client')
    const apps = Array.from({ length: 8 }, (_, i) => ({
      name: `ovf${i}`,
      displayName: `Ovf ${i}`,
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: `/apps/ovf${i}`, icon: 'Package', label: `Ovf ${i}` }] } },
    }))
    ;(api.listApps as ReturnType<typeof vi.fn>).mockResolvedValueOnce(apps)
    // Expanded: hiddenCount is 0 but the list is still collapsible — the toggle
    // must remain (reading "Show less"), proving it doesn't hinge on hiddenCount.
    localStorage.setItem('mc-apps-expanded', '1')
    renderWithProviders(<App />, { route: '/chat' })
    expect(await screen.findByTitle(/show fewer apps/i)).toBeInTheDocument()
  })

  it('shows a portaled hover label for a collapsed (icon-only) nav item', async () => {
    // Covers useNavTip: in collapsed mode nav rows hide their text label and
    // instead show it via a portal to <body> on hover (so the rail's vertical
    // scroll-clip can't chop it). Hover -> the label text appears.
    const { fireEvent } = await import('@testing-library/react')
    localStorage.setItem('mc-nav', '1') // start sidebar collapsed
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    // Collapsed nav items have no visible text; find a row by its class.
    const rows = await waitFor(() => {
      const found = container.querySelectorAll('nav [class*="group/nav"]')
      if (found.length === 0) throw new Error('no nav rows yet')
      return found
    })
    // The icon-only row still names itself for assistive tech via aria-label,
    // since the visible text only mounts on hover (no permanent DOM text node).
    expect(screen.getByLabelText('Chat')).toBeInTheDocument()
    // Hover the first row -> its portaled label text should mount.
    fireEvent.mouseEnter(rows[0])
    expect(await screen.findByText('Chat')).toBeInTheDocument()
    // Leave -> label begins fade-out (still present until the timer).
    fireEvent.mouseLeave(rows[0])
  })

  it('surfaces the collapsed hover label on keyboard focus and is Enter-activatable', async () => {
    // Keyboard-only users (no pointer) must still be able to identify icon-only
    // rows: the label appears on focus, not just mouseenter. The row is also a
    // real control (role=button + tabIndex) operable with Enter.
    const { fireEvent } = await import('@testing-library/react')
    localStorage.setItem('mc-nav', '1') // start sidebar collapsed
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    const rows = await waitFor(() => {
      const found = container.querySelectorAll('nav [role="button"][class*="group/nav"]')
      if (found.length === 0) throw new Error('no focusable nav rows yet')
      return found
    })
    // Focusable as a button.
    expect(rows[0].getAttribute('tabindex')).toBe('0')
    // Focus -> the portaled label mounts (parity with hover).
    fireEvent.focus(rows[0])
    expect(await screen.findByText('Chat')).toBeInTheDocument()
    // Blur -> begins fade-out (still mounted until the unmount timer).
    fireEvent.blur(rows[0])
    // Enter activates without throwing (navigates to the row's route).
    fireEvent.keyDown(rows[0], { key: 'Enter' })
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
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', code: 'KeyF', altKey: true, shiftKey: true, bubbles: true }))
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
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'V', code: 'KeyV', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).not.toHaveBeenCalled()
  })
})

describe('onCycleReasoningEffort no-slot cases', () => {
  it('does not cycle reasoning effort when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })

  it('does not cycle prev reasoning effort when no active slot', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'chat/setActiveSlot', payload: null })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })
})

describe('onCycleApprovalMode and onCyclePrevAgent shortcuts', () => {
  it('cycles approval mode forward on Alt+Shift+F', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'F', code: 'KeyF', altKey: true, shiftKey: true, bubbles: true }))
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

  it('cycles approval mode backward on Alt+Shift+V', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatMode as ReturnType<typeof vi.fn>).mockClear()
    // Force approvalMode to 'yolo' via fulfilled thunk action
    store.dispatch({ type: 'dashboard/changeApprovalMode/fulfilled', payload: 'yolo' })
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'V', code: 'KeyV', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatMode).toHaveBeenCalledWith('trust', 'slot-1')
  })

  it('cycles reasoning effort forward on Alt+Shift+D', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, reasoning_effort: '' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'D', code: 'KeyD', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', 'low')
  })

  it('cycles reasoning effort backward on Alt+Shift+C', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotReasoningEffort as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, reasoning_effort: 'low' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'C', code: 'KeyC', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotReasoningEffort).toHaveBeenCalledWith('slot-1', '')
  })
})

describe('Alt+Shift+S/X model cycling via React Query cache', () => {
  it('does not call chatSlotModel on Alt+Shift+S without cache', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'claude-3' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', code: 'KeyS', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).not.toHaveBeenCalled()
  })

  it('does not call chatSlotModel on Alt+Shift+X without cache', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'claude-3' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    renderWithProviders(<App />, { route: '/chat' })
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'X', code: 'KeyX', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).not.toHaveBeenCalled()
  })

  it('cycles to next model on Alt+Shift+S', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'auto' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'opus' }, { name: 'sonnet' }])
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'S', code: 'KeyS', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-1', 'opus')
  })

  it('cycles to previous model on Alt+Shift+X', async () => {
    const { api } = await import('../api/client')
    const { store } = await import('../store')
    ;(api.chatSlotModel as ReturnType<typeof vi.fn>).mockClear()
    store.dispatch({ type: 'dashboard/sseSlots', payload: [{ key: 'slot-1', messages: 0, running: false, model: 'opus' }] })
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-1' })
    const { queryClient } = renderWithProviders(<App />, { route: '/chat' })
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'opus' }, { name: 'sonnet' }])
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'X', code: 'KeyX', altKey: true, shiftKey: true, bubbles: true }))
    })
    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-1', 'auto')
  })
})
