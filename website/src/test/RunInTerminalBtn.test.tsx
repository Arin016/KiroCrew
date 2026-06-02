import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import RunInTerminalBtn from '../components/RunInTerminalBtn'
import * as registry from '../utils/terminalRegistry'

vi.mock('../utils/terminalRegistry', () => ({
  sendToTerminal: vi.fn(),
  getActiveTerminalWs: vi.fn(),
  onTerminalReady: vi.fn(() => vi.fn()),
}))

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('RunInTerminalBtn', () => {
  it('renders terminal icon button', () => {
    renderWithProviders(<RunInTerminalBtn code="ls -la" />)
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
    expect(screen.getByTitle('Run in terminal')).toBeInTheDocument()
  })

  it('sends code to terminal on click when WebSocket is active', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).toHaveBeenCalledWith('echo hello')
  })

  it('strips prompt characters before sending', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code="$ git status" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).toHaveBeenCalledWith('git status')
  })

  it('strips prompt chars from multiline code', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code={"$ cd /tmp\n$ ls"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).toHaveBeenCalledWith('cd /tmp\nls')
  })

  it('shows check icon after successful send', async () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()
    expect(screen.queryByLabelText('Run in terminal')).not.toBeInTheDocument()
  })

  it('reverts to idle state after success timeout', async () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(1200) })

    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
  })

  it('shows error state when terminal never connects (5s timeout)', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue(null)
    vi.mocked(registry.sendToTerminal).mockReturnValue(false)
    vi.mocked(registry.onTerminalReady).mockReturnValue(vi.fn())

    const store = createTestStore({ terminal: { open: true, position: 'bottom', sessions: [], activeSessionId: null } })
    renderWithProviders(<RunInTerminalBtn code="ls" />, { store })
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    act(() => { vi.advanceTimersByTime(5000) })

    expect(screen.getByLabelText('No active terminal')).toBeInTheDocument()
  })

  it('reverts from error state after timeout', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue(null)
    vi.mocked(registry.sendToTerminal).mockReturnValue(false)
    vi.mocked(registry.onTerminalReady).mockReturnValue(vi.fn())

    const store = createTestStore({ terminal: { open: true, position: 'bottom', sessions: [], activeSessionId: null } })
    renderWithProviders(<RunInTerminalBtn code="ls" />, { store })
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByLabelText('No active terminal')).toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
  })

  it('sends code when onTerminalReady fires', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue(null)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)
    let readyCb: (() => void) | undefined
    vi.mocked(registry.onTerminalReady).mockImplementation((cb) => {
      readyCb = cb
      return vi.fn()
    })

    const store = createTestStore({ terminal: { open: true, position: 'bottom', sessions: [], activeSessionId: null } })
    renderWithProviders(<RunInTerminalBtn code="brazil-build" />, { store })
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).not.toHaveBeenCalled()

    act(() => { readyCb?.() })
    expect(registry.sendToTerminal).toHaveBeenCalledWith('brazil-build')
  })

  it('does not strip $ when not followed by whitespace (variable ref)', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code={"$HOME/bin/run"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).toHaveBeenCalledWith('$HOME/bin/run')
  })

  it('does not strip $(subshell) syntax', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
    vi.mocked(registry.sendToTerminal).mockReturnValue(true)

    renderWithProviders(<RunInTerminalBtn code={"$(whoami)"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).toHaveBeenCalledWith('$(whoami)')
  })

  it('does nothing when code is empty after stripping prompt chars', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)

    renderWithProviders(<RunInTerminalBtn code="$ " />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(registry.sendToTerminal).not.toHaveBeenCalled()
  })

  it('dispatches openCliPanel when terminal is closed and no WebSocket', () => {
    vi.mocked(registry.getActiveTerminalWs).mockReturnValue(null)
    vi.mocked(registry.sendToTerminal).mockReturnValue(false)

    const store = createTestStore({ terminal: { open: false, position: 'bottom', sessions: [], activeSessionId: null } })
    renderWithProviders(<RunInTerminalBtn code="ls" />, { store })
    fireEvent.click(screen.getByLabelText('Run in terminal'))

    expect(store.getState().terminal.open).toBe(true)
  })
})
