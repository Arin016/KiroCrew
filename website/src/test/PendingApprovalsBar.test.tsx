//
// Contract under test — the persistent pending-approvals bar.
//
// The bar exists for approvals with NO owning chat (background sources like the
// taskrunner and cron), which otherwise surface only in the notification feed.
// So it MUST:
//   - render nothing when there is nothing unowned pending
//   - count and list ONLY unowned rows (a `slot`-owned approval renders in its
//     chat; a `task-gate-*` approval owns the Projects nav badge)
//   - resolve a row via api.resolveApproval and drop it from the list
//   - NOT resurrect a row a concurrent refetch re-adds to the cache while its
//     resolve is in flight (the shared query is polled + websocket-invalidated)
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PendingApprovalsBar from '../components/PendingApprovalsBar'
import { api } from '../api/client'

type Approval = { id: string; source?: string; tool?: string; tool_input?: string; slot?: string }

/**
 * Mount with a server-like mutable list: `api.approvals` reads it and
 * `api.resolveApproval` removes the row, exactly as the real backend does — so
 * the post-resolve reconcile (invalidate → refetch) sees the row gone.
 */
function mountLive(seed: Approval[]) {
  const live = [...seed]
  vi.spyOn(api, 'approvals').mockImplementation(async () => [...live])
  const resolveSpy = vi.spyOn(api, 'resolveApproval').mockImplementation(async (id: string) => {
    const i = live.findIndex(a => a.id === id)
    if (i >= 0) live.splice(i, 1)
    return { ok: true }
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['global-approvals'], [...live])
  const utils = render(
    <QueryClientProvider client={qc}>
      <PendingApprovalsBar />
    </QueryClientProvider>,
  )
  return { qc, live, resolveSpy, ...utils }
}

/** Mount with static seed (no resolve wired) for pure render/filter assertions. */
function mountStatic(seed: Approval[]) {
  vi.spyOn(api, 'approvals').mockResolvedValue([...seed])
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnMount: false } },
  })
  qc.setQueryData(['global-approvals'], [...seed])
  const utils = render(
    <QueryClientProvider client={qc}>
      <PendingApprovalsBar />
    </QueryClientProvider>,
  )
  return { qc, ...utils }
}

const expand = () => fireEvent.click(screen.getByRole('button', { expanded: false }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('PendingApprovalsBar', () => {
  it('renders nothing when there are no unowned approvals', () => {
    mountStatic([])
    expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument()
  })

  it('renders nothing when the only pending approval is owned by a chat', () => {
    mountStatic([{ id: 'a1', tool: 'ls', slot: 'dashboard:default' }])
    expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument()
  })

  it('excludes project task-gate approvals', () => {
    mountStatic([{ id: 'task-gate-1', tool: 'ship' }])
    expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument()
  })

  it('shows the count of only unowned, non-task-gate approvals', () => {
    mountStatic([
      { id: 'u1', tool: 'ls', source: 'taskrunner' },
      { id: 'u2', tool: 'mkdir', source: 'cron' },
      { id: 'owned', tool: 'rm', slot: 'dashboard:default' },
      { id: 'task-gate-9', tool: 'ship' },
    ])
    const bar = screen.getByTestId('pending-approvals-bar')
    expect(within(bar).getByText('2')).toBeInTheDocument()
  })

  it('is collapsed by default and reveals rows on expand', () => {
    mountStatic([{ id: 'u1', tool: 'ls', source: 'taskrunner' }])
    expect(screen.queryByText('Approve')).not.toBeInTheDocument()
    expand()
    expect(screen.getByText('Approve')).toBeInTheDocument()
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('approves via the API and drops the row from the list', async () => {
    const { resolveSpy } = mountLive([
      { id: 'u1', tool: 'ls', source: 'taskrunner' },
      { id: 'u2', tool: 'mkdir', source: 'cron' },
    ])
    expand()
    fireEvent.click(screen.getAllByText('Approve')[0])
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith('u1', 'approve'))
    await waitFor(() => {
      const bar = screen.getByTestId('pending-approvals-bar')
      expect(within(bar).getByText('1')).toBeInTheDocument()
    })
  })

  it('rejects via the API with the reject action', async () => {
    const { resolveSpy } = mountLive([{ id: 'u1', tool: 'ls', source: 'taskrunner' }])
    expand()
    fireEvent.click(screen.getByText('Reject'))
    await waitFor(() => expect(resolveSpy).toHaveBeenCalledWith('u1', 'reject'))
    // Last unowned row resolved -> the whole bar leaves.
    await waitFor(() => {
      expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument()
    })
  })

  it('does not resurrect a row that a concurrent refetch re-adds mid-resolve', async () => {
    // A resolve that never settles keeps the row in the in-flight window.
    let settle: (v: { ok: boolean }) => void = () => {}
    vi.spyOn(api, 'approvals').mockResolvedValue([{ id: 'u1', tool: 'ls', source: 'taskrunner' }])
    vi.spyOn(api, 'resolveApproval').mockReturnValue(new Promise(r => { settle = r }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } })
    qc.setQueryData(['global-approvals'], [{ id: 'u1', tool: 'ls', source: 'taskrunner' }])
    render(
      <QueryClientProvider client={qc}>
        <PendingApprovalsBar />
      </QueryClientProvider>,
    )
    expand()
    fireEvent.click(screen.getByText('Approve'))
    // The bar (its last row) is hidden the instant the resolve starts.
    await waitFor(() => expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument())
    // A concurrent poll / websocket refetch writes the still-pending row back
    // into the shared cache. It must NOT reappear while the resolve is in flight.
    act(() => { qc.setQueryData(['global-approvals'], [{ id: 'u1', tool: 'ls', source: 'taskrunner' }]) })
    expect(screen.queryByTestId('pending-approvals-bar')).not.toBeInTheDocument()
    settle({ ok: true })
  })
})
