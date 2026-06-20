import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { safeSetItem, safeSetSessionItem, isQuotaExceededError } from '../utils/safeStorage'

/** Build a DOMException that looks like a browser quota error. */
function quotaError(name = 'QuotaExceededError', code = 22): DOMException {
  // jsdom's DOMException constructor sets `name`; `code` is derived from the
  // legacy name table, so for custom names we override it explicitly.
  const e = new DOMException('quota', name)
  Object.defineProperty(e, 'code', { value: code, configurable: true })
  return e
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('isQuotaExceededError', () => {
  it('recognizes the Chrome/Safari QuotaExceededError', () => {
    expect(isQuotaExceededError(quotaError('QuotaExceededError', 22))).toBe(true)
  })

  it('recognizes the Firefox NS_ERROR_DOM_QUOTA_REACHED (code 1014)', () => {
    expect(isQuotaExceededError(quotaError('NS_ERROR_DOM_QUOTA_REACHED', 1014))).toBe(true)
  })

  it('rejects non-quota errors', () => {
    expect(isQuotaExceededError(new Error('nope'))).toBe(false)
    expect(isQuotaExceededError(quotaError('SecurityError', 18))).toBe(false)
    expect(isQuotaExceededError(undefined)).toBe(false)
  })
})

describe('safeSetItem', () => {
  it('writes through to localStorage on the happy path', () => {
    expect(safeSetItem('k', 'v')).toBe(true)
    expect(window.localStorage.getItem('k')).toBe('v')
  })

  it('never throws and returns false when storage rejects with a non-quota error', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError('SecurityError', 18)
    })
    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })

  it('reclaims disposable height caches and retries when quota is exceeded', () => {
    // Seed a couple of disposable height-cache entries that reclaim should drop.
    window.localStorage.setItem('vc_heights_session-A', '{"a":1}')
    window.localStorage.setItem('vc_heights_session-B', '{"b":2}')
    window.localStorage.setItem('keep-me', 'important')

    // First setItem call throws quota; once the height caches are removed the
    // retry succeeds. We simulate that by failing only while the height keys
    // are still present.
    const real = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      const heightKeysPresent =
        this.getItem('vc_heights_session-A') !== null ||
        this.getItem('vc_heights_session-B') !== null
      if (heightKeysPresent && !key.startsWith('vc_heights_')) {
        throw quotaError()
      }
      real.call(this, key, value)
    })

    const ok = safeSetItem('new-key', 'new-value')

    expect(ok).toBe(true)
    expect(window.localStorage.getItem('new-key')).toBe('new-value')
    // Disposable caches were reclaimed...
    expect(window.localStorage.getItem('vc_heights_session-A')).toBeNull()
    expect(window.localStorage.getItem('vc_heights_session-B')).toBeNull()
    // ...but non-disposable data was preserved.
    expect(window.localStorage.getItem('keep-me')).toBe('important')
    spy.mockRestore()
  })

  it('returns false (without throwing) when quota persists after reclaim', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })
    // No height caches to reclaim → no retry → swallow and report failure.
    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })
})

describe('safeSetSessionItem', () => {
  it('writes through to sessionStorage on the happy path', () => {
    expect(safeSetSessionItem('k', 'v')).toBe(true)
    expect(window.sessionStorage.getItem('k')).toBe('v')
  })

  it('never throws and returns false when sessionStorage rejects', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })
    expect(() => safeSetSessionItem('k', 'v')).not.toThrow()
    expect(safeSetSessionItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })
})
