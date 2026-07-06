import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Mock the knowledge API so we control /items and /sources responses
const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

// Must import after the mock is registered
const { default: KnowledgePage } = await import('../pages/knowledge/index')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

// One source with 436 total items, but the page only returns a 3-item slice and a
// grand total of 482 — mirrors the real bug (backend caps the page at 100).
const pageItems = [
  { id: 'i1', title: 'A', item_type: 'document', status: 'active', source_id: 's1', created_at: '2026-01-01', updated_at: '2026-01-01' },
  { id: 'i2', title: 'B', item_type: 'document', status: 'active', source_id: 's1', created_at: '2026-01-01', updated_at: '2026-01-01' },
  { id: 'i3', title: 'C', item_type: 'document', status: 'active', source_id: 's1', created_at: '2026-01-01', updated_at: '2026-01-01' },
]

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  mockKnowledgeApi.mockImplementation((path: string) => {
    if (path.startsWith('/items')) return Promise.resolve({ items: pageItems, total: 482 })
    if (path === '/sources') return Promise.resolve([
      { id: 's1', name: 'Opportunity Planner', source_type: 'local_folder', uri: '/op', sync_status: 'synced', item_count: 436 },
    ])
    if (path === '/stats') return Promise.resolve({ items: 482, entities: 0, relations: 0, sources: 1 })
    if (path === '/namespaces') return Promise.resolve([])
    return Promise.resolve([])
  })
})

describe('Knowledge List View — pagination math + per-source badge total', () => {
  it('badge shows the source total (item_count), not the loaded page slice', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    // With the fix the badge reflects source.item_count (436). Pre-fix it would render the
    // 3-item page slice, so 436 never appears.
    expect(await screen.findByText('Opportunity Planner')).toBeInTheDocument()
    expect(await screen.findByText('436')).toBeInTheDocument()
  })

  it('requests the backend page cap (limit=100) so pagination math is correct', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await waitFor(() => {
      const itemsCall = mockKnowledgeApi.mock.calls.find(c => String(c[0]).startsWith('/items'))
      expect(itemsCall).toBeTruthy()
      expect(String(itemsCall![0])).toContain('limit=100')
    })
    // total 482 / 100 = 5 pages -> the pager renders and reports the right page count
    expect(await screen.findByText('Page 1 of 5')).toBeInTheDocument()
  })
})
