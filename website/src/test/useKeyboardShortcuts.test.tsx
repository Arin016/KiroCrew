import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, act } from '@testing-library/react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { renderHookWithProviders, createTestStore, renderWithProviders } from './helpers'
import ShortcutsModal from '../components/ShortcutsModal'

beforeEach(() => localStorage.clear())

describe('formatShortcut', () => {
  const setPlatform = (val: string) => Object.defineProperty(navigator, 'platform', { value: val, configurable: true })

  describe('on macOS', () => {
    beforeEach(() => setPlatform('MacIntel'))
    it('uses Option symbol', () => {
      expect(formatShortcut({ id: 't', key: 'k', alt: true, label: '', group: 'Actions' })).toBe('\u2325K')
    })
    it('uses Shift symbol', () => {
      expect(formatShortcut({ id: 't', key: 'n', alt: true, shift: true, label: '', group: 'Actions' })).toBe('\u2325\u21e7N')
    })
    it('uses Return symbol', () => {
      expect(formatShortcut({ id: 't', key: 'Enter', alt: true, label: '', group: 'Actions' })).toBe('\u2325\u23ce')
    })
  })

  describe('on non-Mac', () => {
    beforeEach(() => setPlatform('Win32'))
    it('formats Alt + key', () => {
      expect(formatShortcut({ id: 't', key: 'k', alt: true, label: '', group: 'Actions' })).toBe('Alt + K')
    })
    it('formats Alt + Shift + key', () => {
      expect(formatShortcut({ id: 't', key: 'n', alt: true, shift: true, label: '', group: 'Actions' })).toBe('Alt + Shift + N')
    })
    it('formats arrow keys', () => {
      expect(formatShortcut({ id: 't', key: 'ArrowLeft', alt: true, label: '', group: 'Chat Navigation' })).toBe('Alt + \u2190')
    })
  })
})

describe('DEFAULT_SHORTCUTS', () => {
  it('has unique IDs', () => {
    const ids = DEFAULT_SHORTCUTS.map(s => s.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
  it('has all required groups', () => {
    const groups = new Set(DEFAULT_SHORTCUTS.map(s => s.group))
    expect(groups).toContain('Chat Navigation')
    expect(groups).toContain('Panel Navigation')
    expect(groups).toContain('Actions')
  })
})

describe('useKeyboardShortcuts — toggle behavior', () => {
  const onToggleShortcutsModal = vi.fn()
  const onNewChat = vi.fn()

  function setup(opts: { enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore({
      dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as any,
      chat: { activeSlot: null, slotHistory: [] } as any,
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, disabled: opts.disabled }),
      { store },
    )
    return store
  }

  beforeEach(() => { onToggleShortcutsModal.mockClear(); onNewChat.mockClear() })

  it('Alt+K fires when shortcuts are enabled', () => {
    setup()
    fireEvent.keyDown(document, { code: 'KeyK', altKey: true })
    expect(onToggleShortcutsModal).toHaveBeenCalledTimes(1)
  })

  it('Alt+K fires even when shortcuts are disabled', () => {
    setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'KeyK', altKey: true })
    expect(onToggleShortcutsModal).toHaveBeenCalledTimes(1)
  })

  it('Alt+Shift+N fires new chat when enabled', () => {
    setup()
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })

  it('Alt+Shift+N is suppressed when disabled', () => {
    setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).not.toHaveBeenCalled()
  })

  it('responds to SHORTCUTS_ENABLED_EVENT to re-enable', () => {
    setup({ enabled: false })
    // Re-enable via event (wrapped in act since it triggers state update)
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '1')
    act(() => { window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT)) })
    fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })
    expect(onNewChat).toHaveBeenCalledTimes(1)
  })
})

describe('ShortcutsModal', () => {
  const onClose = vi.fn()
  beforeEach(() => onClose.mockClear())

  it('renders all shortcut groups', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    expect(screen.getByText('Chat Navigation')).toBeInTheDocument()
    expect(screen.getByText('Panel Navigation')).toBeInTheDocument()
    expect(screen.getByText('Actions')).toBeInTheDocument()
  })

  it('renders the enable toggle checked by default', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    const toggle = screen.getByRole('switch')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('clicking toggle sets localStorage to disabled', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(localStorage.getItem(SHORTCUTS_ENABLED_KEY)).toBe('0')
  })

  it('clicking toggle dispatches SHORTCUTS_ENABLED_EVENT', () => {
    const handler = vi.fn()
    window.addEventListener(SHORTCUTS_ENABLED_EVENT, handler)
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(handler).toHaveBeenCalledTimes(1)
    window.removeEventListener(SHORTCUTS_ENABLED_EVENT, handler)
  })

  it('Escape key closes modal', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clicking backdrop closes modal', () => {
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    fireEvent.click(screen.getByRole('dialog'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+A agent cycling', () => {
  it('calls onCycleAgent on Alt+Shift+A', () => {
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).toHaveBeenCalledTimes(1)
  })

  it('does not fire when disabled', () => {
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent, disabled: true }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).not.toHaveBeenCalled()
  })

  it('does not fire when shortcuts are globally disabled', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
    const onCycleAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyA', altKey: true, shiftKey: true })
    expect(onCycleAgent).not.toHaveBeenCalled()
  })
})



describe('Alt+Shift+D approval mode cycling', () => {
  it('calls onCycleApprovalMode on Alt+Shift+D', () => {
    const onCycleApprovalMode = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCycleApprovalMode }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyD', altKey: true, shiftKey: true })
    expect(onCycleApprovalMode).toHaveBeenCalledTimes(1)
  })
})

describe('Alt+Shift+Z previous agent', () => {
  it('calls onCyclePrevAgent on Alt+Shift+Z', () => {
    const onCyclePrevAgent = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevAgent }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyZ', altKey: true, shiftKey: true })
    expect(onCyclePrevAgent).toHaveBeenCalledTimes(1)
  })
})



describe('Alt+Shift+C previous approval mode', () => {
  it('calls onCyclePrevApprovalMode on Alt+Shift+C', () => {
    const onCyclePrevApprovalMode = vi.fn()
    const store = createTestStore()
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn(), onCyclePrevApprovalMode }),
      { store }
    )
    fireEvent.keyDown(document, { code: 'KeyC', altKey: true, shiftKey: true })
    expect(onCyclePrevApprovalMode).toHaveBeenCalledTimes(1)
  })
})
