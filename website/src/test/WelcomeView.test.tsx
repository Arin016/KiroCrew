import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WelcomeView from '../components/WelcomeView'

const defaultProps = {
  input: '',
  setInput: vi.fn(),
  send: vi.fn(),
  currentAgent: 'kiroclaw',
  defaultAgent: 'kiroclaw',
  switchAgent: vi.fn(),
  installedAgents: [
    { name: 'kiroclaw', description: 'Default agent', source: 'kiroclaw', model: 'claude-3' },
    { name: 'custom', description: 'Custom agent', source: 'aim', model: 'claude-3' },
  ],
  currentWorkspace: 'default',
  switchWorkspace: vi.fn(),
  availableWorkspaces: [{ name: 'default', path: '/home', is_default: true }],
}

describe('WelcomeView', () => {
  it('renders welcome heading', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByText('What can I do for you?')).toBeInTheDocument()
  })

  it('renders message input', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByLabelText('Message input')).toBeInTheDocument()
  })

  it('disables send button when input is empty', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })

  it('enables send button when input has text', () => {
    renderWithProviders(<WelcomeView {...defaultProps} input="hello" />)
    expect(screen.getByRole('button', { name: /send/i })).not.toBeDisabled()
  })

  it('calls send on button click', () => {
    const send = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} input="test" send={send} />)
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(send).toHaveBeenCalledOnce()
  })

  it('calls send on Enter key when sendOnEnter is enabled', () => {
    const send = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} input="test" send={send} sendOnEnter="enter" />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(send).toHaveBeenCalledOnce()
  })

  it('does not call send on Enter key when sendOnEnter is disabled', () => {
    const send = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} input="test" send={send} sendOnEnter="ctrl-enter" />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(send).not.toHaveBeenCalled()
  })

  it('does not call send on Shift+Enter', () => {
    const send = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} input="test" send={send} sendOnEnter="enter" />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', shiftKey: true })
    expect(send).not.toHaveBeenCalled()
  })

  it('shows prefill hint when enabled', () => {
    renderWithProviders(<WelcomeView {...defaultProps} prefillHint />)
    expect(screen.getByText(/Plan pre-filled/)).toBeInTheDocument()
  })
})


const history = [
  { key: 'sess-1', title: 'Deploy pipeline to gamma', created: '2026-03-22T10:00:00Z' },
  { key: 'sess-2', title: 'Fix login validation bug', created: '2026-03-21T09:00:00Z' },
  { key: 'sess-3', title: 'Refactor memory module', created: '2026-03-20T08:00:00Z' },
]

describe('WelcomeView history suggestions', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows no suggestions when input is empty', () => {
    renderWithProviders(<WelcomeView {...defaultProps} history={history} />)
    expect(screen.queryByText('Continue a previous chat?')).not.toBeInTheDocument()
  })

  it('shows suggestions after debounce when input matches history', () => {
    renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={history} />)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByText('Continue a previous chat?')).toBeInTheDocument()
    expect(screen.getByText('Deploy pipeline to gamma')).toBeInTheDocument()
  })

  it('does not show non-matching sessions', () => {
    renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={history} />)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByText('Fix login validation bug')).not.toBeInTheDocument()
    expect(screen.queryByText('Refactor memory module')).not.toBeInTheDocument()
  })

  it('calls onResumeSession when clicking a suggestion', () => {
    const onResume = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={history} onResumeSession={onResume} />)
    act(() => { vi.advanceTimersByTime(350) })
    fireEvent.mouseDown(screen.getByText('Deploy pipeline to gamma'))
    expect(onResume).toHaveBeenCalledWith('sess-1', 'Deploy pipeline to gamma')
  })

  it('dismisses suggestions on Escape', () => {
    renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={history} />)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByText('Continue a previous chat?')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByText('Continue a previous chat?')).not.toBeInTheDocument()
  })

  it('resets dismissed state when input changes', () => {
    const { rerender } = renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={history} />)
    act(() => { vi.advanceTimersByTime(350) })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByText('Continue a previous chat?')).not.toBeInTheDocument()
    // Change input without clearing — dismissed should reset
    rerender(<WelcomeView {...defaultProps} input="deploy pipeline" history={history} />)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByText('Continue a previous chat?')).toBeInTheDocument()
  })

  it('limits suggestions to 5', () => {
    const manyHistory = Array.from({ length: 10 }, (_, i) => ({
      key: `s-${i}`, title: `Deploy task ${i}`, created: '2026-03-20T00:00:00Z',
    }))
    renderWithProviders(<WelcomeView {...defaultProps} input="deploy" history={manyHistory} />)
    act(() => { vi.advanceTimersByTime(350) })
    const buttons = screen.getAllByText(/Deploy task \d/)
    expect(buttons).toHaveLength(5)
  })

  it('does not match across title+key boundary', () => {
    const edgeHistory = [
      { key: 'def', title: 'abc', created: '2026-03-20T00:00:00Z' },
    ]
    renderWithProviders(<WelcomeView {...defaultProps} input="cde" history={edgeHistory} />)
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.queryByText('Continue a previous chat?')).not.toBeInTheDocument()
  })
})

describe('WelcomeView voice input', () => {
  it('renders mic button when onVoiceToggle is provided', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onVoiceToggle={vi.fn()} />)
    expect(screen.getByTitle('Voice input')).toBeInTheDocument()
  })

  it('does not render mic button when onVoiceToggle is undefined', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.queryByTitle('Voice input')).not.toBeInTheDocument()
  })

  it('calls onVoiceToggle when mic button is clicked', () => {
    const toggle = vi.fn()
    renderWithProviders(<WelcomeView {...defaultProps} onVoiceToggle={toggle} />)
    fireEvent.click(screen.getByTitle('Voice input'))
    expect(toggle).toHaveBeenCalledOnce()
  })

  it('shows recording state on mic button', () => {
    renderWithProviders(<WelcomeView {...defaultProps} voiceRecording onVoiceToggle={vi.fn()} />)
    expect(screen.getByTitle('Stop recording')).toBeInTheDocument()
  })
})