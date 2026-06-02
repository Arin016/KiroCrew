/**
 * Per-slot chat draft persistence. Drafts survive tab close, refresh, and
 * browser crashes via localStorage (previously sessionStorage which cleared on
 * close). All functions are safe against corrupt/missing storage.
 */
export const DRAFTS_KEY = 'mc-chat-drafts'
const DRAFTS_TS_KEY = 'mc-chat-drafts-ts'
export const DRAFT_SAVE_DEBOUNCE_MS = 300
/** Cap stored drafts to prevent unbounded growth from deleted slots. */
export const DRAFT_MAX_ENTRIES = 50
/** Discard drafts not edited within this window. Guards against stale sensitive
 *  content (API keys, credentials, PII) persisting indefinitely in localStorage. */
export const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000 // 30 days

export type Drafts = Record<string, string>

/** Per-slot last-edited timestamps. Updated by `setDraft`, persisted by
 *  `saveDrafts`, consulted by `loadDrafts` to evict stale entries. */
const timestamps: Record<string, number> = {}
let timestampsLoaded = false

function ensureTimestampsLoaded(): void {
  if (timestampsLoaded) return
  timestampsLoaded = true
  try {
    const raw = localStorage.getItem(DRAFTS_TS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof v === 'number') timestamps[k] = v
      }
    }
  } catch { /* ignore */ }
}

export function loadDrafts(): Drafts {
  ensureTimestampsLoaded()
  try {
    const raw = localStorage.getItem(DRAFTS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const cutoff = Date.now() - DRAFT_TTL_MS
    const fresh: Drafts = {}
    let pruned = false
    let stamped = false
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v !== 'string') continue
      // No timestamp = legacy pre-TTL entry; stamp now and treat as fresh.
      if (!(k in timestamps)) { timestamps[k] = Date.now(); stamped = true }
      if (timestamps[k] >= cutoff) fresh[k] = v
      else { delete timestamps[k]; pruned = true }
    }
    // Persist when we evicted something OR when we stamped legacy entries, so
    // the next load sees real timestamps instead of re-stamping with a fresh
    // Date.now() (which would reset the TTL indefinitely on every reload).
    if (pruned || stamped) persistNow(fresh)
    return fresh
  } catch (e) {
    if (import.meta.env.DEV) console.warn('chatDrafts: load failed', e)
    return {}
  }
}

/**
 * Evict oldest entries (by insertion order) if `drafts` exceeds the cap.
 * Mutates `drafts` in place. `setDraft` refreshes insertion position on each
 * write, so the most-recently-edited draft is never evicted.
 *
 * NOTE: relies on `Object.keys()` returning keys in insertion order. This is
 * guaranteed by the ES spec for string keys *except* purely numeric strings
 * (e.g. "0", "42"), which are enumerated first in numeric order. Slot keys in
 * this codebase are always `chat-<counter>-<timestamp>` so the numeric-index
 * rule cannot trigger. If slot naming ever changes, revisit this.
 */
function capDrafts(drafts: Drafts): void {
  const keys = Object.keys(drafts)
  if (keys.length <= DRAFT_MAX_ENTRIES) return
  for (const k of keys.slice(0, keys.length - DRAFT_MAX_ENTRIES)) {
    delete drafts[k]
    delete timestamps[k]
  }
}

/**
 * Persist `drafts` to localStorage.
 *
 * SIDE EFFECT: mutates `drafts` in place if it exceeds `DRAFT_MAX_ENTRIES`
 * (oldest entries are deleted). The in-place mutation is intentional — the
 * caller (ChatPage) holds a ref to this object and relies on the cap staying
 * applied across render cycles. Clone before calling if you don't want this.
 *
 * CROSS-TAB BEHAVIOR: full overwrite. Two dashboard tabs open simultaneously
 * will see last-write-wins for the shared key. A merge was attempted but
 * breaks LRU ordering and resurrects intentionally-deleted drafts (see
 * commit history). Accepted tradeoff — dashboard is typically single-tab.
 */
export function saveDrafts(drafts: Drafts): void {
  ensureTimestampsLoaded()
  capDrafts(drafts)
  // Prune timestamps for keys no longer present.
  for (const k of Object.keys(timestamps)) {
    if (!(k in drafts)) delete timestamps[k]
  }
  persistNow(drafts)
}

/** Raw persist: write drafts + timestamps to localStorage. Called by
 *  `saveDrafts` and internally by `loadDrafts` after TTL pruning.
 *
 *  Order matters: write TIMESTAMPS BEFORE DRAFTS. The two setItem calls are
 *  non-atomic, and if the second fails (e.g. QuotaExceededError), new drafts
 *  would land on disk without corresponding timestamps — the next load would
 *  treat them as legacy and re-stamp with a fresh Date.now(), resetting TTL.
 *  By writing timestamps first, a failed drafts write leaves the timestamp
 *  map ahead of the draft map; orphan timestamps are harmless because
 *  loadDrafts iterates draft keys, not timestamp keys. */
function persistNow(drafts: Drafts): void {
  try {
    localStorage.setItem(DRAFTS_TS_KEY, JSON.stringify(timestamps))
    localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts))
  } catch (e) {
    if (import.meta.env.DEV) console.warn('chatDrafts: save failed', e)
  }
}

/** Mutate `drafts` for `slot`: set if non-empty (delete-then-reinsert to
 *  refresh insertion-order position for LRU eviction), delete if empty.
 *  Also stamps edit time for TTL-based staleness eviction. */
export function setDraft(drafts: Drafts, slot: string, value: string): void {
  ensureTimestampsLoaded()
  delete drafts[slot]
  if (value) {
    drafts[slot] = value
    timestamps[slot] = Date.now()
  } else {
    delete timestamps[slot]
  }
}

/** @internal test-only: reset module state between tests. */
export function __resetForTests(): void {
  for (const k of Object.keys(timestamps)) delete timestamps[k]
  timestampsLoaded = false
}
