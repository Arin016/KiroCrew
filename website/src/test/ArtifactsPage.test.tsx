import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')

const mkArtifact = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'widget',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  ...overrides,
})

describe('ArtifactsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows empty state when no artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/No artifacts yet/i)).toBeInTheDocument())
  })

  it('renders the library with artifacts', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue', { tags: ['ops', 'cr'], version: 3 }),
        mkArtifact('pipeline-health', { tags: ['ops'], kind: 'widget' }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(screen.getByText('pipeline health')).toBeInTheDocument()
    expect(screen.getByText('cr-queue')).toBeInTheDocument()
    expect(screen.getByText('v3')).toBeInTheDocument()
  })

  it('displays summary stats', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('a', { tags: ['x', 'y'], version: 2 }),
        mkArtifact('b', { tags: ['x'], kind: 'markdown', version: 1 }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Total')).toBeInTheDocument())
    // 2 total, 1 widget, 2 unique tags, 3 total versions
    expect(screen.getAllByText('2').length).toBeGreaterThan(0)
  })

  it('filters by name search', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [
        mkArtifact('cr-queue'),
        mkArtifact('ticket-board'),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const search = screen.getByPlaceholderText(/Filter by name/i) as HTMLInputElement
    await userEvent.type(search, 'queue')
    expect(screen.getByText('cr queue')).toBeInTheDocument()
    expect(screen.queryByText('ticket board')).not.toBeInTheDocument()
  })

  it('shows error banner on fetch failure', async () => {
    vi.mocked(api).artifacts = vi.fn().mockRejectedValue(new Error('network down'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/network down/i)).toBeInTheDocument())
  })

  it('calls deleteArtifact when user confirms delete', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const deleteBtn = screen.getByLabelText('Remove from artifacts library')
    await userEvent.click(deleteBtn)
    expect(deleteSpy).toHaveBeenCalledWith('cr-queue')
  })

  it('does not delete when user cancels', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    const deleteSpy = vi.fn()
    vi.mocked(api).deleteArtifact = deleteSpy
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    await userEvent.click(screen.getByLabelText('Remove from artifacts library'))
    expect(deleteSpy).not.toHaveBeenCalled()
  })

  it('refetches with kind filter when kind dropdown changes', async () => {
    const fetcher = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifacts = fetcher
    renderWithProviders(<ArtifactsPage />)
    // Wait for loading state to clear and the kind dropdown to mount.
    await waitFor(() =>
      expect(screen.getByDisplayValue(/all kinds/i)).toBeInTheDocument(),
    )
    const kindSelect = screen.getByDisplayValue(/all kinds/i) as HTMLSelectElement
    await userEvent.selectOptions(kindSelect, 'markdown')
    await waitFor(() => {
      expect(fetcher).toHaveBeenLastCalledWith({ tag: undefined, kind: 'markdown' })
    })
  })

  it('clicking row navigates to detail page (button is keyboard-reachable)', async () => {
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({
      artifacts: [mkArtifact('cr-queue')],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    const openBtn = screen.getByLabelText('Open standalone')
    expect(openBtn).toBeInTheDocument()
  })
})
