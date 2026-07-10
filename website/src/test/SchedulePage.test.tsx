import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

// Covers the arm -> confirm -> delete -> revert state machine added in
// CR-287255321. This logic is on a destructive, irreversible action and
// had two real bugs (premature button re-enable before await load(),
// and confirmDeleteId not resetting on a failed delete) caught by AutoSDE
// during review -- these tests lock in both fixes.

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  ...overrides,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    deleteCron: vi.fn(),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    kiroclawAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKiroclawAgents: vi.fn().mockResolvedValue({}),
  },
}))

const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>{children}</MemoryRouter>
)

describe('SchedulePage delete button state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('arms on first click, deletes on second click', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockResolvedValue({})

    render(<SchedulePage />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteBtn = screen.getByRole('button', { name: 'Delete' })
    expect(api.deleteCron).not.toHaveBeenCalled()

    // First click arms the row -- button swaps label, no API call yet.
    fireEvent.click(deleteBtn)
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()

    // After delete, refresh the list to empty so the row disappears.
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    // Second click (now "Confirm") actually deletes.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.deleteCron).toHaveBeenCalledWith('job-1'))
  })

  it('reverts confirm state back to Delete if the delete call fails', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockRejectedValue(new Error('boom'))

    render(<SchedulePage />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const confirmBtn = await screen.findByRole('button', { name: 'Confirm' })
    fireEvent.click(confirmBtn)

    await waitFor(() => expect(api.deleteCron).toHaveBeenCalled())
    // Bug fix under test: even on failure, the button must revert out of
    // "Confirm" (previously it stayed stuck with no way to re-arm).
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument())
    expect(screen.getByText(/boom/)).toBeInTheDocument()
  })

  it('auto-reverts the armed state after the timeout if not confirmed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    render(<SchedulePage />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    await vi.advanceTimersByTimeAsync(3100)

    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('arming a different row disarms the previously armed row', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'job-1', name: 'Nightly report' }), mkJob({ id: 'job-2', name: 'Weekly digest' })],
    })

    render(<SchedulePage />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(deleteButtons[0])
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    // Arming row 2 must disarm row 1 -- only one row confirmable at a time.
    const remainingDeleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(remainingDeleteButtons[remainingDeleteButtons.length - 1])

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Confirm' })).toHaveLength(1)
    })
    expect(api.deleteCron).not.toHaveBeenCalled()
  })
})
