/**
 * Regression for Mesh-1908: clicking inside the inline session-rename input
 * did not move the caret to the click point. Root cause: the session row is
 * draggable, and a draggable ancestor makes the browser treat a mousedown in a
 * descendant text input as a drag-gesture start, suppressing native caret
 * placement. Fix: the row is non-draggable while that row is being renamed
 * (draggable={renamingSlot !== s.key}).
 *
 * jsdom can't exercise real caret placement, so the load-bearing assertion is
 * the invariant the fix turns on: the row's draggable attribute is "false"
 * while its rename input is open, and "true" otherwise.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, { get: () => vi.fn().mockResolvedValue([]) }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const SLOT_KEY = 'chat-rename-1'
const slot = { key: SLOT_KEY, title: 'My Session Title', running: false, tags: [], created: '', last_ts: '' }

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function rowFor(container: HTMLElement): HTMLElement {
  const wrap = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
  expect(wrap).toBeTruthy()
  const row = (wrap as HTMLElement).querySelector('.session-row')
  expect(row).toBeTruthy()
  return row as HTMLElement
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('Mesh-1908: session row drag is disabled during inline rename', () => {
  // The legacy lane uses dnd-kit drag; the logical drag-enabled state is
  // surfaced via data-draggable (dnd-kit's `disabled` prop is otherwise not
  // reflected as a DOM attribute). Drag must be off during rename so the input
  // gets native click-to-place-caret.
  it('row is draggable before rename', () => {
    const { container } = renderSidebar()
    expect(rowFor(container).getAttribute('data-draggable')).toBe('true')
  })

  it('row becomes non-draggable once the rename input is open', () => {
    const { container } = renderSidebar()
    const row = rowFor(container)
    expect(row.getAttribute('data-draggable')).toBe('true')

    fireEvent.contextMenu(row)
    const menu = container.querySelector('[role="menu"]') as HTMLElement
    expect(menu).toBeTruthy()
    fireEvent.click(within(menu).getByText('Rename'))

    // Same row node, now rendering the input; drag must be off so the browser
    // gives the input native click-to-place-caret.
    expect(rowFor(container).getAttribute('data-draggable')).toBe('false')
    // select-text overrides the row's inherited select-none so click-drag
    // selection works in the field — the other half of the fix.
    const input = within(rowFor(container)).getByRole('textbox')
    expect(input.className).toContain('select-text')
  })
})
