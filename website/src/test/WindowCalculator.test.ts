// Feature: chat-virtualizer
//
// Property tests for WindowCalculator covering:
// - Property 1: Window Correctness Invariant
// - Property 5: Jump Navigation Window Placement
// Plus targeted unit tests for edge cases the property tests don't reach
// (empty list, scroll past end, getOffset/getIndexAtOffset round-trip).

import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import {
  computeWindow,
  computeJumpWindow,
  getOffset,
  getIndexAtOffset,
  getTotalHeight,
} from '../hooks/virtualizer/WindowCalculator'

// Arbitrary: an item heights array of plausible chat message sizes.
const heightsArb = fc.array(fc.integer({ min: 20, max: 800 }), { minLength: 1, maxLength: 200 })

// Feature: chat-virtualizer, Property 1: Window Correctness Invariant
// **Validates: Requirements 2.1, 2.2, 2.3**
describe('Property 1: Window Correctness Invariant', () => {
  it('every item overlapping the viewport is inside [start, end)', () => {
    fc.assert(
      fc.property(
        heightsArb,
        fc.integer({ min: 0, max: 50_000 }),       // scrollTop
        fc.integer({ min: 100, max: 2000 }),       // viewportHeight
        fc.integer({ min: 0, max: 20 }),           // overscan
        (heights, scrollTop, viewportHeight, overscan) => {
          const getH = (i: number) => heights[i]
          const total = heights.reduce((a, b) => a + b, 0)
          // Clamp scroll to total content range so we don't always test the
          // "past end" branch (still tested separately below).
          const top = Math.min(scrollTop, Math.max(0, total - 1))
          const bottom = top + viewportHeight

          const { start, end } = computeWindow(top, viewportHeight, heights.length, getH, overscan)

          // Range bounds.
          expect(start).toBeGreaterThanOrEqual(0)
          expect(end).toBeLessThanOrEqual(heights.length)
          expect(start).toBeLessThanOrEqual(end)

          // Compute each item's [top, bottom] in content coordinates and verify
          // every visually-overlapping item is inside the mounted range.
          let off = 0
          for (let i = 0; i < heights.length; i++) {
            const itemTop = off
            const itemBottom = off + heights[i]
            const overlaps = itemBottom > top && itemTop < bottom
            if (overlaps) {
              expect(i).toBeGreaterThanOrEqual(start)
              expect(i).toBeLessThan(end)
            }
            off = itemBottom
          }
        },
      ),
      { numRuns: 100 },
    )
  })
})

// Feature: chat-virtualizer, Property 5: Jump Navigation Window Placement
// **Validates: Requirements 4.1, 5.1**
describe('Property 5: Jump Navigation Window Placement', () => {
  it('computeJumpWindow always contains the target index', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 5000 }),       // itemCount
        fc.integer({ min: 0, max: 4999 }),       // raw target (clamped inside)
        fc.integer({ min: 0, max: 50 }),         // overscan
        (itemCount, rawTarget, overscan) => {
          const target = rawTarget % itemCount
          const { start, end } = computeJumpWindow(target, itemCount, overscan)
          expect(start).toBeLessThanOrEqual(target)
          expect(target).toBeLessThan(end)
          // Window size is at most 2*overscan + 1 (clamped at edges).
          expect(end - start).toBeLessThanOrEqual(2 * overscan + 1)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('jump-to-last-index returns window ending at itemCount', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 1000 }),
        fc.integer({ min: 0, max: 20 }),
        (itemCount, overscan) => {
          const { end } = computeJumpWindow(itemCount - 1, itemCount, overscan)
          expect(end).toBe(itemCount)
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Round-trip property: getOffset and getIndexAtOffset agree on item boundaries.
describe('Property: offset round-trip', () => {
  it('getIndexAtOffset(getOffset(i)) === i for every i in [0, itemCount)', () => {
    fc.assert(
      fc.property(
        fc.array(fc.integer({ min: 1, max: 500 }), { minLength: 1, maxLength: 100 }),
        (heights) => {
          const getH = (i: number) => heights[i]
          for (let i = 0; i < heights.length; i++) {
            const off = getOffset(i, heights.length, getH)
            // At the exact top edge of item i, we want item i (not i-1).
            expect(getIndexAtOffset(off, heights.length, getH)).toBe(i)
          }
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Targeted unit tests for edge cases.

describe('computeWindow edge cases', () => {
  it('returns {0,0} for empty list', () => {
    expect(computeWindow(0, 500, 0, () => 100, 5)).toEqual({ start: 0, end: 0 })
  })

  it('handles scroll past end by showing the tail', () => {
    const heights = [100, 100, 100]
    const { start, end } = computeWindow(10_000, 500, 3, (i) => heights[i], 1)
    // Tail item must be inside the window.
    expect(start).toBeLessThanOrEqual(2)
    expect(end).toBe(3)
  })

  it('respects overscan at start of list (clamps to 0)', () => {
    const heights = [50, 50, 50, 50, 50]
    const { start, end } = computeWindow(0, 100, 5, (i) => heights[i], 10)
    // Two items visible (indices 0,1); overscan would push start negative
    // but it's clamped to 0.
    expect(start).toBe(0)
    expect(end).toBe(5) // overscan extends past end too, clamped to itemCount
  })

  it('zero-height items are tolerated', () => {
    const heights = [100, 0, 0, 100]
    const { start, end } = computeWindow(0, 50, 4, (i) => heights[i], 0)
    expect(start).toBe(0)
    // The first 0-height item lives inside item 0's bottom edge so it
    // overlaps the viewport top edge — it must be in the window.
    expect(end).toBeGreaterThan(0)
  })
})

describe('getOffset / getIndexAtOffset', () => {
  it('getOffset(0) === 0', () => {
    expect(getOffset(0, 5, () => 100)).toBe(0)
  })

  it('getOffset(itemCount) === total', () => {
    expect(getOffset(5, 5, () => 100)).toBe(500)
  })

  it('getIndexAtOffset clamps to last index when past the end', () => {
    expect(getIndexAtOffset(99_999, 3, () => 100)).toBe(2)
  })

  it('getIndexAtOffset returns 0 for empty list', () => {
    expect(getIndexAtOffset(0, 0, () => 100)).toBe(0)
  })

  it('getTotalHeight sums all item heights', () => {
    expect(getTotalHeight(4, (i) => (i + 1) * 10)).toBe(10 + 20 + 30 + 40)
  })
})
