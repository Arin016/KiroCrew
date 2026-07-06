import { safeSetItem } from './safeStorage'
/**
 * Per-file inline comment draft persistence. Pending (unsubmitted) comments
 * survive `MarkdownPanel` close, page refresh, and browser crashes via
 * localStorage. All functions are safe against corrupt/missing storage.
 */
import type { InlineComment } from '../components/CommentOverlay'

export const COMMENT_DRAFTS_KEY = 'mc-comment-drafts'
/** Cap stored files to prevent unbounded growth from long-term reviewers. */
export const COMMENT_DRAFT_MAX_FILES = 20

export type CommentDrafts = Record<string, InlineComment[]>

/** Defensive: accept only non-empty arrays of objects with required keys. */
function isValidComments(v: unknown): v is InlineComment[] {
  if (!Array.isArray(v) || v.length === 0) return false
  return v.every(c => c && typeof c === 'object'
    && typeof (c as InlineComment).id === 'string'
    && typeof (c as InlineComment).anchor === 'string'
    && typeof (c as InlineComment).text === 'string')
}

export function loadCommentDrafts(): CommentDrafts {
  try {
    const raw = localStorage.getItem(COMMENT_DRAFTS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    // Drop any entries that don't match the expected shape (tolerate partial corruption).
    const clean: CommentDrafts = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (isValidComments(v)) clean[k] = v
    }
    return clean
  } catch (e) {
    // eslint-disable-next-line no-console
    if (import.meta.env.DEV) console.warn('commentDrafts: load failed', e)
    return {}
  }
}

/**
 * Evict oldest file entries (by insertion order) if over the cap. Mutates in
 * place. `setCommentsForFile` refreshes insertion position on every write so
 * the most-recently-edited file survives.
 */
function capDrafts(drafts: CommentDrafts): void {
  const keys = Object.keys(drafts)
  if (keys.length <= COMMENT_DRAFT_MAX_FILES) return
  for (const k of keys.slice(0, keys.length - COMMENT_DRAFT_MAX_FILES)) delete drafts[k]
}

/**
 * Persist `drafts` to localStorage. If over cap, evicts oldest entries — but
 * only after a successful write, so a `setItem` failure (e.g. QuotaExceeded)
 * doesn't silently drop in-memory drafts that were never persisted.
 * Full overwrite (last-write-wins across tabs) — dashboard is typically
 * single-tab; a read-merge approach caused LRU ordering bugs in Mesh-686.
 */
export function saveCommentDrafts(drafts: CommentDrafts): void {
  const toSave = { ...drafts }
  capDrafts(toSave)
  const ok = safeSetItem(COMMENT_DRAFTS_KEY, JSON.stringify(toSave))
  if (ok) {
    // Sync evictions back to the caller only after a successful persist.
    for (const k of Object.keys(drafts)) if (!(k in toSave)) delete drafts[k]
  }
}

/**
 * Set (or delete if empty) the comments for `filePath`. Delete-then-reinsert
 * refreshes insertion-order position so LRU eviction targets the least
 * recently touched file.
 */
export function setCommentsForFile(drafts: CommentDrafts, filePath: string, comments: InlineComment[]): void {
  delete drafts[filePath]
  if (comments.length > 0) drafts[filePath] = comments
}
