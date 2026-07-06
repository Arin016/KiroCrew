/**
 * Board view (tag-columns) folder rename was dead: renderColumnFolder rendered
 * the folder name as a static <span> with no editingId branch, so the ⋯-menu
 * "Rename" item flipped editingId but no input ever appeared, and there was no
 * double-click affordance either. The list-view header (renderFolderHeader)
 * has both. This adds the inline edit input + double-click-to-rename to board
 * view for parity.
 *
 * Radix DropdownMenu can't be opened in jsdom (needs PointerEvent), so the
 * load-bearing path here is double-click → inline input → Enter commit, which
 * exercises the exact branch that was missing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
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

const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const FOLDER_ID = 'folder-zzzz'

const tags: ChatTag[] = [{ id: REVIEW, name: 'Review', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0 }]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function colFolderHeader(container: HTMLElement): HTMLElement {
  const el = container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}"]`)
  expect(el).toBeTruthy()
  return el as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: folder rename', () => {
  it('shows no rename input until the folder name is double-clicked', () => {
    const { container } = renderSidebar()
    const header = colFolderHeader(container)
    expect(within(header).queryByRole('textbox')).toBeNull()
  })

  it('reveals an inline input pre-filled with the folder name on double-click', () => {
    const { container } = renderSidebar()
    const header = colFolderHeader(container)
    fireEvent.doubleClick(within(header).getByText('CDF'))
    const input = within(colFolderHeader(container)).getByRole('textbox') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.value).toBe('CDF')
  })

  it('commits the new name via Enter, persisting it through updateChatFolder', async () => {
    const { container } = renderSidebar()
    fireEvent.doubleClick(within(colFolderHeader(container)).getByText('CDF'))
    const input = within(colFolderHeader(container)).getByRole('textbox')
    fireEvent.change(input, { target: { value: 'Renamed Folder' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(FOLDER_ID, { name: 'Renamed Folder' }))
  })
})
