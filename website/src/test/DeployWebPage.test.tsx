/**
 * Integration test for src/apps/deploy-web/DeployWebPage.tsx.
 *
 * Exercises the major rendering + control-flow paths with a mocked api client:
 *   - Setup card: config prefill + IAM policy reveal
 *   - Sites list: unconfigured / empty / populated
 *   - Publish: confirm-gate (200 requires_confirm) and scan-gate (409 reason=scan)
 *   - SiteRow: recall confirm-gate (200 requires_confirm)
 * The deploy/recall/destroy mocks return { status, data } to mirror the
 * status-aware api client (the backend uses 200+requires_confirm and 409 as
 * normal control flow, not thrown errors).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from '../store'

vi.mock('../api/client', () => ({
  api: {
    deployWebConfig: vi.fn(() => Promise.resolve({ profile: '', region: 'us-west-2' })),
    deployWebSaveConfig: vi.fn((b: any) => Promise.resolve(b)),
    deployWebIamPolicy: vi.fn(() => Promise.resolve({ policy: '{"Version":"2012-10-17","Statement":[]}' })),
    deployWebVerify: vi.fn(() => Promise.resolve({ status: 200, data: { reachable: true, account: '123456789012' } })),
    deployWebSites: vi.fn(() => Promise.resolve({ sites: [], configured: false })),
    deployWebDeploy: vi.fn(),
    deployWebRecall: vi.fn(),
    deployWebDestroy: vi.fn(),
  },
}))

import { api } from '../api/client'
import DeployWebPage from '../apps/deploy-web/DeployWebPage'

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/deploy-web']}>
          <DeployWebPage />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.deployWebConfig).mockResolvedValue({ profile: '', region: 'us-west-2' })
  vi.mocked(api.deployWebSites).mockResolvedValue({ sites: [], configured: false })
  vi.mocked(api.deployWebIamPolicy).mockResolvedValue({ policy: '{"Version":"2012-10-17","Statement":[]}' })
})

describe('DeployWebPage', () => {
  it('renders the header, setup card, and an unconfigured sites notice', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: /Web Deploy/i })).toBeInTheDocument()
    expect(screen.getByText('Setup')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByText(/Configure an AWS profile to list your sites/i)).toBeInTheDocument(),
    )
    // Unconfigured → Publish card warns to set a profile first.
    expect(screen.getByText(/Set an AWS profile in Setup first/i)).toBeInTheDocument()
  })

  it('reveals the IAM policy on demand', async () => {
    renderPage()
    await userEvent.click(screen.getByText(/Required IAM policy/i))
    await waitFor(() => expect(api.deployWebIamPolicy).toHaveBeenCalled())
    expect(await screen.findByText(/"Version":"2012-10-17"/)).toBeInTheDocument()
  })

  it('lists published sites when configured', async () => {
    vi.mocked(api.deployWebSites).mockResolvedValue({
      configured: true,
      sites: [{ site_id: 'my-demo', bucket: 'bkt-123', distribution_id: 'E1ABC' }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('my-demo')).toBeInTheDocument())
    expect(screen.getByText(/bucket: bkt-123/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Recall/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Destroy/i })).toBeInTheDocument()
  })

  it('publish shows the confirm-gate then the published URL on confirm', async () => {
    vi.mocked(api.deployWebSites).mockResolvedValue({ sites: [], configured: true })
    vi.mocked(api.deployWebDeploy)
      .mockResolvedValueOnce({ status: 200, data: { requires_confirm: true, public: true, site_id: 'demo', bytes: 512, scan: 'clean', message: 'This will publish to a PUBLIC URL.' } })
      .mockResolvedValueOnce({ status: 200, data: { site_id: 'demo', url: 'https://abc.cloudfront.net/', status: 'InProgress', reused: false } })
    renderPage()
    await waitFor(() => expect(api.deployWebSites).toHaveBeenCalled())

    await userEvent.type(screen.getByPlaceholderText(/my-demo/i), 'demo')
    await userEvent.type(screen.getByPlaceholderText(/artifact slug/i), 'my-art')
    await userEvent.click(screen.getByRole('button', { name: /^Publish$/i }))

    expect(await screen.findByText(/Confirm public publish/i)).toBeInTheDocument()
    expect(screen.getByText(/512 bytes/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Confirm & publish/i }))
    expect(await screen.findByText('https://abc.cloudfront.net/')).toBeInTheDocument()
    // Second call carried confirm:true.
    expect(vi.mocked(api.deployWebDeploy).mock.calls[1][0]).toMatchObject({ confirm: true })
  })

  it('publish surfaces the pre-publish scan block (409)', async () => {
    vi.mocked(api.deployWebSites).mockResolvedValue({ sites: [], configured: true })
    vi.mocked(api.deployWebDeploy).mockResolvedValue({
      status: 409, data: { blocked: true, reason: 'scan', count: 2, findings: 'line 3: AWS secret key\nline 9: private token' },
    })
    renderPage()
    await waitFor(() => expect(api.deployWebSites).toHaveBeenCalled())

    await userEvent.type(screen.getByPlaceholderText(/my-demo/i), 'demo')
    await userEvent.type(screen.getByPlaceholderText(/artifact slug/i), 'my-art')
    await userEvent.click(screen.getByRole('button', { name: /^Publish$/i }))

    expect(await screen.findByText(/scan found 2 issue/i)).toBeInTheDocument()
    expect(screen.getByText(/Publish anyway/i)).toBeInTheDocument()
  })

  it('recall shows a confirm-gate before mutating', async () => {
    vi.mocked(api.deployWebSites).mockResolvedValue({
      configured: true,
      sites: [{ site_id: 'my-demo', bucket: 'bkt-123', distribution_id: 'E1ABC' }],
    })
    vi.mocked(api.deployWebRecall).mockResolvedValue({
      status: 200, data: { requires_confirm: true, action: 'recall', site_id: 'my-demo', message: 'Recall empties the site (URL → 404) but keeps the infra.' },
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('my-demo')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /Recall/i }))
    expect(await screen.findByText(/Recall empties the site/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Confirm recall/i })).toBeInTheDocument()
  })
})
