import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import { openActivityToTab } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    browseFiles: vi.fn().mockResolvedValue({ path: '/projects/foo', parent: '/', dirs: [], files: [] }),
  },
}))

import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

describe('ActivityViewer', () => {
  const baseProps = {
    subagents: {},
    toolLog: [],
    open: true,
    onToggle: vi.fn(),
    slot: 'test-slot',
  }

  // useSortableTable persists the chosen sort to localStorage keyed by tableId,
  // so clear it between tests to keep the file-browser sort tests independent.
  beforeEach(() => localStorage.clear())

  it('auto-expands file browser when projectDir is set', () => {
    render(<ActivityViewer {...baseProps} projectDir="/projects/foo" />, { wrapper })
    // The "Browse files" section should be expanded (showing the path bar / loading state)
    expect(screen.getByText('Browse files')).toBeInTheDocument()
    // When expanded, the query fires and eventually shows "Loading…" or content
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('does not auto-expand file browser when projectDir is absent', () => {
    render(<ActivityViewer {...baseProps} />, { wrapper })
    expect(screen.getByText('Browse files')).toBeInTheDocument()
    // Browser is collapsed — no Loading indicator
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('file browser sort toggle reorders entries by date (newest first)', async () => {
    // alpha is oldest, zeta is newest. Default sort is name (alpha first);
    // toggling to date sorts newest-first (zeta first).
    vi.mocked(api.browseFiles).mockResolvedValue({
      path: '/projects/foo',
      parent: '/',
      dirs: [],
      files: [
        { name: 'alpha.txt', path: '/projects/foo/alpha.txt', mtime: 1000 },
        { name: 'zeta.txt', path: '/projects/foo/zeta.txt', mtime: 9_999_999_999 },
      ],
    } as never)
    render(<ActivityViewer {...baseProps} projectDir="/projects/foo" />, { wrapper })
    const alpha = await screen.findByText('alpha.txt')
    const zeta = await screen.findByText('zeta.txt')
    // Default (name): alpha precedes zeta in the DOM.
    expect(
      alpha.compareDocumentPosition(zeta) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    // Toggle to date: newest (zeta) now precedes alpha.
    fireEvent.click(screen.getByLabelText(/Sort by date modified/i))
    expect(
      zeta.compareDocumentPosition(alpha) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('column headers flip both Name and Date between ascending and descending', async () => {
    vi.mocked(api.browseFiles).mockResolvedValue({
      path: '/projects/foo',
      parent: '/',
      dirs: [],
      files: [
        { name: 'alpha.txt', path: '/projects/foo/alpha.txt', mtime: 1000 },
        { name: 'zeta.txt', path: '/projects/foo/zeta.txt', mtime: 9_999_999_999 },
      ],
    } as never)
    render(<ActivityViewer {...baseProps} projectDir="/projects/foo" />, { wrapper })
    const alpha = await screen.findByText('alpha.txt')
    const zeta = await screen.findByText('zeta.txt')
    const before = (first: HTMLElement, second: HTMLElement) =>
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING

    const nameHeader = screen.getByLabelText(/Sort by name/i)
    const dateHeader = screen.getByLabelText(/Sort by date modified/i)

    // Default sort is name asc → alpha before zeta.
    expect(before(alpha, zeta)).toBeTruthy()
    // Click active name column → flips to desc → zeta before alpha.
    fireEvent.click(nameHeader)
    expect(before(zeta, alpha)).toBeTruthy()
    // Click name again → flips back to asc (no reset) → alpha before zeta.
    fireEvent.click(nameHeader)
    expect(before(alpha, zeta)).toBeTruthy()
    // Select date → opens newest-first (desc) → zeta before alpha.
    fireEvent.click(dateHeader)
    expect(before(zeta, alpha)).toBeTruthy()
    // Click date again → flips to ascending / oldest-first → alpha before zeta.
    fireEvent.click(dateHeader)
    expect(before(alpha, zeta)).toBeTruthy()
    // Click date once more → flips back to newest-first → zeta before alpha.
    fireEvent.click(dateHeader)
    expect(before(zeta, alpha)).toBeTruthy()
  })

  it('Resources section renders links in Files tab', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    // Files tab is the default
    store.dispatch(openActivityToTab('files'))
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            {...baseProps}
            navLinks={[{ url: 'https://code.amazon.com/reviews/CR-1', type: 'cr', label: 'CR-1', msgIdx: 0 }]}
          />
        </QueryClientProvider>
      </Provider>,
    )
    // Resources section should appear in the Files tab
    expect(screen.getByText('Resources')).toBeInTheDocument()
    expect(screen.getByText('CR-1')).toBeInTheDocument()
  })
})
