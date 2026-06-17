import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  ;(globalThis as any).ResizeObserver = class { observe() {}; unobserve() {}; disconnect() {} }
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

  it('Navigation tab renders links + outline and scrolls a section on click', () => {
    const onScroll = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    // Seed the active tab to 'nav' so the component renders the Navigation
    // body directly — SegmentedControl falls back to compact mode under jsdom
    // (zero width), so clicking the inactive segment label is unreliable.
    store.dispatch(openActivityToTab('nav'))
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            {...baseProps}
            navLinks={[{ url: 'https://code.amazon.com/reviews/CR-1', type: 'cr', label: 'CR-1', msgIdx: 0 }]}
            navSections={[{ label: 'First question', msgIdx: 0, displayIdx: 3 }]}
            onScrollToNavSection={onScroll}
          />
        </QueryClientProvider>
      </Provider>,
    )
    expect(screen.getByText('CR-1')).toBeInTheDocument()
    const section = screen.getByText('First question')
    expect(section).toBeInTheDocument()
    fireEvent.click(section)
    expect(onScroll).toHaveBeenCalledWith(3)
  })
})
