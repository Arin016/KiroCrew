// Persistent height cache for the chat virtualizer.
//
// Maps stable item keys to measured pixel heights so placeholders for
// off-screen items have correct sizes. Persists to localStorage keyed by
// session ID, with debounced writes (100ms) and LRU-style pruning at 2000
// entries.
//
// Falls back to in-memory-only mode when localStorage is unavailable
// (private browsing, quota exceeded, sandboxed iframes, etc.). Corrupted
// JSON triggers a console.warn and a fresh cache for that session.

const LS_KEY_PREFIX = 'vc_heights_'
const MAX_ENTRIES = 2000
const FLUSH_DELAY_MS = 100

type FlushTimer = ReturnType<typeof setTimeout>

/** Returns the localStorage object if accessible, else null. */
function getStorage(): Storage | null {
  try {
    // Touching localStorage can throw in sandboxed iframes / disabled storage.
    if (typeof window === 'undefined') return null
    const ls = window.localStorage
    // Round-trip test catches "quota=0" and other half-broken environments.
    const probe = '__vc_probe__'
    ls.setItem(probe, probe)
    ls.removeItem(probe)
    return ls
  } catch {
    return null
  }
}

export class HeightCache {
  // Map preserves insertion order, which we use as the LRU access order:
  // every `get` / `set` re-inserts the key so the most-recently-touched
  // entries sit at the tail and the oldest get evicted first.
  private readonly cache: Map<string, number> = new Map()
  private readonly sessionId: string
  private readonly storage: Storage | null
  private readonly storageKey: string
  private dirty = false
  private flushTimer: FlushTimer | null = null

  constructor(sessionId: string) {
    this.sessionId = sessionId
    this.storage = getStorage()
    this.storageKey = `${LS_KEY_PREFIX}${sessionId}`
    this.load()
  }

  /** Returns the cached height for `key`, or undefined if not measured. */
  get(key: string): number | undefined {
    const v = this.cache.get(key)
    if (v === undefined) return undefined
    // Re-insert to mark as most-recently-used. Cheap because Map operations
    // are O(1) and we only do this on cache hits.
    this.cache.delete(key)
    this.cache.set(key, v)
    return v
  }

  /** Stores `height` for `key`, evicting the oldest entry if over the cap. */
  set(key: string, height: number): void {
    // Re-insert so the key sits at the tail (most-recent position) regardless
    // of whether it already existed.
    if (this.cache.has(key)) this.cache.delete(key)
    this.cache.set(key, height)
    if (this.cache.size > MAX_ENTRIES) {
      // Map iteration is in insertion order, so the first key is the oldest.
      const oldest = this.cache.keys().next().value
      if (oldest !== undefined) this.cache.delete(oldest)
    }
    this.dirty = true
    this.scheduleFlush()
  }

  /** Writes pending changes to localStorage immediately and clears the timer. */
  flush(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.dirty) return
    this.dirty = false
    if (!this.storage) return
    try {
      // Use Object.create(null) so keys like "__proto__" or "constructor"
      // are stored as own properties instead of mutating the prototype.
      // (A naive `{}` literal swallows __proto__ on assignment.)
      const obj: Record<string, number> = Object.create(null)
      for (const [k, v] of this.cache) obj[k] = v
      this.storage.setItem(this.storageKey, JSON.stringify(obj))
    } catch {
      // Quota exceeded or transient failure — drop this flush. A future set()
      // will dirty the cache again and we'll retry on the next debounce window.
      this.dirty = true
    }
  }

  /** Clears both in-memory and persisted state for this session. */
  clear(): void {
    this.cache.clear()
    this.dirty = false
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.storage) return
    try {
      this.storage.removeItem(this.storageKey)
    } catch {
      // Best-effort — swallow.
    }
  }

  /** Number of entries currently in the cache. Visible for tests/debug. */
  size(): number {
    return this.cache.size
  }

  private load(): void {
    if (!this.storage) return
    let raw: string | null
    try {
      raw = this.storage.getItem(this.storageKey)
    } catch {
      return
    }
    if (raw === null) return
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      // Corrupted blob — log once, reset, and continue. We deliberately
      // wipe persisted state here so a bad write can't keep poisoning
      // future loads.
      // eslint-disable-next-line no-console
      console.warn(`[HeightCache] corrupted localStorage for session ${this.sessionId}; resetting`)
      try { this.storage.removeItem(this.storageKey) } catch { /* ignore */ }
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return
    // Preserve insertion order from the stored object (which preserved LRU
    // order at last flush). Skip non-numeric/non-finite values defensively.
    // Use Object.keys instead of Object.entries so own-property keys like
    // "__proto__" are visible (Object.entries skips them when they were
    // serialized into the prototype slot by JSON.parse).
    for (const k of Object.keys(parsed as Record<string, unknown>)) {
      const v = (parsed as Record<string, unknown>)[k]
      if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
        this.cache.set(k, v)
      }
    }
    // Enforce the cap on load too. set() trims one-at-a-time, but a blob from an
    // older build, a hand-edited entry, or a long-lived session can persist
    // more than MAX_ENTRIES keys — without this the cache would start a session
    // already over the cap. Trim oldest-first (insertion order) to match set().
    while (this.cache.size > MAX_ENTRIES) {
      const oldest = this.cache.keys().next().value
      if (oldest === undefined) break
      this.cache.delete(oldest)
    }
  }

  private scheduleFlush(): void {
    if (this.flushTimer !== null) return
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null
      this.flush()
    }, FLUSH_DELAY_MS)
  }
}
