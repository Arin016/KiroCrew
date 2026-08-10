import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { mcpAppsSwitchState, McpAppsPanel } from '../pages/settings/McpAppsPanel'
import { api } from '../api/client'

function state(partial: Partial<Parameters<typeof mcpAppsSwitchState>[0]> = {}) {
  return mcpAppsSwitchState({
    appsEnabled: true,
    loading: false,
    busy: false,
    ...partial,
  })
}

describe('mcpAppsSwitchState', () => {
  it('reflects apps_enabled', () => {
    expect(state({ appsEnabled: true }).checked).toBe(true)
    expect(state({ appsEnabled: false }).checked).toBe(false)
  })

  it('disables only while loading or applying', () => {
    expect(state({ loading: true }).disabled).toBe(true)
    expect(state({ busy: true }).disabled).toBe(true)
    expect(state().disabled).toBe(false)
  })

  it('takes no gateway input at all', () => {
    // The regression guard for the whole model. `mcp_gateway.enabled` decides
    // whether backends are SHARED, not whether apps render, so a conjunction
    // here would read OFF on a default install while rendering worked. Passing
    // the key must not change the answer — and TS would reject it as a declared
    // field, so this also pins the signature.
    const withGateway = mcpAppsSwitchState({
      appsEnabled: true,
      loading: false,
      busy: false,
      ...({ gatewayEnabled: false } as Record<string, never>),
    })
    expect(withGateway.checked).toBe(true)
  })

  it('exposes no per-state description', () => {
    expect(Object.keys(state())).toEqual(['checked', 'disabled'])
  })
})

describe('McpAppsPanel: the render switch', () => {
  function mount(appsEnabled: boolean, supported = true, gatewayEnabled = false) {
    const state = {
      enabled: gatewayEnabled, apps_enabled: appsEnabled,
      running: gatewayEnabled, ping_ok: gatewayEnabled, supported,
    }
    vi.spyOn(api, 'mcpGatewayStatus').mockImplementation(async () => ({ ...state }) as never)
    vi.spyOn(api, 'mcpGatewayAppsEnable').mockImplementation(async (next: boolean) => {
      state.apps_enabled = next
      return { ok: true, enabled: next } as never
    })
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ mcp_app_panel: false } as never)
    vi.spyOn(api, 'updateDashboardConfig').mockResolvedValue({} as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={['/capabilities']}>
        <QueryClientProvider client={qc}><McpAppsPanel /></QueryClientProvider>
      </MemoryRouter>,
    )
    return state
  }

  async function renderSwitch() {
    // Waits for the switch to be OPERABLE, not merely present: `statusQ.isLoading`
    // disables it on first paint, and a click on a disabled row is swallowed —
    // which would make every assertion below fail for a reason unrelated to it.
    return waitFor(() => {
      const s = screen.getByRole('switch', { name: 'Render MCP apps in chat' })
      expect(s.getAttribute('aria-disabled')).toBeNull()
      return s
    })
  }

  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('reads ON with backend sharing OFF', async () => {
    // The user-visible half of the same guard: this is the default install, and
    // rendering works there. A switch reading OFF here would deny a capability
    // the user has.
    mount(true, true, false)
    await waitFor(async () => {
      expect((await renderSwitch()).getAttribute('aria-checked')).toBe('true')
    })
  })

  it('never touches the sharing endpoint when rendering is turned on', async () => {
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: true } as never)

    mount(false, true, false)
    fireEvent.click(await renderSwitch())

    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(true))
    // Starting the shared backends would restart every active agent session —
    // rendering no longer needs it, so this path must not do it behind the user.
    expect(gatewayEnable).not.toHaveBeenCalled()
  })

  it('asks for no confirmation, because nothing else is started', async () => {
    vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: true } as never)

    mount(false, true, false)
    fireEvent.click(await renderSwitch())

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('turning rendering off writes only the apps flag', async () => {
    const gatewayEnable = vi.spyOn(api, 'mcpGatewayEnable')
    const appsEnable = vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockResolvedValue({ ok: true, enabled: false } as never)

    mount(true, true, true)
    fireEvent.click(await renderSwitch())

    await waitFor(() => expect(appsEnable).toHaveBeenCalledWith(false))
    // Opting out of server-authored UI must not tear down sharing: other
    // sessions depend on it and it is independently valuable.
    expect(gatewayEnable).not.toHaveBeenCalled()
  })

  it('shows the unsupported platform line in the DEFAULT Windows state', async () => {
    mount(true, false, false)
    await waitFor(() => expect(
      screen.getByText(/needs the MCP broker/i),
    ).toBeTruthy())
  })

  it('surfaces the server message when the write is refused', async () => {
    mount(false, true, false)
    // AFTER mount: mount() installs its own spy on this method, so a rejection
    // armed before it would be overwritten and the test would assert nothing.
    vi.spyOn(api, 'mcpGatewayAppsEnable')
      .mockRejectedValue(new Error('apps_enabled is set in config.local.json; edit that file instead'))

    fireEvent.click(await renderSwitch())

    await waitFor(() => expect(
      screen.getByText(/config\.local\.json/i),
    ).toBeTruthy())
  })
})

describe('McpAppsPanel: side panel toggle', () => {
  function mount(dashConfigReady: boolean) {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true, apps_enabled: true, running: true, ping_ok: true, supported: true,
    } as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [{ name: 'srv-0', poolable: true }],
    } as never)
    if (dashConfigReady) {
      vi.spyOn(api, 'dashboardConfig').mockResolvedValue({
        mcp_app_panel: false,
        restore_sessions: true,
        restore_window_minutes: 5,
        merge_queued_messages: false,
        widget_density: 'more',
        verbosity: 'default',
        quick_send: false,
        session_grid: false,
        tail_fork_enabled: false,
        link_previews: true,
        folder_suggestions_enabled: true,
      } as never)
    } else {
      vi.spyOn(api, 'dashboardConfig').mockImplementation(() => new Promise(() => {})) // never resolves
    }
    vi.spyOn(api, 'updateDashboardConfig').mockResolvedValue({} as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <QueryClientProvider client={qc}><McpAppsPanel /></QueryClientProvider>
      </MemoryRouter>,
    )
  }

  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('disables the side panel toggle until dashboardConfig query succeeds', async () => {
    mount(/* dashConfigReady */ false)

    await waitFor(() => {
      const sw = screen.getByRole('switch', { name: 'Open apps in the side panel' })
      expect(sw.getAttribute('aria-disabled')).toBe('true')
    })
  })

  it('enables the side panel toggle once dashboardConfig succeeds', async () => {
    mount(/* dashConfigReady */ true)

    await waitFor(() => {
      const sw = screen.getByRole('switch', { name: 'Open apps in the side panel' })
      expect(sw.getAttribute('aria-disabled')).toBeNull()
    })
  })

  it('calls updateDashboardConfig when toggled', async () => {
    const updateSpy = vi.spyOn(api, 'updateDashboardConfig')
    mount(/* dashConfigReady */ true)

    const sw = await waitFor(() => {
      const el = screen.getByRole('switch', { name: 'Open apps in the side panel' })
      expect(el.getAttribute('aria-disabled')).toBeNull()
      return el
    })
    fireEvent.click(sw)

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ mcp_app_panel: true }),
    ))
  })
})
