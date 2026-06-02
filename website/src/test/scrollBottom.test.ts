/**
 * Tests for the scrollToTrueBottom helper used by ChatPage's "Bottom" pill
 * and auto-scroll callers (Mesh-747 bug 2).
 *
 * - User-clicked Bottom pill calls with instant=true -> behavior 'auto', no
 *   page-by-page animation when scrolled far up.
 * - Organic auto-scrolls (streaming/tab-switch/slot-start) call with no arg
 *   -> behavior 'smooth', preserving the existing animation UX.
 * - In both cases, the helper iterates a scrollBy nudge over ~30 frames so
 *   the scroller keeps re-targeting the bottom as Virtuoso lazy-expands
 *   intermediate items and scrollHeight grows. A single rAF nudge lands
 *   short of the true bottom on long histories.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { VirtuosoHandle } from 'react-virtuoso'
import { scrollToTrueBottom } from '../pages/chat/scrollBottom'

// Stand-in for VirtuosoHandle. The helper only invokes scrollToIndex/scrollBy,
// so a minimal Pick keeps strict typing without re-implementing the full handle.
const makeFakeVirtuoso = () => {
  const fake = {
    scrollToIndex: vi.fn(),
    scrollBy: vi.fn(),
  }
  return { fake, handle: fake as unknown as VirtuosoHandle }
}

// Drive rAF synchronously so all settle frames run in-test.
const installSyncRaf = () => vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 0 })

describe('scrollToTrueBottom', () => {
  let rafSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    rafSpy = installSyncRaf()
  })

  it('instant=true: jumps with behavior "auto" then iterates nudges past the Footer', () => {
    const { fake, handle } = makeFakeVirtuoso()
    scrollToTrueBottom(handle, true)

    expect(fake.scrollToIndex).toHaveBeenCalledTimes(1)
    expect(fake.scrollToIndex).toHaveBeenCalledWith({ index: 'LAST', behavior: 'auto', align: 'end' })
    // Multiple nudge frames -- not just one -- so the scroller keeps catching
    // up with Virtuoso's lazy-render-driven scrollHeight growth.
    expect(fake.scrollBy.mock.calls.length).toBeGreaterThan(1)
    for (const [arg] of fake.scrollBy.mock.calls) {
      expect(arg.behavior).toBe('auto')
      expect(arg.top).toBeGreaterThan(100_000)
    }
  })

  it('default (instant omitted): smooth scroll for organic auto-callers', () => {
    const { fake, handle } = makeFakeVirtuoso()
    scrollToTrueBottom(handle)

    expect(fake.scrollToIndex).toHaveBeenCalledWith({ index: 'LAST', behavior: 'smooth', align: 'end' })
    expect(fake.scrollBy.mock.calls.length).toBeGreaterThan(1)
    for (const [arg] of fake.scrollBy.mock.calls) {
      expect(arg.behavior).toBe('smooth')
    }
  })

  it('instant=false: smooth (explicit form)', () => {
    const { fake, handle } = makeFakeVirtuoso()
    scrollToTrueBottom(handle, false)

    expect(fake.scrollToIndex).toHaveBeenCalledWith({ index: 'LAST', behavior: 'smooth', align: 'end' })
    expect(fake.scrollBy.mock.calls[0][0].behavior).toBe('smooth')
  })

  it('null virtuoso: no-op, no throw', () => {
    expect(() => scrollToTrueBottom(null, true)).not.toThrow()
    // Without a virtuoso ref, no rAF is ever scheduled.
    expect(rafSpy).not.toHaveBeenCalled()
  })

  it('settle is bounded: stops scheduling rAF after a fixed frame budget', () => {
    // Cap rAF recursion at a high but finite count. If the helper had an
    // unbounded loop this test would explode; bounded settle terminates.
    let rafCount = 0
    rafSpy.mockImplementation((cb) => {
      rafCount += 1
      if (rafCount > 100) return 0  // safety stop -- should never hit
      cb(0)
      return rafCount
    })
    const { fake, handle } = makeFakeVirtuoso()
    scrollToTrueBottom(handle, true)

    expect(rafCount).toBeLessThan(100)
    // Each rAF frame fires one scrollBy.
    expect(fake.scrollBy.mock.calls.length).toBe(rafCount)
  })

  it('initial jump fires before the first nudge frame', () => {
    const { fake, handle } = makeFakeVirtuoso()
    scrollToTrueBottom(handle, true)
    // scrollToIndex first (this frame), scrollBy after rAF (next frames).
    expect(fake.scrollToIndex.mock.invocationCallOrder[0])
      .toBeLessThan(fake.scrollBy.mock.invocationCallOrder[0])
  })
})
