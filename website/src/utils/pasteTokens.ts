/**
 * Paste-token utilities.
 *
 * Large pastes into the chat input are collapsed into inline tokens of the
 * form `⌜ Paste #N · M lines ⌟` so the textarea stays readable. The sequence
 * number N is unique within the current input session and drives reliable
 * pairing between a token occurrence in text and its backing PasteBlock.
 *
 * Seq numbers are stable once assigned — if the user deletes token #2 and
 * pastes again, the new block is assigned a fresh seq (max+1), not renumbered.
 */

/** A collapsed paste block stored alongside the input/message. */
export interface PasteBlock {
  id: string       // unique id (React key; not embedded in token text)
  seq: number      // monotonic per-session number visible in the token (`#N`)
  lines: number    // line count displayed in the token (`M lines`)
  content: string  // original pasted text
}

export const PASTE_THRESHOLD_LINES = 3
export const PASTE_THRESHOLD_CHARS = 200

/** Global regex for extracting token occurrences. (1)=seq, (2)=lines. */
export const PASTE_TOKEN_REGEX = /\[ Paste #(\d+) · (\d+) lines \]/g

export function formatToken(block: PasteBlock): string {
  return `[ Paste #${block.seq} · ${block.lines} lines ]`
}

export function shouldCollapse(text: string): boolean {
  if (!text) return false
  return countLines(text) >= PASTE_THRESHOLD_LINES || text.length >= PASTE_THRESHOLD_CHARS
}

export function countLines(text: string): number {
  if (!text) return 0
  return text.split('\n').length
}

/** React-only id; not embedded in token. */
export function makePasteId(): string {
  const t = Date.now().toString(36)
  const r = Math.floor(Math.random() * 1296).toString(36).padStart(2, '0')
  return `${t}${r}`
}

/** Next seq for a new paste = max existing + 1, starting at 1. */
export function nextSeq(blocks: PasteBlock[]): number {
  let max = 0
  for (const b of blocks) { if (b.seq > max) max = b.seq }
  return max + 1
}

/** Ranges for each token whose seq is present in `blocks`, in document order. */
export function findTokenRanges(
  text: string,
  blocks: PasteBlock[],
): Array<{ start: number; end: number; block: PasteBlock }> {
  if (!text || !blocks.length) return []
  const bySeq = new Map(blocks.map(b => [b.seq, b]))
  const out: Array<{ start: number; end: number; block: PasteBlock }> = []
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(m[1])
    const block = bySeq.get(seq)
    if (block) out.push({ start: m.index, end: m.index + m[0].length, block })
  }
  return out
}

export function tokenRangeAt(
  text: string,
  blocks: PasteBlock[],
  caret: number,
): { start: number; end: number; block: PasteBlock } | null {
  for (const r of findTokenRanges(text, blocks)) {
    if (caret >= r.start && caret <= r.end) return r
  }
  return null
}

export function pruneBlocks(text: string, blocks: PasteBlock[]): PasteBlock[] {
  if (!blocks.length) return blocks
  const survivors = new Set(findTokenRanges(text, blocks).map(r => r.block.id))
  const next = blocks.filter(b => survivors.has(b.id))
  return next.length === blocks.length ? blocks : next
}

export function expandAll(text: string, blocks: PasteBlock[]): string {
  if (!text || !blocks.length) return text
  const ranges = findTokenRanges(text, blocks)
  if (!ranges.length) return text
  let out = text
  for (let i = ranges.length - 1; i >= 0; i--) {
    const r = ranges[i]
    out = out.slice(0, r.start) + r.block.content + out.slice(r.end)
  }
  return out
}

/**
 * Merge preserved paste state from `existing` onto `incoming` (from backend
 * refresh). For each user message in `existing` with `meta.pastes`, the
 * tokenized content + pastes are re-applied to the matching incoming user
 * message — matched by expansion equality (`expandAll(old.content, old.pastes)
 * === new.content`). Consumed FIFO so repeated sends don't collide.
 *
 * Falls back to `readStoredPaste(incoming.content)` for messages that have no
 * in-memory counterpart (e.g. after page reload or chat switch) — this reads
 * from the localStorage side table populated by `saveStoredPaste`.
 *
 * Why: the backend only sees/stores the LLM-facing expanded text. Without
 * this merge, the user bubble would "expand" to full text as soon as the
 * refreshSlot after chat_done replaces the optimistic message.
 */
export function mergePreservedPastes<M extends { role: string; content: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const preserved: Array<{ content: string; pastes: PasteBlock[]; expanded: string; files: string[] | null }> = []
  for (const m of existing) {
    const pastes = (m.meta?.pastes as PasteBlock[] | undefined) || []
    if (m.role === 'user' && pastes.length) {
      const files = (m.meta?.files as string[] | undefined) ?? null
      // Normalize trailing whitespace — the backend strips it before storing,
      // so our expanded text (which may have a trailing newline/space from the
      // token + newline pattern) won't match the incoming content byte-for-byte.
      preserved.push({ content: m.content, pastes, expanded: expandAll(m.content, pastes).trimEnd(), files })
    }
  }
  const queue = preserved.slice()
  // Short-circuit: if no existing user messages have paste metadata AND no
  // incoming user message has a matching entry in the localStorage side table,
  // return the `incoming` array reference unchanged. This preserves reference
  // equality for callers that use Object.is / toBe checks, and avoids an
  // unnecessary array allocation in the common no-pastes case.
  if (!queue.length && !incoming.some(m => m.role === 'user' && readStoredPaste(m.content.trimEnd()))) {
    return incoming
  }
  return incoming.map(m => {
    if (m.role !== 'user') return m
    // 1) In-memory preservation (optimistic bubble still present)
    if (queue.length) {
      // Compare against trimEnd()'d incoming content — backend strips trailing
      // whitespace on storage, so our expanded text (pre-strip) wouldn't match.
      const incomingTrimmed = m.content.trimEnd()
      const idx = queue.findIndex(p => p.expanded === incomingTrimmed)
      if (idx >= 0) {
        const match = queue.splice(idx, 1)[0]
        const newMeta: Record<string, unknown> = { ...m.meta, pastes: match.pastes }
        // meta.files is lost on the backend-served message — preserve it
        // from the existing optimistic bubble so file chips stay clickable.
        if (match.files && match.files.length) newMeta.files = match.files
        return { ...m, content: match.content, meta: newMeta }
      }
    }
    // 2) localStorage side table (survives refresh/chat-switch)
    const stored = readStoredPaste(m.content.trimEnd())
    if (stored) {
      const newMeta: Record<string, unknown> = { ...m.meta, pastes: stored.pastes }
      if (stored.files && stored.files.length) newMeta.files = stored.files
      return { ...m, content: stored.displayTxt, meta: newMeta }
    }
    return m
  })
}

/* ---- localStorage side table: content-addressed paste preservation ---- */

const STORE_KEY = 'mc-paste-store-v1'
const STORE_CAP = 200

interface StoredPaste { displayTxt: string; pastes: PasteBlock[]; files?: string[]; savedAt: number }
type Store = Record<string, StoredPaste>

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' && parsed ? parsed as Store : {}
  } catch {
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  // Evict oldest entries past the cap to bound growth.
  const entries = Object.entries(store)
  if (entries.length > STORE_CAP) {
    entries.sort((a, b) => a[1].savedAt - b[1].savedAt)
    const trimmed: Store = {}
    for (const [k, v] of entries.slice(-STORE_CAP)) trimmed[k] = v
    store = trimmed
  }
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(store))
  } catch { /* quota exceeded or storage unavailable — ignore */ }
}

/** Persist paste tokenization for a message so it survives refresh/chat switch.
 *  Keyed by the fully-expanded content (what the backend stores).
 *  Stores `files` alongside so @-file chips stay clickable after refresh. */
export function saveStoredPaste(
  expandedContent: string,
  displayTxt: string,
  pastes: PasteBlock[],
  files?: string[],
): void {
  if (!pastes.length || !expandedContent) return
  const store = readStore()
  // Key by trimEnd() to match what the backend stores (it strips trailing whitespace).
  const key = expandedContent.trimEnd()
  store[key] = {
    displayTxt,
    pastes,
    ...(files && files.length ? { files } : {}),
    savedAt: Date.now(),
  }
  writeStore(store)
}

/** Look up persisted paste tokenization by expanded content. Returns null if absent. */
export function readStoredPaste(expandedContent: string): StoredPaste | null {
  if (!expandedContent) return null
  const store = readStore()
  return store[expandedContent] ?? null
}
