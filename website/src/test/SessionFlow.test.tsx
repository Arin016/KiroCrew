/**
 * Session Flow header: the spawn-tree view that gives the Context Breakdown
 * panel the axis zoetrope has (who spawned whom) plus the one it doesn't (each
 * node's context composition).
 *
 *  - the header renders NOTHING when the session spawned no sub-agents — a
 *    session with no tree has none to draw, and the per-turn rows tell its story.
 *  - a sub-agent's coarse lifecycle collapses to the three states a reader acts
 *    on (running / done / ended), so pending/tool/stopped are not their own pills.
 *  - the main node's context bar is composed from the SAME grouped, ranked
 *    totals the rows use, so a block is one proportion in both places.
 *  - nodes render in spawn order (by startedAt), the order a fan-out is scanned.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import { SessionFlow, mainNodeSegments } from '../pages/SessionFlow'
import { buildColorMap, type ContextTrace } from '../pages/ContextBreakdownPanel'
import type { SubagentActivity } from '../types'

afterEach(cleanup)

const trace = (over: Partial<ContextTrace> = {}): ContextTrace => ({
  slot: 'chat-1',
  turns: [
    {
      ts: '2026-08-26T00:00:00Z',
      phase: 'per_turn',
      blocks: { loaded_skill: 8000, memory: 2000, your_message: 100 },
      total_chars: 10100,
      context_used: 12000,
      context_window: 200000,
      model: 'claude-opus-5',
    },
  ],
  totals: { loaded_skill: 8000, memory: 2000, your_message: 100 },
  injected_chars: 10100,
  user_chars: 100,
  estimated_other_chars: 0,
  peak_context_used: 12000,
  context_window: 200000,
  window_days: 14,
  ...over,
})

const sub = (over: Partial<SubagentActivity> = {}): SubagentActivity => ({
  id: 'sub-1',
  task: 'read the specs',
  agent: 'gpt-review',
  status: 'done',
  streaming: '',
  lastTool: '',
  startedAt: 1000,
  elapsed: 4200,
  ...over,
})

describe('SessionFlow', () => {
  it('renders nothing when the session spawned no sub-agents', () => {
    const { container } = render(<SessionFlow trace={trace()} subagents={{}} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when there is no trace, even with sub-agents', () => {
    const { container } = render(
      <SessionFlow trace={null} subagents={{ 'sub-1': sub() }} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('draws the header with sub-agent count and running count', () => {
    render(
      <SessionFlow
        trace={trace()}
        subagents={{
          a: sub({ id: 'a', status: 'running', startedAt: 1 }),
          b: sub({ id: 'b', status: 'done', startedAt: 2 }),
        }}
      />,
    )
    // "2 sub-agents · 1 running" — assert the header meta text uniquely
    // (a bare /2/ also matches the main node's turn count).
    expect(screen.getByText(/2 sub-agents/i)).toBeTruthy()
    expect(screen.getByText(/1 running/i)).toBeTruthy()
  })

  it('lists sub-agent nodes in spawn order (by startedAt)', () => {
    render(
      <SessionFlow
        trace={trace()}
        subagents={{
          late: sub({ id: 'late', agent: 'zeta-late', startedAt: 900 }),
          early: sub({ id: 'early', agent: 'alpha-early', startedAt: 100 }),
        }}
      />,
    )
    const names = screen.getAllByText(/-(late|early)$/).map(n => n.textContent)
    expect(names).toEqual(['alpha-early', 'zeta-late'])
  })

  it('shows a failed sub-agent as ended and a running one as running', () => {
    render(
      <SessionFlow
        trace={trace()}
        subagents={{
          a: sub({ id: 'a', agent: 'boom', status: 'error', startedAt: 1 }),
          b: sub({ id: 'b', agent: 'live', status: 'tool', startedAt: 2 }),
        }}
      />,
    )
    // 'error' and 'stopped' both collapse to the single "ended" pill; tool ->
    // running. Two distinct pills must be present.
    expect(screen.getByText(/ended/i)).toBeTruthy()
    expect(screen.getAllByText(/running/i).length).toBeGreaterThan(0)
  })
})

describe('mainNodeSegments', () => {
  const colorOf = (label: string) =>
    buildColorMap(trace().totals).get(label) ?? { fill: 'var(--ctx-k1)' }

  it('composes segments from grouped totals, user slice last', () => {
    const segs = mainNodeSegments(trace(), colorOf)
    // loaded_skill (8000) > memory (2000) come first ranked, your_message last.
    expect(segs.map(s => s.key)).toEqual(['loaded_skill', 'memory', 'your_message'])
    expect(segs[segs.length - 1].isUser).toBe(true)
  })

  it('segment percentages sum to ~100', () => {
    const segs = mainNodeSegments(trace(), colorOf)
    const total = segs.reduce((a, s) => a + s.pct, 0)
    expect(Math.round(total)).toBe(100)
  })

  it('returns nothing for an empty totals map', () => {
    expect(mainNodeSegments(trace({ totals: {} }), colorOf)).toEqual([])
  })

  it('omits the user slice when the session has no user chars', () => {
    const segs = mainNodeSegments(
      trace({ totals: { loaded_skill: 8000, memory: 2000 } }),
      colorOf,
    )
    expect(segs.some(s => s.isUser)).toBe(false)
  })
})
