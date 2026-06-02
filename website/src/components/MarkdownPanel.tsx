import { memo, useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, ExternalLink, MoreVertical, Hash, WrapText, Zap, Maximize2, Minimize2, MessageSquare, MessageSquarePlus, Copy, BookOpen, BookmarkPlus, Camera, Check, X } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'
import DetailPanel from './DetailPanel'
import { CommentPopover, CommentList, formatCommentsMessage, type InlineComment } from './CommentOverlay'
import SelectionToolbar, { type SelectionAction } from './SelectionToolbar'
import MarkdownToc, { TocToggle, extractHeadingsFromDOM } from './MarkdownToc'
import { useFileWatch } from '../hooks/useFileWatch'
import { detectFileType } from './FileRenderers'
import { ContentRenderer, MD_EXTS, extOf, langFor, wrapCode } from './ContentRenderer'
import { api } from '../api/client'
import { fileReadUrl } from '../utils/fileReadUrl'
import { loadCommentDrafts, saveCommentDrafts, setCommentsForFile } from '../utils/commentDrafts'
import { copyToClipboard } from '../utils/clipboard'
/**
 * Locate the first char of `selected` in the raw source `content` and return
 * 1-based (line, column). Works perfectly for code files where rendered text
 * equals source text. For markdown, used as a fallback when DOM-based
 * `resolveSourcePos` can't resolve coordinates (rare). Exported for tests.
 */
export function findCoords(content: string, selected: string): { line: number; column: number } | undefined {
  if (!selected) return undefined
  const idx = content.indexOf(selected)
  if (idx < 0) return undefined
  const before = content.slice(0, idx)
  const nl = before.lastIndexOf('\n')
  const line = (before.match(/\n/g)?.length ?? 0) + 1
  const column = (nl < 0 ? idx : idx - nl - 1) + 1
  return { line, column }
}

/**
 * Resolve a selection `Range` inside a markdown-rendered `root` to 1-based
 * (line, column) in the source `content`, using the `data-sourcepos`
 * attributes emitted by the `rehypeSourcepos` plugin.
 *
 * Strategy: walk up from the selection start to the nearest ancestor element
 * carrying `data-sourcepos`, compute the rendered-text offset from that
 * element's start to the selection, then locate the corresponding char in
 * the element's source span by substring search scoped to that tight window
 * (duplicate-text ambiguity drops to near-zero vs global search). Returns
 * undefined when no ancestor carries a position (should not happen when the
 * renderer is built with `sourcePos`) — caller falls back to `findCoords`.
 * Exported for tests.
 */
export function resolveSourcePos(range: Range, root: HTMLElement, content: string): { line: number; column: number } | undefined {
  let el: HTMLElement | null = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer as HTMLElement
    : range.startContainer.parentElement
  while (el && el !== root && !el.hasAttribute('data-sourcepos')) el = el.parentElement
  if (!el || !el.hasAttribute('data-sourcepos')) return undefined
  const m = /^(\d+):(\d+)-(\d+):(\d+)$/.exec(el.getAttribute('data-sourcepos') || '')
  if (!m) return undefined
  // Block offset: useBlockAssembler splits raw content into separate
  // MarkdownBlocks, so data-sourcepos line numbers are relative to the
  // block's own text. The enclosing `[data-block-start]` wrapper carries
  // the 1-based line of that block within the full source.
  let blockEl: HTMLElement | null = el
  while (blockEl && blockEl !== root && !blockEl.hasAttribute('data-block-start')) blockEl = blockEl.parentElement
  const blockStart = blockEl?.hasAttribute('data-block-start') ? +(blockEl.getAttribute('data-block-start') || '1') : 1
  const lineOffset = blockStart - 1
  const sLine = +m[1] + lineOffset, sCol = +m[2], eLine = +m[3] + lineOffset, eCol = +m[4]
  // Rendered-text offset from element start to selection start
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let offset = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) { offset += range.startOffset; break }
    offset += (node as Text).data.length
  }
  // Extract the element's source span from `content`
  const lines = content.split('\n')
  if (sLine < 1 || eLine > lines.length) return { line: sLine, column: sCol }
  let span: string
  if (sLine === eLine) span = lines[sLine - 1].slice(sCol - 1, eCol - 1)
  else {
    const parts = [lines[sLine - 1].slice(sCol - 1)]
    for (let i = sLine; i < eLine - 1; i++) parts.push(lines[i])
    parts.push(lines[eLine - 1].slice(0, eCol - 1))
    span = parts.join('\n')
  }
  // Align rendered text to source span char-by-char: walk `span`, advancing
  // a rendered cursor whenever they match. When the rendered cursor equals
  // `offset`, the current span index is the source position of the selection.
  // Handles `**bold**`, `*em*`, `` `code` ``, `# Heading`, list `- ` / `> `,
  // and similar leading / wrapping / trailing syntax without enumerating
  // syntax characters (anything in span that isn't in rendered is syntax).
  const rendered = el.textContent || ''
  if (offset >= rendered.length) return { line: sLine, column: sCol }
  let spanIdx = 0
  let renderedIdx = 0
  while (spanIdx < span.length && renderedIdx < offset) {
    if (span[spanIdx] === rendered[renderedIdx]) renderedIdx++
    spanIdx++
  }
  // spanIdx now points at the position in span for rendered[renderedIdx] —
  // but may sit on leading syntax between the previous match and the target
  // rendered char. Advance past any such syntax to land on the target.
  while (spanIdx < span.length && span[spanIdx] !== rendered[offset]) spanIdx++
  // Exhausted the span without finding rendered[offset] — happens when
  // rendered text can't be aligned to source char-by-char (HTML entities
  // like `&amp;` → `&`, raw HTML like `<br>` → newline). Returning
  // element-start would silently mislead the agent; return undefined so
  // the caller falls back to `findCoords`.
  if (spanIdx >= span.length) return undefined
  // Convert spanIdx back to (line, column) in source
  let ln = sLine, cl = sCol
  for (let i = 0; i < spanIdx; i++) {
    if (span[i] === '\n') { ln++; cl = 1 } else cl++
  }
  return { line: ln, column: cl }
}
interface Props {
  filePath: string
  content: string
  onContentChange: (c: string) => void
  onSave: (filePath: string, content: string) => Promise<void>
  onClose: () => void
  liveWatch?: boolean
  onSubmitComments?: (message: string) => void
  onRefresh?: (filePath: string) => Promise<void>
}

/** Comment hint banner — shown once per session for markdown files */
function CommentHint({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div className="flex items-center gap-2 px-3 py-2 bg-accent-subtle border border-accent/30 rounded-md text-[12px] text-accent animate-scale-in mx-1 mb-2">
      <span><MessageSquare className="lucide-inline" /></span>
      <span className="flex-1">
        <strong>Tip:</strong> Select any text to add inline comments, then submit them all to the chat.
      </span>
      <button className="text-accent hover:text-accent-fg cursor-pointer bg-transparent border-none text-[11px] font-medium shrink-0" onClick={onDismiss}>Got it</button>
    </div>
  )
}

const HINT_KEY = 'kiroclaw:comment-hint-dismissed'

const DOWNLOAD_FAILED = 'Download failed'

async function downloadFile(filePath: string) {
  try {
    const res = await fetch(fileReadUrl(filePath))
    if (!res.ok) { console.error('downloadFile failed', res.status, res.statusText); alert(DOWNLOAD_FAILED); return }
    const blob = await res.blob()
    const a = document.createElement('a')
    const url = URL.createObjectURL(blob)
    a.href = url
    a.download = filePath.split('/').pop() || 'download'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 2_000)
  } catch (err) { console.error('downloadFile failed', err); alert(DOWNLOAD_FAILED) }
}

/**
 * Round 8 row-2 icon: artifact toggle. When the file is already saved
 * as an artifact, clicking navigates to the detail page. When not, it
 * triggers the add-to-artifacts mutation directly (no modal — matches
 * the bookmark widget pattern).
 */
function ArtifactToggleIconButton({ state, onNavigate }: { state: ReturnType<typeof useFileArtifactState>; onNavigate: () => void }) {
  if (state.existing) {
    return (
      <button
        className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
        onClick={onNavigate}
        title={`In Artifacts — open ${state.existing.slug}`}
        aria-label={`In Artifacts — open ${state.existing.slug}`}
      >
        <BookmarkPlus size={14} style={{ color: 'var(--ok)' }} />
      </button>
    )
  }
  return (
    <button
      className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-50"
      onClick={() => state.add()}
      disabled={state.adding}
      title="Add to artifacts (versioned, persistent)"
      aria-label="Add to artifacts"
    >
      <BookmarkPlus size={14} className={state.added ? 'lucide-inline' : ''} style={state.added ? { color: 'var(--ok)' } : undefined} />
    </button>
  )
}

/**
 * Round 8 row-2 icon: knowledge library toggle. Hidden by the caller
 * when the file's extension isn't supported (or the library is
 * unconfigured). When already added, renders as a static badge.
 */
function KnowledgeToggleIconButton({ state }: { state: ReturnType<typeof useFileKnowledgeState> }) {
  if (state.alreadyAdded) {
    return (
      <span
        className="p-1.5 rounded-md border border-border/40 text-muted inline-flex items-center"
        title="In Knowledge Library"
        aria-label="In Knowledge Library"
      >
        <BookOpen size={14} style={{ color: 'var(--ok)' }} />
      </span>
    )
  }
  return (
    <button
      className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-50"
      onClick={() => state.add()}
      disabled={state.adding}
      title="Add to Knowledge Library"
      aria-label="Add to Knowledge Library"
    >
      <BookOpen size={14} className={state.added ? 'lucide-inline' : ''} style={state.added ? { color: 'var(--ok)' } : undefined} />
    </button>
  )
}

export function OverflowMenu({ filePath, content, revealOrCopy }: { filePath: string; content: string; revealOrCopy: (path: string, action: 'open' | 'reveal') => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const closeTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => { clearTimeout(closeTimerRef.current) }, [])
  const navigate = useNavigate()
  const knowledge = useFileKnowledgeState(filePath)
  const artifact = useFileArtifactState(filePath, content)
  const delayedClose = () => { closeTimerRef.current = setTimeout(() => setOpen(false), 800) }
  useEffect(() => () => { if (closeTimerRef.current) clearTimeout(closeTimerRef.current) }, [])
  // Reset the per-mutation success flags whenever the menu closes so the
  // 'Added!' / 'Snapshotted!' acknowledgement doesn't bleed into the next
  // open if the user closed quickly. Destructure the callbacks so the dep
  // array stays stable across renders (AutoSDE round 13: object refs from
  // hooks change every render, causing the effect to re-fire constantly).
  const knowledgeReset = knowledge.reset
  const artifactResetAdd = artifact.resetAdd
  useEffect(() => {
    if (!open) {
      knowledgeReset()
      artifactResetAdd()
    }
  }, [open, knowledgeReset, artifactResetAdd])
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])
  const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
  const canAddToKnowledge = knowledge.formats && knowledge.formats.includes(ext)
  return (
    <div ref={ref} className="relative">
      <button aria-label="More options" className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={() => setOpen(!open)}>
        <MoreVertical size={14} />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[160px]">
          <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { revealOrCopy(filePath, 'reveal'); setOpen(false) }}>
            Reveal in Finder
          </button>
          <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { navigator.clipboard.writeText(filePath); setOpen(false) }}>
            Copy path
          </button>
          <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { navigator.clipboard.writeText(content); setOpen(false) }}>
            Copy content
          </button>
          <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { downloadFile(filePath); setOpen(false) }}>
            Download
          </button>
          {artifact.existing ? (
            <button
              className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover"
              onClick={() => { navigate(`/artifacts/${encodeURIComponent(artifact.existing!.slug)}`); setOpen(false) }}
              title={`Open artifact ${artifact.existing.slug}`}
            >
              <BookmarkPlus size={14} className="lucide-inline" style={{ color: 'var(--ok)' }} /> In Artifacts <Check size={14} className="lucide-inline" />
            </button>
          ) : (
            <button
              className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover disabled:opacity-50"
              onClick={() => artifact.add(undefined, { onSuccess: delayedClose })}
              disabled={artifact.adding}
              title="Save this file as an artifact (versioned, persistent, comment-able)"
            >
              {artifact.added
                ? <><BookmarkPlus size={14} className="lucide-inline" style={{ color: 'var(--ok)' }} /> Added!</>
                : artifact.adding
                  ? 'Adding…'
                  : <><BookmarkPlus size={14} className="lucide-inline" /> Add to artifacts</>}
            </button>
          )}
          {canAddToKnowledge && (
            knowledge.alreadyAdded ? (
              <span className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-muted">
                <BookOpen size={14} className="lucide-inline" /> In Library <Check size={14} className="lucide-inline" />
              </span>
            ) : (
              <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => knowledge.add(undefined, { onSuccess: delayedClose })} disabled={knowledge.adding}>
                {knowledge.added ? <><BookOpen size={14} className="lucide-inline" style={{color: 'var(--ok)'}} /> {knowledge.addResult === 'exists' ? 'Already in Library' : 'Added!'}</> : knowledge.adding ? 'Adding...' : <><BookOpen size={14} className="lucide-inline" /> Add to Knowledge</>}
              </button>
            )
          )}
        </div>
      )}
    </div>
  )
}

/**
 * File-level knowledge-library state: query for the config + already-added
 * status, mutation to register the file as a source. Always-on so the
 * inline row-2 buttons and the overflow ⋮ entry share a single fetch via
 * React Query's cache.
 */
function useFileKnowledgeState(filePath: string) {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['knowledge-config', filePath],
    queryFn: async () => {
      const r = await fetch('/api/knowledge/config')
      if (!r.ok) return null
      const cfg = await r.json()
      const sr = await fetch(`/api/knowledge/sources?uri=${encodeURIComponent(filePath)}`)
      const sources = sr.ok ? await sr.json() : []
      return { ...cfg, alreadyAdded: sources.length > 0 }
    },
  })
  const formats: string[] | null = data?.enabled ? data.supported_formats : null
  const alreadyAdded = data?.alreadyAdded ?? false
  const { mutate: add, isPending: adding, isSuccess: added, data: addResult, reset } = useMutation({
    mutationFn: async () => {
      const name = filePath.split('/').pop() || filePath
      const res = await fetch('/api/knowledge/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, source_type: 'local_file', uri: filePath }),
      })
      if (res.status === 409) return 'exists' as const
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'failed' }))
        throw new Error(err.error || 'Failed to add source')
      }
      return 'created' as const
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-config', filePath] })
    },
    onError: (err) => alert((err as Error).message),
  })
  return { formats, alreadyAdded, add, adding, added, addResult, reset }
}

/**
 * File-level artifact state: existing artifact for this source_path,
 * adding/snapshotting mutations. Round 8: live_dirty flows through so
 * the inline Snapshot button can gate visibility/enable correctly.
 */
function useFileArtifactState(filePath: string, content: string) {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['artifact-by-source-path', filePath],
    queryFn: async () => {
      const res = await api.artifacts({ source_path: filePath })
      const list = (res?.artifacts ?? []) as { slug: string; name: string }[]
      if (list.length === 0) return null
      try {
        const full = await api.artifact(list[0].slug)
        return { slug: list[0].slug, name: list[0].name, live_dirty: !!full.live_dirty }
      } catch {
        return { slug: list[0].slug, name: list[0].name, live_dirty: false }
      }
    },
  })
  const existing = data ?? null
  const { mutate: add, isPending: adding, isSuccess: added, reset: resetAdd } = useMutation({
    mutationFn: async () => {
      const name = filePath.split('/').pop() || filePath
      const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
      const kind: 'markdown' | 'json' | 'svg' | 'html' | 'text' =
        ext === '.md' || ext === '.markdown' || ext === '.mdx' ? 'markdown'
        : ext === '.json' || ext === '.jsonl' ? 'json'
        : ext === '.svg' ? 'svg'
        : ext === '.html' || ext === '.htm' ? 'html'
        : 'text'
      const created = await api.createArtifact({
        name,
        content,
        kind,
        source: 'manual',
        source_path: filePath,
        description: `Tracking ${filePath}`,
      })
      return created as { slug: string; version: number }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    },
    onError: (err) => alert((err as Error).message),
  })
  const { mutate: snapshot, isPending: snapshotting, isSuccess: snapshotted } = useMutation({
    mutationFn: async () => {
      if (!existing) throw new Error('no existing artifact')
      await api.updateArtifact(existing.slug, { snapshot: true })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifact', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-versions', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-events', existing?.slug] })
    },
    onError: (err) => alert((err as Error).message),
  })
  return { existing, add, adding, added, resetAdd, snapshot, snapshotting, snapshotted }
}

/** Shared comment overlay — popover + comment list */
const CommentOverlayBlock = memo(function CommentOverlayBlock({ popover, addComment, setPopover, onSubmitComments, comments, editComment, removeComment, submitAllComments, containerRef, scrollRef }: {
  popover: { x: number; y: number } | null; addComment: (text: string) => void; setPopover: (v: null) => void
  onSubmitComments?: (message: string) => void; comments: InlineComment[]; editComment: (id: string, text: string) => void; removeComment: (id: string) => void; submitAllComments: () => void; containerRef?: React.RefObject<HTMLElement | null>; scrollRef?: React.RefObject<HTMLElement | null>
}) {
  return (
    <>
      {popover && (
        <CommentPopover x={popover.x} y={popover.y} onSubmit={addComment} containerRef={containerRef} scrollRef={scrollRef}
          onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }} />
      )}
      {onSubmitComments && (
        <CommentList comments={comments} onEdit={editComment} onRemove={removeComment} onSubmitAll={submitAllComments} />
      )}
    </>
  )
})

export default memo(function MarkdownPanel({ filePath, content, onContentChange, onSave, onClose, liveWatch, onSubmitComments, onRefresh }: Props) {
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [lineNums, setLineNums] = useState(true)
  const [wordWrap, setWordWrap] = useState(true)
  const [autocomplete, setAutocomplete] = useState(true)
  // Hydrate pending draft comments for this file from localStorage so they
  // survive panel close, refresh, and crash. Submitting clears them.
  const draftsRef = useRef<ReturnType<typeof loadCommentDrafts>>(null!)
  if (draftsRef.current === null) draftsRef.current = loadCommentDrafts()
  const [comments, setComments] = useState<InlineComment[]>(() => draftsRef.current[filePath] ?? [])
  // Sync state to the new filePath during render (not in a useEffect) so
  // `comments` and `filePath` never disagree within a single render — otherwise
  // a callback firing in the transition window would persist against the wrong
  // file. setState-during-render is a supported React pattern (triggers a
  // re-render before commit).
  const prevFilePathRef = useRef(filePath)
  if (prevFilePathRef.current !== filePath) {
    prevFilePathRef.current = filePath
    setComments(draftsRef.current[filePath] ?? [])
  }
  const [popover, setPopover] = useState<{ x: number; y: number; anchor: string; line?: number; column?: number } | null>(null)
  const highlightMarksRef = useRef<HTMLElement[]>([])

  const clearHighlightMarks = useCallback(() => {
    for (const mark of highlightMarksRef.current) {
      const parent = mark.parentNode
      if (!parent) continue
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark)
      parent.removeChild(mark)
      parent.normalize()
    }
    highlightMarksRef.current = []
  }, [])

  const applyHighlightMarks = useCallback((range: Range) => {
    clearHighlightMarks()
    const marks: HTMLElement[] = []
    const treeWalker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT)
    const textNodes: Text[] = []
    let node: Node | null
    while ((node = treeWalker.nextNode())) {
      if (range.intersectsNode(node)) textNodes.push(node as Text)
    }
    if (textNodes.length === 0 && range.startContainer.nodeType === Node.TEXT_NODE) {
      textNodes.push(range.startContainer as Text)
    }
    for (const textNode of textNodes) {
      const start = textNode === range.startContainer ? range.startOffset : 0
      const end = textNode === range.endContainer ? range.endOffset : textNode.length
      if (start === end) continue
      const highlightRange = document.createRange()
      highlightRange.setStart(textNode, start)
      highlightRange.setEnd(textNode, end)
      const mark = document.createElement('mark')
      mark.style.backgroundColor = 'var(--accent-subtle, rgba(99, 102, 241, 0.15))'
      mark.style.borderRadius = '2px'
      highlightRange.surroundContents(mark)
      marks.push(mark)
    }
    highlightMarksRef.current = marks
  }, [clearHighlightMarks])
  const [refreshing, setRefreshing] = useState(false)
  const [hintDismissed, setHintDismissed] = useState(() => localStorage.getItem(HINT_KEY) === '1')
  const [fullscreen, setFullscreen] = useState(false)
  const [tocOpen, setTocOpen] = useState(false)
  const fileName = filePath.split('/').pop() || filePath
  // Mesh-1654 round 8: surface artifact / knowledge state in the row 2
  // toolbar — same hooks the overflow ⋮ uses, so the cache stays warm
  // across the two views and the button states are coherent.
  const knowledge = useFileKnowledgeState(filePath)
  const artifactState = useFileArtifactState(filePath, content)
  const navigate = useNavigate()
  const previewRef = useRef<HTMLDivElement>(null)
  const sidePanelScrollRef = useRef<HTMLDivElement>(null)
  const fullscreenPreviewRef = useRef<HTMLDivElement>(null)
  const gutterReadRef = useRef<HTMLDivElement>(null)
  const gutterFullscreenRef = useRef<HTMLDivElement>(null)
  const fullscreenBodyRef = useRef<HTMLDivElement>(null)
  const ext = extOf(filePath)
  const fileType = detectFileType(filePath)
  const isMarkdown = MD_EXTS.has(ext)
  const [hasHeadings, setHasHeadings] = useState(false)
  const isRichType = fileType === 'image' || fileType === 'csv' || fileType === 'json' || fileType === 'jsonl' || fileType === 'html' || fileType === 'pdf'
  const lang = langFor(ext)
  const displayContent = isMarkdown ? content : wrapCode(content, ext)
  const highlightedHtml = useMemo(() => {
    if (isMarkdown || editing || isRichType) return ''
    try { return DOMPurify.sanitize(hljs.highlight(content, { language: lang }).value) + '\n' }
    catch { return DOMPurify.sanitize(hljs.highlightAuto(content).value) + '\n' }
  }, [content, lang, isMarkdown, editing, isRichType])

  const { status: watchStatus } = useFileWatch(
    liveWatch && !editing && !dirty ? filePath : null,
    useCallback((c: string) => { onContentChange(c) }, [onContentChange]),
  )

  // Detect headings from rendered DOM for TOC toggle visibility
  useEffect(() => {
    if (!isMarkdown || editing) { setHasHeadings(false); return }
    if (tocOpen) { setHasHeadings(true); return } // MarkdownToc handles extraction when open
    const container = sidePanelScrollRef.current ?? fullscreenBodyRef.current
    if (!container) return
    const check = () => setHasHeadings(extractHeadingsFromDOM(container).length > 0)
    check()
    const mo = new MutationObserver(check)
    mo.observe(container, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [isMarkdown, editing, content, fullscreen, tocOpen])

  const revealOrCopy = useCallback(async (path: string, action: 'open' | 'reveal') => {
    try {
      const res = await fetch('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, action }) })
      const data = await res.json()
      if (data.copy) { await navigator.clipboard.writeText(data.copy); alert('Path copied to clipboard (no desktop available)') }
    } catch { /* ignore */ }
  }, [])

  const handleRefresh = useCallback(async () => {
    if (refreshing || dirty) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        const res = await fetch(fileReadUrl(filePath))
        if (res.ok) onContentChange(await res.text())
      }
    } finally { setRefreshing(false) }
  }, [filePath, onContentChange, onRefresh, refreshing, dirty])

  // Discard pending edits (round 8: matches the artifact detail page's
  // Cancel button). Re-reads the file from disk into the buffer,
  // clearing dirty. Confirms first because edits are gone for good.
  const handleCancel = useCallback(async () => {
    if (!dirty) { setEditing(false); return }
    if (!window.confirm('Discard unsaved changes?')) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        const res = await fetch(fileReadUrl(filePath))
        if (res.ok) onContentChange(await res.text())
      }
      setDirty(false)
      setEditing(false)
    } finally { setRefreshing(false) }
  }, [dirty, filePath, onContentChange, onRefresh])

  const resolveSelectionCoords = useCallback((fallbackText?: string) => {
    const sel = window.getSelection()
    const root = previewRef.current ?? fullscreenPreviewRef.current
    if (!root) return undefined
    // Try live selection first
    if (sel && !sel.isCollapsed && sel.anchorNode && root.contains(sel.anchorNode)) {
      const raw = sel.toString()
      if (raw.trim()) {
        const range = sel.getRangeAt(0)
        if (root.contains(range.startContainer) && root.contains(range.endContainer)) {
          const anchor = raw.trim()
          const rect = range.getBoundingClientRect()
          const coords = isMarkdown
            ? (resolveSourcePos(range, root, displayContent) ?? findCoords(displayContent, raw) ?? findCoords(displayContent, anchor))
            : (findCoords(content, raw) ?? findCoords(content, anchor))
          return { anchor, rect, range: range.cloneRange(), line: coords?.line, column: coords?.column }
        }
      }
    }
    // Fallback: selection was cleared by button click — use text + findCoords
    if (fallbackText) {
      const coords = isMarkdown ? findCoords(displayContent, fallbackText) : findCoords(content, fallbackText)
      return { anchor: fallbackText, rect: new DOMRect(0, 0, 0, 0), range: undefined, line: coords?.line, column: coords?.column }
    }
    return undefined
  }, [content, displayContent, isMarkdown])

  const handleCommentAction = useCallback((text: string, rect: DOMRect) => {
    const info = resolveSelectionCoords(text)
    if (!info) return
    if (info.range) applyHighlightMarks(info.range)
    const popRect = info.rect.width > 0 ? info.rect : rect
    setPopover({ x: popRect.left, y: popRect.bottom, anchor: info.anchor, line: info.line, column: info.column })
    window.getSelection()?.removeAllRanges()
  }, [resolveSelectionCoords, applyHighlightMarks])

  const handleCopyAction = useCallback((text: string) => {
    if (text) copyToClipboard(text)
  }, [])

  const selectionActions: SelectionAction[] = useMemo(() => {
    if (!onSubmitComments) return [{ id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: handleCopyAction }]
    return [
      { id: 'comment', icon: <MessageSquarePlus size={12} />, label: 'Comment', onClick: handleCommentAction },
      { id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: handleCopyAction },
    ]
  }, [onSubmitComments, handleCommentAction, handleCopyAction])

  const addComment = useCallback((text: string) => {
    if (!popover) return
    const newComment = { id: Math.random().toString(36).substring(2), anchor: popover.anchor, text, line: popover.line, column: popover.column }
    setComments(prev => [...prev, newComment])
    setPopover(null)
    clearHighlightMarks()
  }, [popover, clearHighlightMarks])

  const removeComment = useCallback((id: string) => {
    setComments(prev => prev.filter(c => c.id !== id))
  }, [])

  const editComment = useCallback((id: string, text: string) => {
    setComments(prev => prev.map(c => c.id === id ? { ...c, text } : c))
  }, [])

  const submitAllComments = useCallback(() => {
    if (!onSubmitComments || comments.length === 0) return
    onSubmitComments(formatCommentsMessage(filePath, comments, displayContent))
    setComments([])
  }, [onSubmitComments, comments, filePath, displayContent])

  const dismissHint = useCallback(() => {
    setHintDismissed(true)
    localStorage.setItem(HINT_KEY, '1')
  }, [])

  useEffect(() => {
    if (editing) { setPopover(null); clearHighlightMarks(); window.getSelection()?.removeAllRanges() }
  }, [editing, clearHighlightMarks])

  // Centralize persistence: fires on any comments mutation (add / remove /
  // submit-clear) and after the filePath sync-reset above. Keeping it in one
  // place avoids duplicate writes from StrictMode double-invoked updaters and
  // eliminates persistComments from callback dep arrays.
  useEffect(() => {
    setCommentsForFile(draftsRef.current, filePath, comments)
    saveCommentDrafts(draftsRef.current)
  }, [comments, filePath])

  const handleSave = useCallback(async () => {
    setSaving(true); setSaveError(null)
    try { await onSave(filePath, content); setDirty(false) }
    catch (err) { setSaveError(err instanceof Error ? err.message : 'Save failed') }
    finally { setSaving(false) }
  }, [filePath, content, onSave])

  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  const guardedClose = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return
    onClose()
  }, [dirty, onClose])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { if (popover) { setPopover(null); clearHighlightMarks() } else if (fullscreen) setFullscreen(false); else guardedClose() }
      if ((e.metaKey || e.ctrlKey) && e.key === 's' && editing && dirty) { e.preventDefault(); handleSaveRef.current() }
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [guardedClose, editing, dirty, fullscreen, popover, clearHighlightMarks])

  const handleChange = useCallback((v: string) => { onContentChange(v); setDirty(true) }, [onContentChange])
  const clearPopover = useCallback(() => { setPopover(null); clearHighlightMarks() }, [clearHighlightMarks])

  // Lock body scroll when fullscreen overlay is open
  useEffect(() => {
    if (!fullscreen) return
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [fullscreen])

  return (
    <>
    <DetailPanel
      title={fileName}
      onClose={guardedClose}
      initialWidth={480}
      minWidth={420}
      storageKey="mc-panel-width"
      headerActions={<>
        {/* Row 1: identity / view actions — refresh, TOC, fullscreen,
            external open, overflow ⋮. Stays uncluttered regardless of
            edit/preview mode. */}
        <button
          className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
          onClick={handleRefresh}
          disabled={refreshing || dirty}
          title={dirty ? 'Save or discard changes first' : 'Refresh file (re-read from disk)'}
          aria-label={dirty ? 'Save or discard changes first' : 'Refresh file (re-read from disk)'}
        ><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /></button>
        {isMarkdown && !editing && <TocToggle visible={tocOpen} hasHeadings={hasHeadings} onClick={() => setTocOpen(v => !v)} />}
        <button
          className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
          onClick={() => setFullscreen(f => !f)}
          title={fullscreen ? 'Exit full screen (Esc)' : 'Full screen'}
          aria-label={fullscreen ? 'Exit full screen' : 'Full screen'}
        >{fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}</button>
        <button
          className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
          onClick={() => revealOrCopy(filePath, 'open')}
          title="Open with default application"
          aria-label="Open with default application"
        ><ExternalLink size={14} /></button>
        <OverflowMenu filePath={filePath} content={content} revealOrCopy={revealOrCopy} />
      </>}
      secondaryHeaderActions={!isRichType ? (
        // Row 2: editor controls — formatting toggles (only in edit mode)
        // on the left, mode toggle + save action on the right. Hidden
        // entirely for rich types (markdown preview at top of stack only when no editor)
        // where there's no editor surface to configure.
        <>
          <div className="flex items-center gap-1.5">
            {!editing && (
              // Round 8: file-level lifecycle shortcuts (Add to artifacts /
              // Knowledge) live on the left of row 2 when in preview mode
              // — fast access without opening the overflow ⋮. Hidden in
              // edit mode so the user focuses on save/cancel/snapshot.
              <>
                <ArtifactToggleIconButton state={artifactState} onNavigate={() => navigate(`/artifacts/${encodeURIComponent(artifactState.existing!.slug)}`)} />
                {(() => {
                  const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
                  const canK = knowledge.formats && knowledge.formats.includes(ext)
                  if (!canK) return null
                  return <KnowledgeToggleIconButton state={knowledge} />
                })()}
              </>
            )}
            {editing && (
              <>
                <button
                  className={`p-1.5 rounded-md border cursor-pointer transition-all ${wordWrap ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  onClick={() => setWordWrap(!wordWrap)}
                  title="Toggle word wrap"
                  aria-label="Toggle word wrap"
                  aria-pressed={wordWrap}
                ><WrapText size={14} /></button>
                <button
                  className={`p-1.5 rounded-md border cursor-pointer transition-all ${autocomplete ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  onClick={() => setAutocomplete(!autocomplete)}
                  title="Toggle autocomplete"
                  aria-label="Toggle autocomplete"
                  aria-pressed={autocomplete}
                ><Zap size={14} /></button>
                <button
                  className={`p-1.5 rounded-md border cursor-pointer transition-all ${lineNums ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  onClick={() => setLineNums(!lineNums)}
                  title="Toggle line numbers"
                  aria-label="Toggle line numbers"
                  aria-pressed={lineNums}
                ><Hash size={14} /></button>
              </>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            {/* Edit-mode action order matches the artifact detail page
                (round 9 polish): Save | Snapshot | Cancel | Preview.
                In view mode only Snapshot + Edit/Preview are relevant. */}
            <button className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`} disabled={saving || !dirty || !editing} onClick={handleSave} style={!editing ? { display: 'none' } : undefined}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            {/* Snapshot — round 8 spec: only when the file is an artifact
                (no overflow entry anymore). Visible in both view and edit
                mode but disabled until there's something to snapshot
                (live drift OR pending edits). Saves the buffer first if
                dirty so the snapshot captures what the user just typed. */}
            {artifactState.existing && (
              <button
                className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                disabled={artifactState.snapshotting || (!artifactState.existing.live_dirty && !dirty)}
                title={
                  artifactState.snapshotted
                    ? 'Snapshotted!'
                    : artifactState.snapshotting
                      ? 'Snapshotting…'
                      : dirty
                        ? 'Save and snapshot — captures the current edits as a new version'
                        : 'Snapshot — capture the current file content as a new version'
                }
                aria-label="Snapshot"
                onClick={async () => {
                  if (dirty) {
                    // Save first so the snapshot reflects the buffer.
                    await handleSave()
                  }
                  artifactState.snapshot()
                }}
              ><Camera size={14} /></button>
            )}
            {editing && (
              <button
                className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40 inline-flex items-center gap-1"
                onClick={handleCancel}
                disabled={refreshing}
                title="Cancel — discard unsaved edits"
              >
                <X size={12} /> Cancel
              </button>
            )}
            <button
              className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${editing ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
              onClick={() => setEditing(!editing)}
            >{editing ? 'Preview' : 'Edit'}</button>
          </div>
        </>
      ) : undefined}
      footer={<>
        <div className="text-[11px] text-muted font-mono truncate flex items-center gap-2" title={filePath}>
          {watchStatus === 'open' && <span className="inline-block w-1.5 h-1.5 rounded-full bg-ok animate-pulse" title="Live watching" />}
          {filePath}
        </div>
      </>}
    >
      {saveError && <div className="text-[11px] text-danger">{saveError}</div>}
      {/* Comment hint for markdown files */}
      {isMarkdown && !editing && onSubmitComments && !hintDismissed && (
        <CommentHint onDismiss={dismissHint} />
      )}
      <div className="flex-1 overflow-hidden -mx-5 -my-4 px-4 py-4 flex">
        {!fullscreen && <div ref={sidePanelScrollRef} className="flex-1 overflow-auto"><ContentRenderer isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onChange={handleChange}
          previewRef={previewRef} displayContent={displayContent} isMarkdown={isMarkdown} highlightedHtml={highlightedHtml} gutterReadRef={gutterReadRef} markdownClassName="msg-content text-sm leading-relaxed" /></div>}
        {!fullscreen && tocOpen && isMarkdown && !editing && <MarkdownToc containerRef={sidePanelScrollRef} onClose={() => setTocOpen(false)} />}
      </div>
      {!fullscreen && !editing && <SelectionToolbar containerRef={sidePanelScrollRef} actions={selectionActions} />}
      {!fullscreen && <CommentOverlayBlock popover={popover} addComment={addComment} setPopover={clearPopover} onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} />}
    </DetailPanel>
    {fullscreen && createPortal(
      <div className="fixed inset-0 z-[9999] bg-bg flex flex-col" role="dialog" aria-modal="true" aria-label="Full screen file preview"
        ref={el => { if (el && !el.dataset.focused) { el.dataset.focused = '1'; const first = el.querySelector<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); first?.focus() } }}
        onKeyDown={e => { if (e.key === 'Tab') { if ((document.activeElement as HTMLElement)?.closest('.monaco-editor')) return; const focusable = e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); if (focusable.length === 0) return; const first = focusable[0], last = focusable[focusable.length - 1]; if (e.shiftKey) { if (document.activeElement === first) { e.preventDefault(); last.focus() } } else { if (document.activeElement === last) { e.preventDefault(); first.focus() } } } }}>

        {/* Header — pl-20 clears macOS traffic-light buttons */}
        <div className="flex items-center justify-between pl-20 pr-6 h-12 shrink-0 border-b border-border">
          <span className="text-base font-semibold text-text-strong truncate">{fileName}</span>
          <div className="flex items-center gap-1.5">
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40" onClick={handleRefresh} disabled={refreshing || dirty} title={dirty ? 'Save or discard changes first' : 'Refresh file'} aria-label="Refresh file"><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /></button>
            {isMarkdown && !editing && <TocToggle visible={tocOpen} hasHeadings={hasHeadings} onClick={() => setTocOpen(v => !v)} />}
            <OverflowMenu filePath={filePath} content={content} revealOrCopy={revealOrCopy} />
            {!isRichType && editing && (
              <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${wordWrap ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setWordWrap(!wordWrap)} title="Toggle word wrap"><WrapText size={14} /></button>
            )}
            {!isRichType && editing && (
              <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${autocomplete ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setAutocomplete(!autocomplete)} title="Toggle autocomplete"><Zap size={14} /></button>
            )}
            {!isRichType && editing && (
              <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${lineNums ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setLineNums(!lineNums)} title="Toggle line numbers"><Hash size={14} /></button>
            )}
            {!isRichType && (
              <button className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${editing ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setEditing(!editing)}>{editing ? 'Preview' : 'Edit'}</button>
            )}
            {!isRichType && (
              <button className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`} disabled={saving || !dirty} onClick={handleSave}>{saving ? 'Saving…' : 'Save'}</button>
            )}
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={() => setFullscreen(false)} title="Exit full screen (Esc)" aria-label="Exit full screen"><Minimize2 size={14} /></button>
          </div>
        </div>
        {saveError && <div className="px-16 text-[11px] text-danger">{saveError}</div>}
        {isMarkdown && !editing && onSubmitComments && !hintDismissed && <div className="px-16"><CommentHint onDismiss={dismissHint} /></div>}
        {/* Body */}
        <div className="flex-1 flex overflow-hidden">
          <div ref={fullscreenBodyRef} className="flex-1 overflow-auto px-16 py-4">
            <ContentRenderer isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onChange={handleChange}
              previewRef={fullscreenPreviewRef} displayContent={displayContent} isMarkdown={isMarkdown} highlightedHtml={highlightedHtml} gutterReadRef={gutterFullscreenRef} />
          </div>
          {tocOpen && isMarkdown && !editing && <MarkdownToc containerRef={fullscreenBodyRef} onClose={() => setTocOpen(false)} />}
        </div>
        {!editing && <SelectionToolbar containerRef={fullscreenBodyRef} actions={selectionActions} />}
        <CommentOverlayBlock popover={popover} addComment={addComment} setPopover={clearPopover} onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} scrollRef={fullscreenBodyRef} />
        {/* Footer */}
        <div className="shrink-0 px-6 py-2 border-t border-border text-[11px] text-muted font-mono truncate">{filePath}</div>
      </div>,
      document.body
    )}
    </>
  )
})
