// Pure-function windowing math for the chat virtualizer.
//
// Given a scroll position, viewport height, item count, and a per-item
// height getter, compute which contiguous range of items must be mounted
// (visible items + overscan above/below). Also exposes `getOffset` (sum
// of heights up to an index) and `getIndexAtOffset` (inverse).
//
// Linear scan with early termination: O(N) worst case but amortized O(K)
// where K is the visible window. For 1000 items at ~80px average, this is
// <1ms. If profiling ever shows issues at 5000+ items, swap in a
// prefix-sum tree.

export interface WindowRange {
  /** First index to mount (inclusive). */
  start: number
  /** One past the last index to mount (exclusive). */
  end: number
}

/** Height getter — must return a non-negative finite number for every index in [0, count). */
export type HeightGetter = (index: number) => number

/**
 * Compute the mounted window range for a scroll state.
 *
 * The returned range satisfies:
 *   - 0 ≤ start ≤ end ≤ itemCount
 *   - All visible items (those overlapping the viewport rect) are inside
 *     [start, end), with `overscan` extra items on each side (clamped to bounds).
 *   - For empty lists (itemCount === 0), returns { start: 0, end: 0 }.
 */
export function computeWindow(
  scrollTop: number,
  viewportHeight: number,
  itemCount: number,
  getHeight: HeightGetter,
  overscan: number,
): WindowRange {
  if (itemCount <= 0) return { start: 0, end: 0 }
  // Defensive clamps — pathological inputs (negative scroll, NaN height)
  // shouldn't crash the renderer.
  const top = Math.max(0, scrollTop)
  const bottom = top + Math.max(0, viewportHeight)
  const overscanN = Math.max(0, Math.floor(overscan))

  // Walk forward accumulating heights. Track the first index whose bottom
  // edge passes `top` (firstVisible) and the first index whose top edge
  // passes `bottom` (firstAfter).
  let offset = 0
  let firstVisible = -1
  let firstAfter = -1
  for (let i = 0; i < itemCount; i++) {
    const h = Math.max(0, getHeight(i) || 0)
    const itemBottom = offset + h
    if (firstVisible === -1 && itemBottom > top) {
      firstVisible = i
    }
    if (firstVisible !== -1 && offset >= bottom) {
      firstAfter = i
      break
    }
    offset = itemBottom
  }
  // Edge case: scroll position is past the end of all content. Show the
  // tail (last item plus overscan above) so the user sees content.
  if (firstVisible === -1) firstVisible = itemCount - 1
  if (firstAfter === -1) firstAfter = itemCount

  const start = Math.max(0, firstVisible - overscanN)
  const end = Math.min(itemCount, firstAfter + overscanN)
  return { start, end }
}

/**
 * Compute the cumulative pixel offset at the top edge of `index`.
 *
 * For index === 0, returns 0. For index === itemCount, returns the total
 * content height. Out-of-range indices are clamped.
 */
export function getOffset(
  index: number,
  itemCount: number,
  getHeight: HeightGetter,
): number {
  const target = Math.max(0, Math.min(itemCount, Math.floor(index)))
  let offset = 0
  for (let i = 0; i < target; i++) {
    offset += Math.max(0, getHeight(i) || 0)
  }
  return offset
}

/**
 * Inverse of getOffset: find the index whose vertical span contains `pixelOffset`.
 *
 * Returns the largest index `i` such that the cumulative height of items
 * 0..i-1 is ≤ pixelOffset. For pixelOffset ≥ totalHeight returns
 * itemCount - 1 (clamped). For empty lists returns 0.
 */
export function getIndexAtOffset(
  pixelOffset: number,
  itemCount: number,
  getHeight: HeightGetter,
): number {
  if (itemCount <= 0) return 0
  const target = Math.max(0, pixelOffset)
  let offset = 0
  for (let i = 0; i < itemCount; i++) {
    const h = Math.max(0, getHeight(i) || 0)
    if (offset + h > target) return i
    offset += h
  }
  return itemCount - 1
}

/**
 * Compute a window centered on `targetIndex` for jump navigation.
 *
 * The returned range always includes `targetIndex` (provided 0 ≤ targetIndex < itemCount)
 * and has approximately `2 * overscan + 1` items, clamped to list bounds.
 */
export function computeJumpWindow(
  targetIndex: number,
  itemCount: number,
  overscan: number,
): WindowRange {
  if (itemCount <= 0) return { start: 0, end: 0 }
  const t = Math.max(0, Math.min(itemCount - 1, Math.floor(targetIndex)))
  const overscanN = Math.max(0, Math.floor(overscan))
  const start = Math.max(0, t - overscanN)
  const end = Math.min(itemCount, t + overscanN + 1)
  return { start, end }
}

/** Sum of all item heights — used to size the scroll container. */
export function getTotalHeight(itemCount: number, getHeight: HeightGetter): number {
  let total = 0
  for (let i = 0; i < itemCount; i++) {
    total += Math.max(0, getHeight(i) || 0)
  }
  return total
}

/**
 * Expand the window upward by `overscan` items, clamped at 0.
 *
 * Pure: returns the same object identity if start is already 0, otherwise
 * a new range. Used by the top-sentinel IntersectionObserver to load
 * older items as the user scrolls up.
 */
export function expandWindowUp(
  range: WindowRange,
  overscan: number,
): WindowRange {
  if (range.start === 0) return range
  const newStart = Math.max(0, range.start - Math.max(0, overscan))
  return { start: newStart, end: range.end }
}

/**
 * Expand the window downward by `overscan` items, clamped at itemCount.
 *
 * Pure: returns the same object identity if end is already itemCount,
 * otherwise a new range. Used by the bottom-sentinel
 * IntersectionObserver to load newer items in jump-mode scenarios.
 */
export function expandWindowDown(
  range: WindowRange,
  itemCount: number,
  overscan: number,
): WindowRange {
  if (range.end >= itemCount) return range
  const newEnd = Math.min(itemCount, range.end + Math.max(0, overscan))
  return { start: range.start, end: newEnd }
}
