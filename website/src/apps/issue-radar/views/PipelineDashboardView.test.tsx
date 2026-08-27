/**
 * PipelineDashboardView — the seam that hands Issue Radar's active repository to the
 * pipeline views.
 *
 * What is pinned here is the one thing that is NOT visible from either child: that a
 * repository switch REMOUNTS them rather than re-rendering them. Each child holds
 * drill-down state scoped to one repository (the fold view an open step and item, the
 * lanes view a held lane), and the L2 sessions query is keyed on the issue NUMBER
 * alone because the dispatch queue is a number->entry map. So an item left expanded
 * across a repository switch renders the previous repository's sessions and costs
 * under the new repository's name.
 *
 * Asserted by counting MOUNTS of stubbed children and by checking that state they
 * hold is discarded -- not by inspecting a React `key`, which is not observable.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const active = { owner: 'acme', repo: 'alpha', provider: 'github', host: 'github.com' }

// `vi.mock` factories are hoisted above module-level consts, so anything they close
// over has to come from `vi.hoisted` -- including the mutable active-repo holder the
// context mock reads.
const { state, foldMounts, laneMounts } = vi.hoisted(() => ({
  state: { active: { owner: 'acme', repo: 'alpha', provider: 'github', host: 'github.com' } },
  foldMounts: [] as string[],
  laneMounts: [] as string[],
}))

vi.mock('../context', () => ({
  useIssueRadar: () => state,
}))

vi.mock('../pipeline/views/GlobalPipelineView', async () => {
  const { useState } = await import('react')
  // Named and capitalized: an anonymous arrow assigned to `default` is not
  // recognizable as a component, and rules-of-hooks rejects the hook call inside it.
  function FoldStub({ repo }: { repo: { owner: string; repo: string } }) {
    foldMounts.push(`fold:${repo.owner}/${repo.repo}`)
    const [open, setOpen] = useState(false)
    return (
      <div>
        <span data-testid="fold-repo">{`${repo.owner}/${repo.repo}`}</span>
        <button onClick={() => setOpen(true)}>expand-fold</button>
        <span data-testid="fold-open">{open ? 'open' : 'closed'}</span>
      </div>
    )
  }
  return { default: FoldStub }
})

vi.mock('../pipeline/views/PipelineView', async () => {
  const { useState } = await import('react')
  function LanesStub({ repo }: { repo: { owner: string; repo: string } }) {
    laneMounts.push(`lanes:${repo.owner}/${repo.repo}`)
    const [open, setOpen] = useState(false)
    return (
      <div>
        <span data-testid="lanes-repo">{`${repo.owner}/${repo.repo}`}</span>
        <button onClick={() => setOpen(true)}>expand-lanes</button>
        <span data-testid="lanes-open">{open ? 'open' : 'closed'}</span>
      </div>
    )
  }
  return { default: LanesStub }
})

import PipelineDashboardView from './PipelineDashboardView'

beforeEach(() => {
  foldMounts.length = 0
  laneMounts.length = 0
  state.active = { ...active }
})

describe('PipelineDashboardView — the active repository is the identity', () => {
  it('hands Issue Radar\u2019s active repository to the fold view', () => {
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-repo').textContent).toBe('acme/alpha')
  })

  it('REMOUNTS the fold view on a repository switch, discarding its drill-down', () => {
    const { rerender } = render(<PipelineDashboardView />)

    // Drill in, the way an operator would.
    fireEvent.click(screen.getByText('expand-fold'))
    expect(screen.getByTestId('fold-open').textContent).toBe('open')

    // Issue Radar's picker moves to another repository.
    state.active = { ...active, repo: 'beta' }
    rerender(<PipelineDashboardView />)

    expect(screen.getByTestId('fold-repo').textContent).toBe('acme/beta')
    // The open item is GONE. Without the remount it would still read "open", and the
    // number-keyed L2 query behind it would show the previous repository's sessions.
    expect(screen.getByTestId('fold-open').textContent).toBe('closed')
    expect(foldMounts).toContain('fold:acme/alpha')
    expect(foldMounts).toContain('fold:acme/beta')
  })

  it('treats provider and host as part of the identity, not decoration', () => {
    // The same slug on a different host is a DIFFERENT repository, so it must remount
    // too -- a key built from owner/repo alone would silently keep the old state.
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByText('expand-fold'))
    expect(screen.getByTestId('fold-open').textContent).toBe('open')

    state.active = { ...active, provider: 'gitlab', host: 'gitlab.example.com' }
    rerender(<PipelineDashboardView />)

    expect(screen.getByTestId('fold-open').textContent).toBe('closed')
  })

  it('remounts the lanes view on the same switch', () => {
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByText('Item lanes'))
    fireEvent.click(screen.getByText('expand-lanes'))
    expect(screen.getByTestId('lanes-open').textContent).toBe('open')

    state.active = { ...active, repo: 'beta' }
    rerender(<PipelineDashboardView />)

    expect(screen.getByTestId('lanes-open').textContent).toBe('closed')
  })

  it('does NOT remount when nothing about the repository changed', () => {
    // A poll tick or an unrelated context change must not throw away the operator's
    // open item -- that would be the opposite failure, and a key rebuilt from a fresh
    // object identity each render would cause it.
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByText('expand-fold'))
    state.active = { ...active }
    rerender(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-open').textContent).toBe('open')
  })
})
