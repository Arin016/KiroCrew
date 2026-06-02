import { describe, it, expect } from 'vitest'
import type { DisplayItem } from '../pages/chat/types'
import { findPrevUserMsgDisplayIdx } from '../utils/findPrevUserMsgDisplayIdx'

const user = (content: string, idx: number): DisplayItem => ({
  kind: 'single',
  msg: { role: 'user', content, cls: '' },
  idx,
})

const assistant = (content: string, idx: number): DisplayItem => ({
  kind: 'single',
  msg: { role: 'assistant', content, cls: '' },
  idx,
})

const turnBlock = (): DisplayItem => ({
  kind: 'turn',
  items: [{ kind: 'single', msg: { role: 'assistant', content: 'a', cls: '' }, idx: 0 }],
  complete: true,
})

describe('findPrevUserMsgDisplayIdx', () => {
  it('returns -1 for an empty list', () => {
    expect(findPrevUserMsgDisplayIdx([], 0)).toBe(-1)
  })

  it('returns -1 when there is no user message', () => {
    expect(findPrevUserMsgDisplayIdx([assistant('a1', 0), turnBlock()], 2)).toBe(-1)
  })

  it('finds the user message directly above the viewport top', () => {
    const items = [user('q1', 0), assistant('a1', 1), user('q2', 2), assistant('a2', 3)]
    expect(findPrevUserMsgDisplayIdx(items, 3)).toBe(2)
  })

  it('excludes the viewport-top item itself (strictly above)', () => {
    const items = [user('q1', 0), user('q2', 1), assistant('a', 2)]
    // topmost=1 is q2; "strictly above" means we skip it and find q1.
    expect(findPrevUserMsgDisplayIdx(items, 1)).toBe(0)
  })

  it('returns -1 when the only user message is at or below the viewport top', () => {
    const items = [user('q1', 0), assistant('a', 1)]
    expect(findPrevUserMsgDisplayIdx(items, 0)).toBe(-1)
  })

  it('chains: after each jump, the next call finds an earlier message', () => {
    const items = [
      user('q1', 0),
      assistant('a1', 1),
      user('q2', 2),
      assistant('a2', 3),
      user('q3', 4),
    ]
    // First click from bottom (topmost=4, showing q3) → q2
    expect(findPrevUserMsgDisplayIdx(items, 4)).toBe(2)
    // Click again from q2 (topmost=2) → q1
    expect(findPrevUserMsgDisplayIdx(items, 2)).toBe(0)
    // Click again from q1 (topmost=0) → nothing above
    expect(findPrevUserMsgDisplayIdx(items, 0)).toBe(-1)
  })

  it('skips turn blocks (user messages only live in top-level singles)', () => {
    const items = [user('q1', 0), turnBlock(), turnBlock()]
    expect(findPrevUserMsgDisplayIdx(items, 2)).toBe(0)
  })

  it('skips group items', () => {
    const group: DisplayItem = {
      kind: 'group',
      msgs: [{ role: 'tool', content: 'x', cls: '' }],
      startIdx: 1,
    }
    const items = [user('q1', 0), group, assistant('a', 2)]
    expect(findPrevUserMsgDisplayIdx(items, 2)).toBe(0)
  })

  it('clamps topmostVisibleIdx beyond array length', () => {
    const items = [user('q1', 0), assistant('a', 1)]
    expect(findPrevUserMsgDisplayIdx(items, 999)).toBe(0)
  })

  it('handles negative topmostVisibleIdx as "no lookup possible"', () => {
    const items = [user('q1', 0), assistant('a', 1)]
    expect(findPrevUserMsgDisplayIdx(items, -1)).toBe(-1)
  })
})
