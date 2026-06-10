// Feature: chat-virtualizer
//
// Property tests for HeightCache covering:
// - Property 3: Height Cache Consistency
// - Property 4: Height Cache Round-Trip
// Plus targeted unit tests for LRU eviction, flush debounce, and corruption
// recovery — areas that property tests don't cover well.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import * as fc from 'fast-check'
import { HeightCache } from '../hooks/virtualizer/HeightCache'

beforeEach(() => {
  // Reset persisted state between tests so sessions don't bleed into each
  // other. Tests use unique session IDs anyway, but this is belt-and-braces.
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
})

// Arbitrary: short alphanumeric keys (real item keys are stable IDs).
const keyArb = fc.stringMatching(/^[a-zA-Z0-9_-]{1,12}$/)
// Arbitrary: non-negative finite heights (real heights are pixel measurements).
const heightArb = fc.integer({ min: 0, max: 5000 })

// Feature: chat-virtualizer, Property 3: Height Cache Consistency
// **Validates: Requirements 3.1, 3.2**
describe('Property 3: Height Cache Consistency', () => {
  it('cache.get returns the last value written for each key', () => {
    fc.assert(
      fc.property(
        // A sequence of (key, height) writes. Keys can repeat — the last
        // write wins. We verify that property holds.
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 80 }),
        (ops) => {
          const cache = new HeightCache(`prop3-${Math.random()}`)

          // Track the expected last-write-wins state in a plain Map.
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            cache.set(k, h)
            expected.set(k, h)
          }

          // Every key written must read back as its most recent height.
          for (const [k, h] of expected) {
            expect(cache.get(k)).toBe(h)
          }
          // Keys never written must read back as undefined. Use a key
          // outside the arbitrary's character class so it can't collide
          // with anything fast-check generated.
          expect(cache.get('!!never written!!')).toBeUndefined()
        },
      ),
      { numRuns: 100 },
    )
  })
})

// Feature: chat-virtualizer, Property 4: Height Cache Round-Trip
// **Validates: Requirements 3.3, 3.4**
describe('Property 4: Height Cache Round-Trip', () => {
  it('flush + new instance with same sessionId returns same values', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 50 }),
        // Random session ID per run keeps localStorage isolated between
        // shrinking attempts.
        fc.uuid(),
        (ops, sid) => {
          const a = new HeightCache(sid)
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            a.set(k, h)
            expected.set(k, h)
          }
          a.flush()

          // Fresh instance reading from the same persisted slot must see
          // the same key→height mapping.
          const b = new HeightCache(sid)
          for (const [k, h] of expected) {
            expect(b.get(k)).toBe(h)
          }

          // Cleanup so the next shrink attempt starts clean.
          b.clear()
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Targeted unit tests — areas where property tests are awkward (timers,
// quota, corruption).

describe('HeightCache: LRU eviction', () => {
  it('caps at 2000 entries and evicts the oldest first', () => {
    const c = new HeightCache('lru-test')
    // Insert 2050 keys: keys 0..49 should be evicted, 50..2049 should remain.
    for (let i = 0; i < 2050; i++) c.set(`k${i}`, i)
    expect(c.size()).toBe(2000)
    expect(c.get('k0')).toBeUndefined()
    expect(c.get('k49')).toBeUndefined()
    expect(c.get('k50')).toBe(50)
    expect(c.get('k2049')).toBe(2049)
  })

  it('get() promotes a key to most-recently-used', () => {
    const c = new HeightCache('lru-promote')
    for (let i = 0; i < 2000; i++) c.set(`k${i}`, i)
    // Touch k0 — it should now be most-recently-used.
    c.get('k0')
    // Insert one more; k1 (now oldest) should be evicted, not k0.
    c.set('k_new', 9999)
    expect(c.get('k0')).toBe(0)
    expect(c.get('k1')).toBeUndefined()
    expect(c.get('k_new')).toBe(9999)
  })
})

describe('HeightCache: debounced flush', () => {
  it('schedules a single flush within 100ms regardless of how many writes', () => {
    vi.useFakeTimers()
    const c = new HeightCache('debounce')
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')

    for (let i = 0; i < 50; i++) c.set(`k${i}`, i)
    // No flush yet — still inside the debounce window.
    expect(setItemSpy).not.toHaveBeenCalled()

    vi.advanceTimersByTime(100)
    // One flush, not 50.
    expect(setItemSpy).toHaveBeenCalledTimes(1)
    setItemSpy.mockRestore()
  })

  it('flush() writes immediately and cancels the pending timer', () => {
    vi.useFakeTimers()
    const c = new HeightCache('flush-now')
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')

    c.set('a', 10)
    c.flush()
    expect(setItemSpy).toHaveBeenCalledTimes(1)

    // Advance past the debounce window — no second write should fire.
    vi.advanceTimersByTime(500)
    expect(setItemSpy).toHaveBeenCalledTimes(1)
    setItemSpy.mockRestore()
  })
})

describe('HeightCache: corruption recovery', () => {
  it('console.warns and resets when localStorage holds invalid JSON', () => {
    const sid = 'corrupt-test'
    window.localStorage.setItem(`vc_heights_${sid}`, '{not valid json')
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const c = new HeightCache(sid)
    expect(warnSpy).toHaveBeenCalledTimes(1)
    expect(warnSpy.mock.calls[0][0]).toMatch(/corrupted localStorage/)
    expect(c.size()).toBe(0)
    // Corrupted blob should have been removed.
    expect(window.localStorage.getItem(`vc_heights_${sid}`)).toBeNull()
    warnSpy.mockRestore()
  })

  it('skips non-numeric and negative values when loading', () => {
    const sid = 'bad-values'
    window.localStorage.setItem(
      `vc_heights_${sid}`,
      JSON.stringify({ a: 100, b: 'oops', c: -5, d: NaN, e: 200 }),
    )
    const c = new HeightCache(sid)
    expect(c.get('a')).toBe(100)
    expect(c.get('b')).toBeUndefined()
    expect(c.get('c')).toBeUndefined()
    expect(c.get('d')).toBeUndefined()
    expect(c.get('e')).toBe(200)
  })
})

describe('HeightCache: storage failure modes', () => {
  it('falls back to memory-only when setItem throws (e.g. quota)', () => {
    vi.useFakeTimers()
    // Throw only for the cache's own key so the constructor's getStorage probe
    // (which writes/removes "__vc_probe__") still succeeds and storage is
    // considered available. Otherwise the probe throws, storage is null, and
    // flush() early-returns — the quota catch this test claims to cover never
    // actually runs, yet the in-memory assertion passes regardless.
    const setItemSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation((key: string) => {
        if (key.startsWith('vc_heights_')) {
          throw new DOMException('quota', 'QuotaExceededError')
        }
      })

    const c = new HeightCache('quota')
    c.set('a', 100)
    vi.advanceTimersByTime(100)
    // The debounced flush actually attempted the persisting write (and threw)…
    expect(setItemSpy).toHaveBeenCalledWith('vc_heights_quota', expect.any(String))
    // …and the cache degraded gracefully to memory-only.
    expect(c.get('a')).toBe(100)
    setItemSpy.mockRestore()
  })

  it('clear() removes the persisted blob and resets memory', () => {
    const c = new HeightCache('clear-test')
    c.set('a', 1)
    c.flush()
    expect(window.localStorage.getItem('vc_heights_clear-test')).not.toBeNull()
    c.clear()
    expect(c.get('a')).toBeUndefined()
    expect(window.localStorage.getItem('vc_heights_clear-test')).toBeNull()
  })
})
