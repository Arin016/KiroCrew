import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OverflowMenu } from '../components/MarkdownPanel'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn(),
    createArtifact: vi.fn(),
  },
}))

const writeText = vi.fn()
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  writeText.mockReset()
  queryClient.clear()
  Object.assign(navigator, { clipboard: { writeText } })
  // Default: no existing artifact for any path. Tests can override.
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
  vi.mocked(api).createArtifact = vi.fn().mockResolvedValue({ slug: 'test-doc-md', version: 1 })
})

function openMenu() {
  render(<OverflowMenu filePath="/tmp/hello.txt" content={'line one\nline two\n'} revealOrCopy={vi.fn()} />, { wrapper })
  fireEvent.click(screen.getAllByRole('button')[0])
}

describe('MarkdownPanel OverflowMenu', () => {
  it('exposes both Copy path and Copy content entries', () => {
    openMenu()
    expect(screen.getByText('Copy path')).toBeInTheDocument()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
  })

  it('Copy path writes the filePath to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy path'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt')
  })

  it('Copy content writes the raw file content to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('line one\nline two\n')
  })

  it('closes the overflow menu after Copy content is clicked', () => {
    openMenu()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Copy content'))
    expect(screen.queryByText('Copy content')).not.toBeInTheDocument()
  })

  it('Copy content copies an empty string for an empty file without throwing', () => {
    render(<OverflowMenu filePath="/tmp/empty.txt" content="" revealOrCopy={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('')
  })

  // ── Phase 6 (Mesh-1654): Add to artifacts (no-modal flow) ─────────────
  it('shows "Add to artifacts" entry for files not yet saved', async () => {
    openMenu()
    await waitFor(() => expect(screen.getByText('Add to artifacts')).toBeInTheDocument())
  })

  it('clicks Add-to-artifacts and POSTs without an interstitial modal', async () => {
    openMenu()
    await waitFor(() => expect(screen.getByText('Add to artifacts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Add to artifacts'))
    await waitFor(() => expect(api.createArtifact).toHaveBeenCalledOnce())
    const body = vi.mocked(api).createArtifact.mock.calls[0][0]
    expect(body.source_path).toBe('/tmp/hello.txt')
    expect(body.source).toBe('manual')
    expect(body.kind).toBe('text') // .txt → text
    expect(body.name).toBe('hello.txt')
  })

  it('autodetects kind from extension', async () => {
    render(<OverflowMenu filePath="/tmp/note.md" content="# hi" revealOrCopy={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    await waitFor(() => expect(screen.getByText('Add to artifacts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Add to artifacts'))
    await waitFor(() => expect(api.createArtifact).toHaveBeenCalledOnce())
    expect(vi.mocked(api).createArtifact.mock.calls[0][0].kind).toBe('markdown')
  })

  it('shows "In Artifacts" + click navigates when file is already saved', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [{ slug: 'hello-txt', name: 'hello.txt' }],
    })
    // Round 6: menu also fetches full artifact detail to check live_dirty.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue({
      slug: 'hello-txt', name: 'hello.txt', kind: 'markdown', source: 'manual',
      description: '', tags: [], version: 1, created_at: '', updated_at: '',
      live_dirty: false,
    })
    openMenu()
    await waitFor(() => expect(screen.getByText('In Artifacts')).toBeInTheDocument())
    expect(screen.queryByText('Add to artifacts')).toBeNull()
    // The "In Artifacts" button is a navigation affordance — assert its
    // title points to the right slug.
    const btn = screen.getByTitle('Open artifact hello-txt')
    expect(btn).toBeInTheDocument()
    // live_dirty=false → no Snapshot affordance.
    expect(screen.queryByText('Snapshot')).toBeNull()
  })

  // ── Coverage push for new hook paths ────────────────────────────────────
  it('Add to Knowledge entry shows when format is supported', async () => {
    // Hits the knowledgeFormats path in useFileKnowledgeState.
    globalThis.fetch = vi.fn().mockImplementation((url: any) => {
      if (typeof url === 'string' && url.startsWith('/api/knowledge/config')) {
        return Promise.resolve(new Response(JSON.stringify({
          enabled: true,
          supported_formats: ['.md', '.txt', '.py'],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      if (typeof url === 'string' && url.startsWith('/api/knowledge/sources')) {
        return Promise.resolve(new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      return Promise.resolve(new Response('null', { status: 200 }))
    }) as any
    openMenu()
    await waitFor(() => expect(screen.getByText('Add to Knowledge')).toBeInTheDocument())
  })

  it('Add to Knowledge button posts on click', async () => {
    let postedBody: any = null
    globalThis.fetch = vi.fn().mockImplementation((url: any, init?: any) => {
      if (typeof url === 'string' && url.startsWith('/api/knowledge/config')) {
        return Promise.resolve(new Response(JSON.stringify({
          enabled: true,
          supported_formats: ['.txt'],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      if (typeof url === 'string' && url.startsWith('/api/knowledge/sources') && (!init || init.method !== 'POST')) {
        return Promise.resolve(new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      if (typeof url === 'string' && url.startsWith('/api/knowledge/sources') && init?.method === 'POST') {
        postedBody = JSON.parse(init.body)
        return Promise.resolve(new Response('{"id":"k1"}', { status: 201, headers: { 'Content-Type': 'application/json' } }))
      }
      return Promise.resolve(new Response('null', { status: 200 }))
    }) as any
    openMenu()
    await waitFor(() => expect(screen.getByText('Add to Knowledge')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Add to Knowledge'))
    await waitFor(() => expect(postedBody).not.toBeNull())
    expect(postedBody.uri).toBe('/tmp/hello.txt')
    expect(postedBody.source_type).toBe('local_file')
  })
})
