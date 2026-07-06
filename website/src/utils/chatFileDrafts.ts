/**
 * Per-slot pending-file-attachment persistence. Mirrors `chatDrafts` but for
 * unsent file attachments (paths staged in the compose box before send).
 *
 * Storage differs intentionally:
 *   - `chatDrafts` uses localStorage so in-progress text survives tab close.
 *   - `chatFileDrafts` uses sessionStorage because attachment paths point at
 *     uploaded files that may be garbage-collected server-side after the
 *     session ends. Persisting them across tab close leads to dangling
 *     references in the UI.
 *
 * No TTL, no LRU cap: file-path arrays are tiny and session-scoped.
 */
export const FILE_DRAFTS_KEY = 'mc-chat-file-drafts'

export type FileDrafts = Record<string, string[]>

export function loadFileDrafts(): FileDrafts {
  try {
    const raw = sessionStorage.getItem(FILE_DRAFTS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: FileDrafts = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (!Array.isArray(v)) continue
      const arr = v.filter((x): x is string => typeof x === 'string')
      if (arr.length) out[k] = arr
    }
    return out
  } catch (e) {
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('chatFileDrafts: load failed', e)
    return {}
  }
}

export function saveFileDrafts(drafts: FileDrafts): void {
  try {
    sessionStorage.setItem(FILE_DRAFTS_KEY, JSON.stringify(drafts))
  } catch (e) {
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('chatFileDrafts: save failed', e)
  }
}

/** Mutate `drafts` for `slot`: set a defensive copy if non-empty, delete if
 *  empty. Copy prevents the caller's array from being mutated through the
 *  stored reference (and vice versa). */
export function setFileDraft(drafts: FileDrafts, slot: string, value: string[]): void {
  if (value && value.length) drafts[slot] = value.slice()
  else delete drafts[slot]
}
