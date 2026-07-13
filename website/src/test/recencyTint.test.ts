import { describe, it, expect } from 'vitest'
import { RECENT_TINT_COUNT, computeRecentRank, recencyTintShadow } from '../utils/recencyTint'

const iso = (min: number) => `2026-07-09T20:${String(min).padStart(2, '0')}:00Z`
const shadow = (w: number, op: number) => `inset ${w}px 0 0 color-mix(in srgb, var(--accent) ${op}%, transparent)`

describe('computeRecentRank', () => {
  it('ranks by last_ts descending (1 = most recent)', () => {
    const r = computeRecentRank([
      { key: 'a', last_ts: iso(10) },
      { key: 'b', last_ts: iso(30) },
      { key: 'c', last_ts: iso(20) },
    ], 5)
    expect(r.get('b')).toBe(1)
    expect(r.get('c')).toBe(2)
    expect(r.get('a')).toBe(3)
  })

  it('keeps only the `count` most-recent and excludes the rest', () => {
    const slots = Array.from({ length: 8 }, (_, i) => ({ key: `s${i}`, last_ts: iso(i + 1) }))
    const r = computeRecentRank(slots, 5)
    expect(r.size).toBe(5)
    expect(r.get('s7')).toBe(1)     // newest (minute 8)
    expect(r.get('s3')).toBe(5)     // 5th newest (minute 4)
    expect(r.has('s2')).toBe(false) // 6th newest — beyond the count
  })

  it('excludes sessions with missing or unparseable last_ts', () => {
    const r = computeRecentRank([
      { key: 'a', last_ts: iso(10) },
      { key: 'b' },
      { key: 'c', last_ts: '' },
      { key: 'd', last_ts: 'not-a-date' },
    ], 5)
    expect(r.size).toBe(1)
    expect(r.get('a')).toBe(1)
    expect(r.has('b')).toBe(false)
    expect(r.has('c')).toBe(false)
    expect(r.has('d')).toBe(false)
  })

  it('RECENT_TINT_COUNT defaults to 5', () => {
    expect(RECENT_TINT_COUNT).toBe(5)
  })
})

describe('recencyTintShadow', () => {
  it('grades width 7→3px and opacity 100→40% across 5 ranks', () => {
    expect(recencyTintShadow(1, 5)).toBe(shadow(7, 100))
    expect(recencyTintShadow(2, 5)).toBe(shadow(6, 85))
    expect(recencyTintShadow(3, 5)).toBe(shadow(5, 70))
    expect(recencyTintShadow(4, 5)).toBe(shadow(4, 55))
    expect(recencyTintShadow(5, 5)).toBe(shadow(3, 40))
  })

  it('caps width at 7px and opacity at 100% when count > 5', () => {
    // total=8: the four most-recent ranks exceed the cap and clamp to 7px / 100%
    for (const rank of [1, 2, 3, 4]) {
      expect(recencyTintShadow(rank, 8)).toBe(shadow(7, 100))
    }
    expect(recencyTintShadow(5, 8)).toBe(shadow(6, 85)) // first uncapped step
    expect(recencyTintShadow(8, 8)).toBe(shadow(3, 40)) // floor
  })

  it('floors the least-recent tinted rank at 3px / 40%', () => {
    expect(recencyTintShadow(3, 3)).toBe(shadow(3, 40))
    expect(recencyTintShadow(1, 1)).toBe(shadow(3, 40))
  })
})
