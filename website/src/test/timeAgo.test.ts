import { describe, it, expect } from 'vitest'
import { timeAgo } from '../utils/timeAgo'

describe('timeAgo', () => {
  // Regression guard: a missing/unparseable timestamp (callers pass 0 / NaN
  // when a date is absent) must render '--', not a garbage age (ts=0 → ~20602d).
  it.each([0, NaN, undefined as unknown as number, null as unknown as number, -5, Infinity, -Infinity, 0.5])(
    'returns "--" for a non-positive / non-finite ts (%s)',
    (bad) => {
      expect(timeAgo(bad)).toBe('--')
    },
  )

  it('formats a valid recent timestamp', () => {
    const now = Math.floor(Date.now() / 1000)
    expect(timeAgo(now)).toBe('just now') // s < 10
    expect(timeAgo(now - 120)).toBe('2m ago')
    expect(timeAgo(now - 7200)).toBe('2h ago')
    expect(timeAgo(now - 172800)).toBe('2d ago')
  })

  it('treats minor clock skew (small future ts) as just now', () => {
    const future = Math.floor(Date.now() / 1000) + 5 // s ≈ -5, still < 10
    expect(timeAgo(future)).toBe('just now')
  })
})
