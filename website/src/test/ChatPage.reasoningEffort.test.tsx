/**
 * Tests for reasoning effort button in ChatInput (Mesh-1412).
 * Tests the ChatInput component directly to avoid ChatPage's complex dependencies.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

import ChatInput, { REASONING_EFFORT_PROVIDERS, EFFORT_DISPLAY, modelSupportsEffort } from '../components/ChatInput'
import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'

beforeEach(() => { vi.clearAllMocks() })

function renderInput(props: Partial<Parameters<typeof ChatInput>[0]> = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: { dashboard: { slots: [], unreadSlots: [], refreshTrigger: 0, subagentRunning: {}, subagentDetails: {}, subagentText: {} } as any, chat: { activeSlot: null, messages: [], slotRunning: false, toolLog: [], activityOpen: false } as any, notifications: { items: [] } as any },
  })
  const defaults = {
    value: '',
    onChange: vi.fn(),
    onSend: vi.fn(),
    providerId: 'acp',
    reasoningEffort: 'high',
    onReasoningEffortClick: vi.fn(),
    modelName: 'claude-opus-4.7',
    onModelClick: vi.fn(),
  }
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Provider store={store}><ChatInput {...defaults} {...props} /></Provider></QueryClientProvider>)
}

describe('ChatInput reasoning effort button', () => {
  it('renders effort button with current level for claude_code provider', () => {
    renderInput()
    expect(screen.getByTitle(/Reasoning: High/)).toBeInTheDocument()
  })

  it('does not render effort button when capability is off (prop undefined)', () => {
    renderInput({ onReasoningEffortClick: undefined })
    expect(screen.queryByTitle(/Reasoning: High/)).not.toBeInTheDocument()
  })


  it('calls onModelClick with rect on click (reasoning effort merged into model button)', () => {
    const onModelClick = vi.fn()
    renderInput({ onModelClick })
    fireEvent.click(screen.getByTitle(/Reasoning: High/))
    expect(onModelClick).toHaveBeenCalledTimes(1)
    expect(onModelClick.mock.calls[0][0]).toHaveProperty('x')
  })

  it('shows disabled state when running', () => {
    renderInput({ isRunning: true })
    const btn = screen.getByTitle('Stop the current response to switch models')
    expect(btn).toBeDisabled()
  })

  it('EFFORT_DISPLAY covers all valid values incl xhigh', () => {
    expect(EFFORT_DISPLAY['']).toBeDefined()
    expect(EFFORT_DISPLAY['low']).toBeDefined()
    expect(EFFORT_DISPLAY['medium']).toBeDefined()
    expect(EFFORT_DISPLAY['high']).toBeDefined()
    expect(EFFORT_DISPLAY['xhigh']).toBeDefined()
    expect(EFFORT_DISPLAY['max']).toBeDefined()
  })

  it('REASONING_EFFORT_PROVIDERS includes claude_code and acp (kiro)', () => {
    expect(REASONING_EFFORT_PROVIDERS.has('claude_code')).toBe(true)
    expect(REASONING_EFFORT_PROVIDERS.has('acp')).toBe(true)
  })

  it('modelSupportsEffort gates per-model (Opus/Sonnet only)', () => {
    // Capable: Opus/Sonnet in either naming convention.
    expect(modelSupportsEffort('claude-opus-4.7')).toBe(true)
    expect(modelSupportsEffort('claude-sonnet-4.6')).toBe(true)
    expect(modelSupportsEffort('global.anthropic.claude-opus-4-8[1m]')).toBe(true)
    // Not capable: haiku, auto, empty/undefined, third-party.
    expect(modelSupportsEffort('claude-haiku-4.5')).toBe(false)
    expect(modelSupportsEffort('auto')).toBe(false)
    expect(modelSupportsEffort('')).toBe(false)
    expect(modelSupportsEffort(undefined)).toBe(false)
    expect(modelSupportsEffort('deepseek-3.2')).toBe(false)
  })
})

const mockApi = vi.hoisted(() => ({ chatSlotReasoningEffort: vi.fn().mockResolvedValue({ ok: true }), effortLevels: vi.fn().mockResolvedValue(['low', 'medium', 'high', 'xhigh', 'max']) }))
vi.mock('../api/client', () => ({ api: mockApi, SEARCH_MIN_CHARS: 2 }))

function renderDropdown(props: Partial<Parameters<typeof ReasoningEffortDropdown>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const defaults = { slot: 's1', currentEffort: 'high', onClose: vi.fn() }
  return render(<QueryClientProvider client={qc}><ReasoningEffortDropdown {...defaults} {...props} /></QueryClientProvider>)
}

describe('ReasoningEffortDropdown', () => {
  beforeEach(() => { mockApi.effortLevels.mockClear() })

  it('renders all 6 levels incl xhigh from the ACP-reported list', async () => {
    renderDropdown()
    // Levels are fetched async via useQuery — wait for xhigh to appear.
    await vi.waitFor(() => expect(screen.getByTitle(/Set reasoning effort to xhigh/)).toBeInTheDocument())
    expect(screen.getByTitle(/Use the model default effort/)).toBeInTheDocument()
    expect(screen.getByTitle(/Set reasoning effort to low/)).toBeInTheDocument()
    expect(screen.getByTitle(/Set reasoning effort to medium/)).toBeInTheDocument()
    expect(screen.getByTitle('Set reasoning effort to high')).toBeInTheDocument()
    expect(screen.getByTitle(/Set reasoning effort to max/)).toBeInTheDocument()
  })

  it('calls API and onClose on selection', async () => {
    const onClose = vi.fn()
    renderDropdown({ onClose })
    fireEvent.click(screen.getByTitle(/Set reasoning effort to low/))
    expect(onClose).toHaveBeenCalled()
    await vi.waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('s1', 'low'))
  })

  it('marks active level with check icon', () => {
    renderDropdown({ currentEffort: 'max' })
    const maxBtn = screen.getByTitle(/Set reasoning effort to max/)
    expect(maxBtn.querySelector('.lucide-check')).toBeInTheDocument()
    const lowBtn = screen.getByTitle(/Set reasoning effort to low/)
    expect(lowBtn.querySelector('.lucide-check')).not.toBeInTheDocument()
  })

  it('deduplicates default when API returns "default" string', async () => {
    mockApi.effortLevels.mockResolvedValueOnce(['default', 'high', 'low', 'max', 'medium', 'xhigh'])
    renderDropdown()
    await vi.waitFor(() => expect(screen.getByTitle(/Set reasoning effort to xhigh/)).toBeInTheDocument())
    const defaults = screen.getAllByTitle(/default/i)
    expect(defaults).toHaveLength(1)
  })

  it('always shows the current effort even when absent from the reported list', async () => {
    // Slot is on 'xhigh' but this model only reports low/medium/high.
    mockApi.effortLevels.mockResolvedValueOnce(['low', 'medium', 'high'])
    renderDropdown({ currentEffort: 'xhigh' })
    // The active level is still rendered and check-marked (reselectable).
    const xhighBtn = await screen.findByTitle(/Set reasoning effort to xhigh/)
    expect(xhighBtn).toBeInTheDocument()
    expect(xhighBtn.querySelector('.lucide-check')).toBeInTheDocument()
  })

  it('fetches effort levels scoped to the slot', async () => {
    renderDropdown({ slot: 'slot-xyz' })
    await vi.waitFor(() => expect(mockApi.effortLevels).toHaveBeenCalledWith('slot-xyz'))
  })
})
