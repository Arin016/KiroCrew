/**
 * Per-slot collapsed-paste persistence. Backs the inline `[ Paste #N · M lines ]`
 * tokens in the chat input so they survive slot switches (and tab close /
 * refresh).
 *
 * WHY THIS EXISTS: the textarea text — including the paste token string — is
 * persisted per-slot by `chatDrafts` (localStorage). The `PasteBlock[]` that
 * backs each token was plain React state, cleared on every slot switch. Switch
 * away and back and the token text returned without its block, so the chip went
 * dead: `findTokenRanges` returns `[]`, the chip can't expand, and on send the
 * literal `[ Paste #N · M lines ]` string was sent instead of the content.
 * Persisting the blocks alongside the text draft keeps the two in sync.
 *
 * Storage choice — localStorage with the SAME 30-day TTL as `chatDrafts` (NOT
 * sessionStorage like `chatFileDrafts`):
 *   - The backing text draft already persists across tab close / refresh. If
 *     the blocks used sessionStorage, a refresh would reproduce the identical
 *     dead-token bug — only the trigger would change from slot-switch to
 *     refresh. Matching the text-draft lifetime fixes both.
 *   - File drafts justifiably use sessionStorage because they reference
 *     server-side files that may be GC'd. Paste blocks are self-contained text,
 *     so there is no dangling-reference risk.
 *
 * SENSITIVE DATA: unlike the prior behavior (only the inert `[ Paste #N ]`
 * token string was persisted), this stores the FULL pasted CONTENT — which may
 * contain secrets / PII — in localStorage. The `DRAFT_TTL_MS` (30-day) eviction
 * is the deletion policy that bounds that exposure window. Pasted content is
 * deliberately NOT treated as more sensitive than the surrounding unsent
 * message text, so it shares `chatDrafts`' retention rather than inventing its
 * own — if the retention window is ever revisited it should be changed for ALL
 * chat-content stores together (note `mc-paste-store-v1`, which persists
 * already-SENT paste content, currently has NO TTL — the real outlier).
 *
 * All functions are safe against corrupt / missing / quota-exhausted storage:
 * worst case the slot's blocks are dropped and the token degrades to literal
 * text — i.e. the pre-fix behavior, never worse.
 */
import type { PasteBlock } from './pasteTokens'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS } from './chatDrafts'

export const PASTE_DRAFTS_KEY = 'mc-chat-paste-drafts'
const PASTE_DRAFTS_TS_KEY = 'mc-chat-paste-drafts-ts'
/** Cap stored slots to prevent unbounded growth (shared with text drafts). */
export const PASTE_DRAFT_MAX_ENTRIES = DRAFT_MAX_ENTRIES
/** Discard blocks not touched within this window (shared with text drafts). */
export const PASTE_DRAFT_TTL_MS = DRAFT_TTL_MS
/** Drop (don't truncate) a slot whose serialized blocks exceed this size. A
 *  dropped slot degrades to literal token text — acceptable, equals pre-fix
 *  behavior — and keeps one giant paste from blowing the localStorage quota. */
export const PASTE_DRAFT_MAX_BYTES = 512 * 1024 // 512 KB per slot

export type PasteDrafts = Record<string, PasteBlock[]>

/** Per-slot last-touched timestamps for TTL eviction. Mirrors `chatDrafts`. */
const timestamps: Record<string, number> = {}
let timestampsLoaded = false

function ensureTimestampsLoaded(): void {
  if (timestampsLoaded) return
  timestampsLoaded = true
  try {
    const raw = localStorage.getItem(PASTE_DRAFTS_TS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof v === 'number') timestamps[k] = v
      }
    }
  } catch { /* ignore */ }
}

/** A value is a valid PasteBlock iff it carries all four fields with the right
 *  primitive types. Anything else is corruption and is dropped. */
function isPasteBlock(v: unknown): v is PasteBlock {
  if (!v || typeof v !== 'object') return false
  const b = v as Record<string, unknown>
  return typeof b.id === 'string' && typeof b.seq === 'number'
    && typeof b.lines === 'number' && typeof b.content === 'string'
}

/** Coerce a stored value into a clean PasteBlock[] (dropping invalid members),
 *  or `null` if it isn't a non-empty array of blocks. */
function sanitizeBlocks(v: unknown): PasteBlock[] | null {
  if (!Array.isArray(v)) return null
  const arr: PasteBlock[] = []
  for (const item of v) {
    if (isPasteBlock(item)) arr.push({ id: item.id, seq: item.seq, lines: item.lines, content: item.content })
  }
  return arr.length ? arr : null
}

export function loadPasteDrafts(): PasteDrafts {
  ensureTimestampsLoaded()
  try {
    const raw = localStorage.getItem(PASTE_DRAFTS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const cutoff = Date.now() - PASTE_DRAFT_TTL_MS
    const fresh: PasteDrafts = {}
    let pruned = false
    let stamped = false
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      const blocks = sanitizeBlocks(v)
      if (!blocks) { if (k in timestamps) { delete timestamps[k]; pruned = true } continue }
      // No timestamp = legacy / pre-TTL entry; stamp now and treat as fresh.
      if (!(k in timestamps)) { timestamps[k] = Date.now(); stamped = true }
      if (timestamps[k] >= cutoff) fresh[k] = blocks
      else { delete timestamps[k]; pruned = true }
    }
    // Persist when we evicted something OR stamped legacy entries, so the next
    // load sees real timestamps instead of re-stamping with a fresh Date.now()
    // (which would reset the TTL indefinitely on every reload).
    if (pruned || stamped) persistNow(fresh)
    return fresh
  } catch (e) {
    if (import.meta.env.DEV) console.warn('chatPasteDrafts: load failed', e)
    return {}
  }
}

/**
 * Evict oldest entries (by insertion order) if `drafts` exceeds the cap.
 * Mutates `drafts` in place. `setPasteDraft` refreshes insertion position on
 * each write, so the most-recently-touched slot is never evicted. Slot keys are
 * always `chat-<counter>-<timestamp>`, so the numeric-key enumeration quirk of
 * `Object.keys()` cannot trigger (same invariant as `chatDrafts`).
 */
function capDrafts(drafts: PasteDrafts): void {
  const keys = Object.keys(drafts)
  if (keys.length <= PASTE_DRAFT_MAX_ENTRIES) return
  for (const k of keys.slice(0, keys.length - PASTE_DRAFT_MAX_ENTRIES)) {
    delete drafts[k]
    delete timestamps[k]
  }
}

/**
 * Persist `drafts` to localStorage.
 *
 * SIDE EFFECT: mutates `drafts` in place — applies the entry cap AND drops any
 * slot whose serialized blocks exceed `PASTE_DRAFT_MAX_BYTES`. The caller
 * (ChatPage) holds a ref to this object and relies on the cap staying applied
 * across renders. Clone before calling if you don't want this.
 *
 * Order matters: write TIMESTAMPS BEFORE DRAFTS (same rationale as
 * `chatDrafts.persistNow`) so a failed drafts write can't strand un-timestamped
 * entries that would later be mistaken for legacy and have their TTL reset.
 */
function persistNow(drafts: PasteDrafts): void {
  capDrafts(drafts)
  // Drop oversized slots (mutates in place) so one huge paste can't wedge the
  // whole store against the quota.
  for (const k of Object.keys(drafts)) {
    try {
      if (JSON.stringify(drafts[k]).length > PASTE_DRAFT_MAX_BYTES) {
        // Always log (NOT DEV-gated): dropping an oversized slot is a silent UX
        // regression of this fix — on the next slot switch / refresh the chip
        // rehydrates as a dead literal `[ Paste #N · M lines ]` token. Leave a
        // breadcrumb so "why did my huge paste become a literal token?" is
        // diagnosable. (Mesh-1909 will replace the hard per-slot cap with a
        // store-level byte-aware LRU so the newest paste is never the casualty.)
        console.warn(`chatPasteDrafts: dropping slot ${k} — serialized blocks exceed ${PASTE_DRAFT_MAX_BYTES} bytes`)
        delete drafts[k]
        delete timestamps[k]
      }
    } catch { delete drafts[k]; delete timestamps[k] }
  }
  // Prune timestamps for keys no longer present.
  for (const k of Object.keys(timestamps)) {
    if (!(k in drafts)) delete timestamps[k]
  }
  try {
    localStorage.setItem(PASTE_DRAFTS_TS_KEY, JSON.stringify(timestamps))
    localStorage.setItem(PASTE_DRAFTS_KEY, JSON.stringify(drafts))
  } catch (e) {
    if (import.meta.env.DEV) console.warn('chatPasteDrafts: save failed', e)
  }
}

export function savePasteDrafts(drafts: PasteDrafts): void {
  ensureTimestampsLoaded()
  persistNow(drafts)
}

/** Mutate `drafts` for `slot`: set a defensive deep copy if non-empty
 *  (delete-then-reinsert to refresh LRU position), delete if empty. The copy
 *  prevents the caller's array/blocks from being mutated through the stored
 *  reference and vice versa. Also stamps touch time for TTL eviction. */
export function setPasteDraft(drafts: PasteDrafts, slot: string, blocks: PasteBlock[]): void {
  ensureTimestampsLoaded()
  delete drafts[slot]
  if (blocks && blocks.length) {
    drafts[slot] = blocks.map(b => ({ id: b.id, seq: b.seq, lines: b.lines, content: b.content }))
    timestamps[slot] = Date.now()
  } else {
    delete timestamps[slot]
  }
}

/** @internal test-only: reset module state between tests. Gated on
 *  `!import.meta.env.PROD` so the helper is tree-shaken out of the production
 *  bundle. PROD (not DEV) is the correct gate: DEV is false under vitest
 *  (mode 'test'), so a DEV gate would make this `undefined` during the test
 *  run itself and break the `beforeEach` callers. */
export const __resetForTests: () => void = import.meta.env.PROD
  ? (undefined as unknown as () => void)
  : () => {
      for (const k of Object.keys(timestamps)) delete timestamps[k]
      timestampsLoaded = false
    }
