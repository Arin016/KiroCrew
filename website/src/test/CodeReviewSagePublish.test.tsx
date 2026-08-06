import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PullRequestSource } from '../types'

const mockApi = vi.hoisted(() => ({
  pullRequestChecks: vi.fn(),
  pullRequestSource: vi.fn(),
  pullRequestStatuses: vi.fn(),
  resolvePullRequestThread: vi.fn(),
  enablePullRequestAutoMerge: vi.fn(),
  markPullRequestReady: vi.fn(),
  pullRequestPendingReview: vi.fn(),
  submitPullRequestReview: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import CodeReviewSagePage from '../apps/code-review-sage/CodeReviewSagePage'

const PR_URL = 'https://github.com/acme/widgets/pull/12'

const source: PullRequestSource = {
  provider: 'github',
  url: PR_URL,
  number: 12,
  title: 'Add source tabs',
  description: 'Summary.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2026-08-05T12:00:00Z',
  headBranch: 'feature/tabs',
  baseBranch: 'main',
  headSha: 'abcdef123456',
  author: 'octocat',
  additions: 3,
  deletions: 1,
  changedFiles: 1,
  files: [{ path: 'src/a.ts', status: 'modified', additions: 3, deletions: 1, patch: '@@ -1 +1 @@\n-a\n+b' }],
  commits: [],
  checks: [],
  comments: [],
}

// The page reads its own app endpoints with bare fetch, so only those are stubbed
// here; every pull-request read/write goes through the mocked api client.
// `/runs` is reset in beforeEach because a test that needs an unfinished run
// rewrites it, and leaking that into the next test would make the suite
// order-dependent.
const DONE_RUNS = {
  runs: [{
    run_id: 'r1',
    status: 'done',
    changes: [PR_URL],
    change_ids: ['GH-acme-widgets-12'],
    progress: { 'GH-acme-widgets-12': { phase: 'done', counts: { red: 1, yellow: 2 } } },
  }],
}

const SAGE_PAYLOADS: Record<string, unknown> = {
  '/runs': DONE_RUNS,
  '/settings': {
    settings: { model: null, effort: '', active_namespaces: ['default'], max_concurrent: 5 },
    models: [], efforts: ['low'], namespaces: ['default'], max_concurrent_max: 30,
  },
  '/namespaces': { namespaces: [{ name: 'default', patterns: 0, candidate: 0, active: true }], active: ['default'] },
  '/learnings': { namespace: 'default', patterns: [], candidate: [] },
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CodeReviewSagePage />
    </QueryClientProvider>,
  )
}

/** Open the reviewed PR's detail by clicking its run row. */
async function openReviewedPr() {
  renderPage()
  const row = await screen.findByRole('button', { name: /acme\/widgets #12/ })
  fireEvent.click(row)
  return row
}

beforeEach(() => {
  SAGE_PAYLOADS['/runs'] = DONE_RUNS
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.pullRequestSource.mockResolvedValue(source)
  mockApi.pullRequestChecks.mockResolvedValue({ checks: [] })
  mockApi.pullRequestStatuses.mockResolvedValue({ statuses: {} })
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  mockApi.submitPullRequestReview.mockResolvedValue({ submitted: true, event: 'APPROVE' })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const match = Object.keys(SAGE_PAYLOADS).find(suffix => url.includes(suffix))
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(match ? SAGE_PAYLOADS[match] : {}),
    } as Response)
  }))
})

afterEach(() => { vi.unstubAllGlobals() })

it('publishes the draft with the review id it was shown', async () => {
  await openReviewedPr()
  const approve = await screen.findByRole('button', { name: 'Approve' })
  fireEvent.click(approve)
  await waitFor(() => expect(mockApi.submitPullRequestReview).toHaveBeenCalledTimes(1))
  // The id comes from the fetched draft, never a blank or assumed value — a blank
  // id would make the backend resolve whatever draft exists instead.
  expect(mockApi.submitPullRequestReview).toHaveBeenCalledWith(PR_URL, '4242', 'APPROVE', 'd1')
  // The bar collapses to the outcome: the draft is gone, so re-publishing it must
  // not be offered.
  expect(await screen.findByText('Published as')).toBeTruthy()
  // Past tense, not the imperative button label ("Published as Approve").
  expect(screen.getByText('approved')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('shows the draft body before offering to publish it', async () => {
  await openReviewedPr()
  // Publishing is irreversible, and a pending review may be one the human started
  // by hand — the contents must be on screen before a verdict button is.
  expect(await screen.findByText('[code-review-sage] draft')).toBeTruthy()
})

it('offers all three verdicts for a pending draft', async () => {
  await openReviewedPr()
  expect(await screen.findByRole('button', { name: 'Submit as comment' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Request changes' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
})

it('withholds publishing while the row is still being reviewed', async () => {
  // A re-review leaves the PREVIOUS run's draft on the pull request until the new
  // one posts, so publishing from a running row would irreversibly submit obsolete
  // findings. Reading the draft stays available; only the verdicts are withheld.
  SAGE_PAYLOADS['/runs'] = {
    runs: [{
      run_id: 'r2',
      status: 'running',
      changes: [PR_URL],
      change_ids: ['GH-acme-widgets-12'],
      progress: { 'GH-acme-widgets-12': { phase: 'deep' } },
    }],
  }
  await openReviewedPr()
  expect(await screen.findByText(/this review is still running/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Request changes' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Submit as comment' })).toBeNull()
  // The draft itself is still shown — the row is readable, just not publishable.
  expect(screen.getByText('[code-review-sage] draft')).toBeTruthy()
})

it('shows no publish controls when the pull request has no draft', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '', body: '', commitId: '', headSha: '',
    stale: false, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  await openReviewedPr()
  expect(await screen.findByText('No draft review on this pull request.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('reports a failed draft read as a failure, never as "no draft"', async () => {
  // The read is the only thing that knows whether a draft exists, so rendering its
  // failure as an absence tells the user their pending review is gone. `retry:false`
  // means the retry button is the sole way back.
  mockApi.pullRequestPendingReview.mockRejectedValue(new Error('gh: not authenticated'))
  await openReviewedPr()
  expect(await screen.findByText(/Couldn't check for a draft review/)).toBeTruthy()
  expect(screen.queryByText('No draft review on this pull request.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()

  const retry = screen.getByRole('button', { name: 'Try again' })
  const before = mockApi.pullRequestPendingReview.mock.calls.length
  fireEvent.click(retry)
  await waitFor(() =>
    expect(mockApi.pullRequestPendingReview.mock.calls.length).toBeGreaterThan(before))
})

it('blocks publishing a draft whose text needs redaction', async () => {
  // The body renders redacted, but submitting would post GitHub's original text —
  // so the buttons must be absent, not merely fail on click.
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: 'use [REDACTED] to deploy',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: true, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  await openReviewedPr()
  expect(await screen.findByText(/must be redacted/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Submit as comment' })).toBeNull()
})

it('blocks publishing a draft written against an earlier commit', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    commitId: 'aaaaaaaaaaaa', headSha: 'abcdef123456',
    stale: true, contentRedacted: false, autoMergeArmed: false, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  await openReviewedPr()
  expect(await screen.findByText(/written against an earlier commit/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
})

it('surfaces a rejected publish instead of claiming success', async () => {
  mockApi.submitPullRequestReview.mockRejectedValue(
    new Error('This draft review is no longer pending -- it was already submitted or replaced.'),
  )
  await openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  expect(await screen.findByText(/no longer pending/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
})

it('does not read a draft until a pull request is opened', async () => {
  renderPage()
  await screen.findByRole('button', { name: /acme\/widgets #12/ })
  expect(mockApi.pullRequestPendingReview).not.toHaveBeenCalled()
})

it('withholds Approve while auto-merge is armed, and says why', async () => {
  // The one case the post-submit stale-head dismissal cannot repair: an approval
  // can satisfy branch protection and let GitHub merge before the dismissal lands.
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: true, contentDigest: 'd1', staleDismissalEnabled: true,
  })
  await openReviewedPr()
  expect(await screen.findByText(/Approve is unavailable while auto-merge is armed/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  // The non-gating verdicts stay available — only APPROVE can let a merge through.
  expect(screen.getByRole('button', { name: 'Submit as comment' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Request changes' })).toBeTruthy()
})

it('sends the digest of the draft it displayed, not a blank', async () => {
  // A blank digest would make the backend skip the content check entirely, so the
  // UI must forward the one it rendered.
  await openReviewedPr()
  fireEvent.click(await screen.findByRole('button', { name: 'Submit as comment' }))
  await waitFor(() => expect(mockApi.submitPullRequestReview).toHaveBeenCalled())
  const args = mockApi.submitPullRequestReview.mock.calls[0]
  expect(args[3]).toBe('d1')
})

it('withholds Approve when the base branch keeps stale approvals', async () => {
  mockApi.pullRequestPendingReview.mockResolvedValue({
    reviewId: '4242', body: '[code-review-sage] draft',
    commitId: 'abcdef123456', headSha: 'abcdef123456',
    stale: false, contentRedacted: false, autoMergeArmed: false,
    contentDigest: 'd1', staleDismissalEnabled: false,
  })
  await openReviewedPr()
  expect(await screen.findByText(/does not dismiss approvals when new commits/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Submit as comment' })).toBeTruthy()
})
