import { describe, it, expect, vi } from 'vitest'
import { pickSearchScrollBehavior, RAPID_STEP_MS, scrollCurrentMatchIntoView } from '../utils/searchScroll'

describe('pickSearchScrollBehavior', () => {
  it('snaps (auto) when stepping faster than the threshold', () => {
    expect(pickSearchScrollBehavior(1000, 900)).toBe('auto') // 100ms apart
    expect(pickSearchScrollBehavior(1000, 1000)).toBe('auto') // same instant
  })

  it('settles (smooth) when the gap is at or beyond the threshold', () => {
    expect(pickSearchScrollBehavior(1000, 1000 - RAPID_STEP_MS)).toBe('smooth') // exactly 250ms
    expect(pickSearchScrollBehavior(2000, 1000)).toBe('smooth') // 1s apart
  })

  it('settles on the first step (lastStepAt = 0)', () => {
    expect(pickSearchScrollBehavior(5000, 0)).toBe('smooth')
  })

  it('respects a custom threshold', () => {
    expect(pickSearchScrollBehavior(1000, 940, 50)).toBe('smooth') // 60ms >= 50
    expect(pickSearchScrollBehavior(1000, 970, 50)).toBe('auto')   // 30ms < 50
  })
})

describe('scrollCurrentMatchIntoView', () => {
  it('returns a cancel function so callers can abort the converge loop', () => {
    const cancel = scrollCurrentMatchIntoView(document.body, 2)
    expect(typeof cancel).toBe('function')
  })

  it('cancel is idempotent and safe to call (no throw, removes listeners)', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const cancel = scrollCurrentMatchIntoView(document.body, 5)
    expect(() => { cancel(); cancel() }).not.toThrow()
    // cancel() tears down the wheel/touchmove listeners it registered.
    expect(remove).toHaveBeenCalledWith('wheel', expect.any(Function))
    expect(remove).toHaveBeenCalledWith('touchmove', expect.any(Function))
    remove.mockRestore()
  })
})
