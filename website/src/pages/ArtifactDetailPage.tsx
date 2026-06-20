import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, AlertTriangle, Camera, ExternalLink, Download, Pencil, X, AlertCircle, RotateCcw, Plus, Sparkles } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { PageHeader, Card, Badge, Btn } from '../components/ui'
import { ContentRenderer, langFor, wrapCode } from '../components/ContentRenderer'
import ReadingWidthToggle from '../components/ReadingWidthToggle'
import { useReadingWidth } from '../hooks/useReadingWidth'
import { CommentPopover, CommentList, formatCommentsMessage, type InlineComment } from '../components/CommentOverlay'
import { findCoords, resolveSourcePos } from '../components/MarkdownPanel'
import { PREFILL_STORAGE_KEY } from './ChatPage'
import type { FileType } from '../components/FileRenderers'
import type { Artifact, ArtifactEvent } from '../types'

function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

/** Map an artifact `kind` to the FileType the ContentRenderer expects.
 * Only used for non-iframe kinds (widget/html still go through the iframe). */
function fileTypeForKind(kind: Artifact['kind']): FileType {
  switch (kind) {
    case 'markdown': return 'markdown'
    case 'json':     return 'json'
    case 'svg':      return 'svg'
    case 'text':     return 'code'
    // widget / html shouldn't reach here, but fall back to markdown rather
    // than throwing — keeps the page survivable for unexpected enum values.
    default:         return 'markdown'
  }
}

/** Pseudo-extension used when rendering text artifacts as code. */
function extForKind(kind: Artifact['kind']): string {
  switch (kind) {
    case 'json': return '.json'
    case 'svg':  return '.svg'
    case 'text': return '.txt'
    default:     return '.md'
  }
}

/** Whether this artifact kind supports inline editing in the detail page.
 * Widget / html are agent-managed (raw HTML editing has too many edge cases —
 * see Mesh-1654 design discussion); markdown / text / json / svg are
 * editable text formats. */
export function isEditableKind(kind: Artifact['kind']): boolean {
  return kind === 'markdown' || kind === 'text' || kind === 'json' || kind === 'svg'
}

/** Format an ISO timestamp into a short human-readable string for the
 * activity timeline ('5/25/26, 10:31 PM'). Falls back to the raw string
 * if Date parsing fails. */
function formatEventTs(ts: string): string {
  if (!ts) return '?'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleString(undefined, {
    year: '2-digit', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

/** Lifecycle activity timeline. Renders a chronological feed of
 * created/edited/iterated/referenced/reverted events from the artifact's
 * audit log. */
const ActivityTimeline = memo(function ActivityTimeline({
  events, navigateToSlot,
}: {
  events: ArtifactEvent[]
  navigateToSlot: (slotKey: string) => void
}) {
  if (!events.length) {
    return (
      <div className="text-[12px] text-muted">No lifecycle events yet.</div>
    )
  }
  // Render newest first so the most recent activity is at the top.
  const ordered = [...events].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
  const verb = (t: ArtifactEvent['type']) => ({
    created: 'Created',
    edited: 'Edited',
    iterated: 'Iterated',
    referenced: 'Referenced',
    reverted: 'Reverted',
  }[t] ?? t)
  // Distinct hues per type so created/edited/iterated don't visually blur
  // together (nrb feedback). reverted uses warn (orange) to flag its
  // 'undo-style' semantics; iterated uses info (cyan) so agent-driven
  // updates visually separate from user edits (accent/violet).
  const dot = (t: ArtifactEvent['type']) => ({
    created: 'var(--ok)',
    edited: 'var(--accent)',
    iterated: 'var(--info)',
    referenced: 'var(--muted)',
    reverted: 'var(--warn)',
  }[t] ?? 'var(--muted)')
  // Some session_id values are markers, not real chat slots — skip the
  // 'from session …' link for those so users don't get sent to the wrong
  // slot. The dashboard's browser client uses 'dashboard:ui' for every
  // request; cron jobs prefix with 'cron:'. Real slot keys never contain
  // a colon (they're random IDs).
  const isRealSlotKey = (sk?: string) =>
    !!sk && sk !== 'dashboard:ui' && !sk.startsWith('cron:') && !sk.startsWith('ui:')
  return (
    <ul className="space-y-1.5 m-0 p-0 list-none">
      {ordered.map((ev, i) => (
        <li key={i} className="flex items-start gap-2 text-[12px]">
          <span
            className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: dot(ev.type) }}
            aria-hidden
          />
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium text-text">{verb(ev.type)}</span>
              {ev.by && <span className="text-muted">by {ev.by}</span>}
              {ev.type === 'reverted' && ev.from_version != null ? (
                <span className="text-muted">v{ev.from_version} → v{ev.version}</span>
              ) : (
                ev.version != null && <span className="text-muted">→ v{ev.version}</span>
              )}
              <span className="text-muted ml-auto">{formatEventTs(ev.ts)}</span>
            </div>
            {/* Source qualifier under the headline. For real chat slots this
                is a clickable link; for dashboard / cron / unknown markers
                it's plain muted text so users don't think it's actionable. */}
            {ev.session_id && isRealSlotKey(ev.session_id) ? (
              <button
                type="button"
                onClick={() => navigateToSlot(ev.session_id as string)}
                className="text-[11px] text-accent hover:underline cursor-pointer bg-transparent border-none p-0 mt-0.5"
                title={`Open session ${ev.session_id}`}
              >
                from session {ev.session_id}
              </button>
            ) : ev.type === 'reverted' && ev.from_version != null ? (
              <span className="text-[11px] text-muted mt-0.5">
                content copied from v{ev.from_version}
              </span>
            ) : ev.session_id === 'dashboard:ui' ? (
              <span className="text-[11px] text-muted mt-0.5">via dashboard</span>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  )
})

/** Renders a non-iframe artifact body — markdown / text / json / svg.
 * Theme vars are inherited naturally because we're not in a sandboxed
 * iframe; nothing to inject. When `editing` is true, swaps the preview
 * for a Monaco code editor. The `previewRef` is owned by the parent so
 * the detail page can attach selection-to-comment handlers above it. */
const ArtifactBodyNative = memo(function ArtifactBodyNative({
  kind, content, editing, onChange, previewRef,
}: {
  kind: Artifact['kind']
  content: string
  editing: boolean
  onChange: (v: string) => void
  previewRef: React.RefObject<HTMLDivElement | null>
}) {
  const fileType = fileTypeForKind(kind)
  const ext = extForKind(kind)
  const isRichType = fileType === 'json' || fileType === 'svg' || fileType === 'html' || fileType === 'image' || fileType === 'csv' || fileType === 'pdf'
  const isMarkdown = fileType === 'markdown'
  const lang = langFor(ext)
  const displayContent = isMarkdown ? content : wrapCode(content, ext)
  const highlightedHtml = useMemo(() => {
    if (isMarkdown || editing || isRichType) return ''
    try { return DOMPurify.sanitize(hljs.highlight(content, { language: lang }).value) + '\n' }
    catch { return DOMPurify.sanitize(hljs.highlightAuto(content).value) + '\n' }
  }, [content, lang, isMarkdown, editing, isRichType])
  return (
    <div
      className="rounded-xl border border-border bg-card overflow-auto"
      style={{ minHeight: 480, height: 'calc(100vh - 240px)' }}
    >
      <div className="p-5 h-full">
        <ContentRenderer
          isRichType={isRichType}
          fileType={fileType}
          content={content}
          editing={editing}
          lang={lang}
          lineNums={true}
          wordWrap={true}
          autocomplete={false}
          onChange={onChange}
          previewRef={previewRef}
          displayContent={displayContent}
          isMarkdown={isMarkdown}
          highlightedHtml={highlightedHtml}
          markdownClassName="msg-content text-sm leading-relaxed"
        />
      </div>
    </div>
  )
})

/** Renders widget / html artifacts in a sandboxed iframe with theme-var
 * injection — the existing path, untouched by Phase 2. */
const ArtifactBodyIframe = memo(function ArtifactBodyIframe({ artifact, slug, previewStyle }: { artifact: Artifact; slug: string; previewStyle?: React.CSSProperties }) {
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const srcdoc = useMemo(
    () => artifact.content ? buildSrcdoc({ html: artifact.content, themeVars, mode: theme }) : null,
    [artifact.content, themeVars, theme],
  )
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!srcdoc) return
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc])
  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden" style={{ minHeight: 480, ...previewStyle }}>
      {blobUrl ? (
        <iframe
          src={blobUrl}
          sandbox="allow-scripts"
          className="w-full border-none bg-card"
          style={{ height: 'calc(100vh - 240px)', minHeight: 480 }}
          title={`Artifact: ${slug}`}
        />
      ) : (
        <div className="p-6 text-muted">Rendering…</div>
      )}
    </div>
  )
})

export default function ArtifactDetailPage() {
  const { slug = '' } = useParams<{ slug: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const { theme, colorTheme, themeVersion } = useTheme()
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [editing, setEditing] = useState(false)
  // Round 8 polish: while editing, the user can flip to a rendered
  // preview of the edit buffer (matches the side panel's Edit/Preview
  // toggle). Stays in edit mode — content isn't committed until Save
  // and isn't discarded until Cancel.
  const [previewDuringEdit, setPreviewDuringEdit] = useState(false)
  const { readingWidth, toggle: toggleReadingWidth, previewStyle: mdPreviewStyle } = useReadingWidth()
  const [editedContent, setEditedContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  // Tag editing (Mesh-1654 round 4): tags shown in the header are editable
  // inline. Adding a tag posts metadata-only (no version bump). Removing a
  // tag works the same way.
  const [addingTag, setAddingTag] = useState(false)
  const [newTag, setNewTag] = useState('')
  // ── Inline-comment state (Phase 4) ────────────────────────────────────────
  // Comments are session-only here — there's no per-slug draft persistence
  // (file-viewer comments persist via `commentDrafts` keyed on filePath; for
  // artifacts the lifecycle log captures real history once Phase 5 lands).
  const [comments, setComments] = useState<InlineComment[]>([])
  const [popover, setPopover] = useState<{ x: number; y: number; anchor: string; line?: number; column?: number } | null>(null)
  const previewRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const selectingRef = useRef(false)

  // Reset version selection AND any in-progress edit when navigating between
  // artifacts. React Router v6 reuses the component instance for parameterized
  // routes, so without this reset, viewing v5 of one artifact then navigating
  // to another would attempt to fetch v5 of the new artifact (which may not
  // exist), and stale edit state would leak into the new artifact.
  useEffect(() => {
    setSelectedVersion(null)
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setComments([])
    setPopover(null)
    setAddingTag(false)
    setNewTag('')
  }, [slug])

  const detailQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    enabled: !!slug,
  })
  const versionsQuery = useQuery<{ slug: string; versions: number[] }>({
    queryKey: ['artifact-versions', slug],
    queryFn: () => api.artifactVersions(slug),
    enabled: !!slug,
  })
  const eventsQuery = useQuery<{ slug: string; events: ArtifactEvent[] }>({
    queryKey: ['artifact-events', slug],
    queryFn: () => api.artifactEvents(slug),
    enabled: !!slug,
  })

  const versions = versionsQuery.data?.versions || []
  const effectiveVersion = selectedVersion ?? detailQuery.data?.version ?? null
  // Live is the always-current state; numbered snapshots are historical
  // even when N == latest version. CRITICAL: do NOT treat selectedVersion
  // === detailQuery.data?.version as "current" — that conflates the
  // selected snapshot with Live and shows live content under a "vN" label,
  // which makes silent saves between snapshots look like they're mutating
  // historical versions (Mesh-1654 round 11 bug fix, found by nrb).
  const isCurrent = !selectedVersion

  const versionQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug, 'version', selectedVersion],
    queryFn: () => api.artifactVersion(slug, selectedVersion as number),
    enabled: !!slug && !!selectedVersion && !isCurrent,
  })

  const artifact = isCurrent ? detailQuery.data : versionQuery.data
  const editable = !!artifact && isEditableKind(artifact.kind) && isCurrent
  const dirty = editing && !!artifact && editedContent !== (artifact.content ?? '')

  // ── Tag editing handlers (Mesh-1654 round 4) ────────────────────────────
  const updateTagsMut = useCallback(async (newTags: string[]) => {
    if (!artifact) return
    setSaveError(null)
    try {
      await api.updateArtifact(artifact.slug, { tags: newTags })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      // Tags-only updates don't bump version, so no need to invalidate
      // versions or events queries.
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [artifact, queryClient, slug])

  const addTag = useCallback((raw: string) => {
    const cleaned = raw.trim().toLowerCase()
    if (!artifact || !cleaned) return
    if (artifact.tags.includes(cleaned)) {
      setNewTag('')
      setAddingTag(false)
      return
    }
    updateTagsMut([...artifact.tags, cleaned])
    setNewTag('')
    setAddingTag(false)
  }, [artifact, updateTagsMut])

  const removeTag = useCallback((tag: string) => {
    if (!artifact) return
    updateTagsMut(artifact.tags.filter(t => t !== tag))
  }, [artifact, updateTagsMut])

  // ── Edit / save / cancel / revert handlers ────────────────────────────────
  const startEditing = useCallback(() => {
    if (!artifact || !editable) return
    setEditedContent(artifact.content ?? '')
    setEditing(true)
    setSaveError(null)
  }, [artifact, editable])

  const cancelEditing = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setPreviewDuringEdit(false)
  }, [dirty])

  const handleSave = useCallback(async (snapshot = false) => {
    if (!artifact || !dirty) return
    setSaving(true)
    setSaveError(null)
    try {
      // snapshot=true → bumps version (creates a new numbered snapshot).
      // snapshot=false → silently updates the live state without versioning,
      // matching the explicit-snapshot model from Mesh-1654 round 5.
      await api.updateArtifact(artifact.slug, { content: editedContent, snapshot })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      if (snapshot) {
        await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
        await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
        // Snapshot is a deliberate checkpoint — drop out of edit mode
        // so the user sees the result. Plain Save (silent) keeps the
        // user in the editor (AutoSDE round 13 UX fix): after the query
        // refetches, artifact.content matches editedContent, dirty
        // becomes false, and the user can keep iterating.
        setEditing(false)
        setEditedContent('')
        setPreviewDuringEdit(false)
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, dirty, editedContent, queryClient, slug])

  // Stash for the keyboard handler effect — keeps deps minimal.
  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  // Snapshot the current live state without an edit. Used by the Snapshot
  // button when not editing — captures whatever is on disk / current.html
  // as a new numbered version. Mesh-1654 round 6: snapshot anytime live
  // differs from the latest numbered version (e.g. after silent saves or
  // external file edits to source_path).
  const handleSnapshotLive = useCallback(async () => {
    if (!artifact) return
    setSaving(true)
    setSaveError(null)
    try {
      // No content field — backend reads live state and snapshots it.
      await api.updateArtifact(artifact.slug, { snapshot: true })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, queryClient, slug])

  const handleRevert = useCallback(async () => {
    if (!artifact || !selectedVersion || isCurrent) return
    const targetVersion = selectedVersion
    const newVersion = (detailQuery.data?.version ?? 1) + 1
    const ok = window.confirm(
      `Revert to v${targetVersion}? This creates a new version (v${newVersion}) with v${targetVersion}'s content. The current state stays in version history.`,
    )
    if (!ok) return
    setSaving(true)
    setSaveError(null)
    try {
      // Fetch the historical version's content (versionQuery may already have
      // it, but going through the API ensures we don't fight an in-flight
      // refetch). Then write it as a new version via PATCH, tagged as a
      // 'reverted' event with the source version pinned so the activity
      // timeline can render it as a revert (not a generic edit) and skip
      // the broken 'from session dashboard:ui' link.
      const versionData = await api.artifactVersion(artifact.slug, targetVersion)
      await api.updateArtifact(artifact.slug, {
        content: versionData.content ?? '',
        event_type: 'reverted',
        from_version: targetVersion,
      })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
      setSelectedVersion(null)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, selectedVersion, isCurrent, detailQuery.data?.version, queryClient, slug])

  // Cmd+S / Ctrl+S to save; Esc to cancel edit.
  useEffect(() => {
    if (!editing) return
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's' && dirty) {
        e.preventDefault()
        // Cmd+Shift+S → snapshot (creates a new version), Cmd+S → silent save.
        handleSaveRef.current(e.shiftKey)
      }
      if (e.key === 'Escape') cancelEditing()
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [editing, dirty, cancelEditing])

  // Warn the browser about unsaved edits on close / reload / nav-away.
  useEffect(() => {
    if (!dirty) return
    const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  // ── Inline-comment handlers (Phase 4) ────────────────────────────────────
  // Comments only make sense for kinds where text→source coords resolve
  // cleanly: markdown (via data-sourcepos) and text (rendered === source).
  // JSON / SVG selection produces noisy anchors; revisit when there's a real
  // user demand.
  const commentable = !!artifact && !editing && isCurrent && (
    artifact.kind === 'markdown' || artifact.kind === 'text'
  )
  const isMarkdown = artifact?.kind === 'markdown'
  const sourceContent = artifact?.content ?? ''

  const handleMouseUp = useCallback(() => {
    if (!commentable) return
    const sel = window.getSelection()
    const raw = sel?.toString() ?? ''
    if (!sel || sel.isCollapsed || !raw.trim()) return
    const root = previewRef.current
    if (!root || !sel.anchorNode || !root.contains(sel.anchorNode)) return
    const range = sel.getRangeAt(0)
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return
    const anchor = raw.trim()
    const rect = range.getBoundingClientRect()
    // For markdown, walk the rendered DOM to map (anchorNode, offset) back to
    // (line, col) in the source via data-sourcepos. For text artifacts the
    // rendered text equals the source so findCoords is exact.
    const coords = isMarkdown
      ? (resolveSourcePos(range, root, sourceContent) ?? findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
      : (findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
    setPopover({ x: rect.left, y: rect.bottom, anchor, line: coords?.line, column: coords?.column })
  }, [commentable, isMarkdown, sourceContent])

  const addComment = useCallback((text: string) => {
    if (!popover) return
    const newComment: InlineComment = {
      id: Math.random().toString(36).substring(2),
      anchor: popover.anchor,
      text,
      line: popover.line,
      column: popover.column,
    }
    setComments(prev => [...prev, newComment])
    setPopover(null)
    window.getSelection()?.removeAllRanges()
  }, [popover])

  const removeComment = useCallback((id: string) => {
    setComments(prev => prev.filter(c => c.id !== id))
  }, [])

  const editComment = useCallback((id: string, text: string) => {
    setComments(prev => prev.map(c => c.id === id ? { ...c, text } : c))
  }, [])

  // Build the chat-injection prompt: prefix with `Iterate on artifact <slug>:`
  // so the agent knows the subject. If pending comments exist, attach the
  // structured comment block; otherwise emit just the prefix and let the
  // user type their request after the colon.
  const buildPromptForChat = useCallback((): string => {
    if (!artifact) return ''
    const header = `Iterate on artifact \`${artifact.slug}\` (${artifact.name})`
    if (comments.length === 0) return header + ': '
    return header + ' with these comments:\n\n' +
      formatCommentsMessage(`artifact:${artifact.slug}`, comments, sourceContent)
  }, [artifact, comments, sourceContent])

  /** Open a fresh chat slot pre-loaded with this artifact in the input.
   * Always creates a NEW session so historical context from unrelated
   * conversations doesn't contaminate the iterate loop. The user reviews
   * the prefill and clicks Send — we never auto-send because comment
   * dumps can be long and may need editing.
   *
   * Works regardless of pending comment state: the comment-less path
   * (just `Iterate on artifact <slug>: `) is the primary "discuss this"
   * entry point and is available for ALL artifact kinds (including
   * widgets that can't be edited inline).
   */
  const iterateWithAgent = useCallback(async () => {
    if (!artifact) return
    const prompt = buildPromptForChat()
    try {
      const res = await api.createChatSlot(`Artifact: ${artifact.name}`)
      sessionStorage.setItem(
        PREFILL_STORAGE_KEY,
        JSON.stringify({ slotKey: res.key, prompt, ts: Date.now() }),
      )
      dispatch(switchSlot(res.key))
      setComments([])
      navigate('/chat')
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [artifact, buildPromptForChat, dispatch, navigate])

  // Drop popover when the user switches to edit mode or pages between
  // versions — those interactions kill the underlying selection anyway.
  useEffect(() => { if (editing || !isCurrent) { setPopover(null) } }, [editing, isCurrent])

  // ── Export helpers (Open-in-new-tab + Download) ───────────────────────────
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const usesIframe = artifact?.kind === 'widget' || artifact?.kind === 'html'
  const exportSrcdoc = useMemo(
    () => artifact?.content && usesIframe
      ? buildSrcdoc({ html: artifact.content, themeVars, mode: theme })
      : null,
    [artifact?.content, themeVars, theme, usesIframe],
  )

  const downloadAsHtml = () => {
    if (!artifact) return
    const isMarkdownLike = artifact.kind === 'markdown' || artifact.kind === 'text' || artifact.kind === 'json' || artifact.kind === 'svg'
    const blobBody = exportSrcdoc ?? artifact.content ?? ''
    const mime = isMarkdownLike
      ? (artifact.kind === 'json' ? 'application/json' : artifact.kind === 'svg' ? 'image/svg+xml' : 'text/plain')
      : 'text/html'
    const ext = artifact.kind === 'markdown' ? 'md'
      : artifact.kind === 'json' ? 'json'
      : artifact.kind === 'svg' ? 'svg'
      : artifact.kind === 'text' ? 'txt'
      : 'html'
    const blob = new Blob([blobBody], { type: mime })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    const safeName = artifact.name.replace(/[^a-zA-Z0-9-_ ]/g, '')
    a.download = `${safeName || artifact.slug}-v${effectiveVersion}.${ext}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
  }

  const openInNewTab = () => {
    if (!artifact) return
    if (exportSrcdoc) {
      const doc = document.implementation.createHTMLDocument(artifact.name || slug)
      const charsetMeta = doc.createElement('meta')
      charsetMeta.setAttribute('charset', 'utf-8')
      doc.head.insertBefore(charsetMeta, doc.head.firstChild)
      doc.body.style.margin = '0'
      doc.body.style.height = '100vh'
      const iframe = doc.createElement('iframe')
      iframe.setAttribute('sandbox', 'allow-scripts')
      iframe.setAttribute('srcdoc', exportSrcdoc)
      iframe.style.width = '100%'
      iframe.style.height = '100%'
      iframe.style.border = 'none'
      doc.body.appendChild(iframe)
      const html = `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      window.open(url, '_blank')
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
      return
    }
    // markdown / text / json / svg — open the raw content in a new tab so
    // the browser renders it with its native viewer.
    const mime = artifact.kind === 'json' ? 'application/json'
      : artifact.kind === 'svg' ? 'image/svg+xml'
      : 'text/plain;charset=utf-8'
    const blob = new Blob([artifact.content ?? ''], { type: mime })
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }

  if (detailQuery.isLoading || (!isCurrent && versionQuery.isLoading))
    return <div className="p-6 text-muted">Loading…</div>
  if (detailQuery.error) {
    const msg = detailQuery.error instanceof Error ? detailQuery.error.message : String(detailQuery.error)
    return (
      <>
        <PageHeader title="Artifact" subtitle={slug} />
        <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <Card>
            <div className="flex items-start gap-3">
              <AlertTriangle className="lucide-inline text-danger" />
              <div>
                <div className="text-sm text-danger font-medium">Failed to load artifact</div>
                <div className="text-[13px] text-muted mt-1">{msg}</div>
              </div>
            </div>
            <div className="mt-3">
              <Btn onClick={() => navigate('/artifacts')}>← Back to library</Btn>
            </div>
          </Card>
        </div>
      </>
    )
  }
  if (!artifact) return <div className="p-6 text-muted">Not found.</div>

  const sel =
    'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring'

  // Cron-source warning shown only while editing — surface the foot-gun
  // (next cron run will create a newer version) without noisy chrome on
  // read-only views.
  const showCronWarning = editing && artifact.source === 'cron'

  return (
    <>
      <PageHeader title={artifact.name} subtitle={`Artifact: ${artifact.slug}`} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <Btn onClick={() => {
            if (dirty && !window.confirm('Discard unsaved changes?')) return
            navigate('/artifacts')
          }} className="flex items-center gap-1">
            <ArrowLeft size={13} /> Back
          </Btn>
          <Badge variant="aim">{artifact.kind}</Badge>
          {artifact.tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted group">
              {t}
              <button
                type="button"
                onClick={() => removeTag(t)}
                className="opacity-0 group-hover:opacity-100 hover:text-danger transition-opacity bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
                title={`Remove tag ${t}`}
                aria-label={`Remove tag ${t}`}
              >
                <X size={10} />
              </button>
            </span>
          ))}
          {addingTag ? (
            <input
              type="text"
              value={newTag}
              onChange={e => setNewTag(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') addTag(newTag)
                if (e.key === ',' || e.key === ' ') {
                  e.preventDefault()
                  if (newTag.trim()) addTag(newTag)
                }
                if (e.key === 'Escape') { setNewTag(''); setAddingTag(false) }
              }}
              onBlur={() => {
                if (newTag.trim()) addTag(newTag)
                else setAddingTag(false)
              }}
              autoFocus
              placeholder="tag…"
              className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-accent text-text outline-none"
              style={{ width: '90px' }}
              aria-label="Add a tag"
            />
          ) : (
            <button
              type="button"
              onClick={() => setAddingTag(true)}
              className="inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded border border-dashed border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors"
              title="Add a tag (comma-separated tags supported)"
              aria-label="Add a tag"
            >
              <Plus size={10} /> tag
            </button>
          )}
          <span className="ml-auto flex items-center gap-2 text-[13px] text-muted">
            {/* Iterate button — primary action for all artifact kinds.
                Available even when comments are empty: opens the current
                chat with `Iterate on artifact <slug>:` pre-filled so the
                user can finish the prompt themselves. For widgets (which
                can't be edited inline) this is the ONLY way to ask the
                agent to change the artifact. */}
            {!editing && (
              <button
                type="button"
                onClick={iterateWithAgent}
                className="px-2 py-1 rounded-md text-[12px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover hover:shadow-[0_0_12px_var(--accent-glow)] transition-all"
                title={comments.length > 0
                  ? `Discuss with agent (${comments.length} comment${comments.length === 1 ? '' : 's'} attached)`
                  : 'Discuss this artifact with the agent'}
              >
                <span className="inline-flex items-center gap-1">
                  <Sparkles size={12} /> Iterate
                  {comments.length > 0 && (
                    <span className="ml-1 px-1 rounded bg-accent-fg/20 text-[10px]">
                      {comments.length}
                    </span>
                  )}
                </span>
              </button>
            )}
            {/* Editing controls — Save / Snapshot / Cancel only when editing; Edit otherwise */}
            {editing ? (
              <>
                <button
                  type="button"
                  onClick={() => handleSave(false)}
                  disabled={!dirty || saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`}
                  title="Save to Live (Cmd+S) — updates the live state without versioning"
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                  type="button"
                  onClick={() => handleSave(true)}
                  disabled={!dirty || saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title="Snapshot (Cmd+Shift+S) — save and create a new version"
                >
                  <span className="inline-flex items-center gap-1"><Camera size={12} /> Snapshot</span>
                </button>
                <button
                  type="button"
                  onClick={cancelEditing}
                  disabled={saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title="Cancel (Esc)"
                >
                  <span className="inline-flex items-center gap-1"><X size={12} /> Cancel</span>
                </button>
                {/* Preview toggle (round 8): peek at the rendered output of
                    the edit buffer without leaving edit mode. Mirrors the
                    side panel's Edit/Preview toggle. */}
                <button
                  type="button"
                  onClick={() => setPreviewDuringEdit(p => !p)}
                  disabled={saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all disabled:opacity-40 ${previewDuringEdit ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  title={previewDuringEdit ? 'Back to editor' : 'Preview rendered output of current edits'}
                >
                  {previewDuringEdit ? 'Edit' : 'Preview'}
                </button>
              </>
            ) : (
              <>
                {/* Snapshot: only shown when live differs from the
                    latest numbered version (Mesh-1654 round 6). Enabled
                    even without an active edit because silent saves and
                    external file edits drift live ahead of history. */}
                {isCurrent && artifact.live_dirty && (
                  <button
                    type="button"
                    onClick={handleSnapshotLive}
                    disabled={saving}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                    title="Snapshot — capture the current state as a new version"
                  >
                    <span className="inline-flex items-center gap-1"><Camera size={12} /> Snapshot</span>
                  </button>
                )}
                {editable && (
                  <button
                    type="button"
                    onClick={startEditing}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
                    title="Edit content"
                  >
                    <span className="inline-flex items-center gap-1"><Pencil size={12} /> Edit</span>
                  </button>
                )}
              </>
            )}

            <span>Version</span>
            <select
              className={sel}
              disabled={saving}
              value={selectedVersion === null ? 'live' : String(selectedVersion)}
              onChange={(e) => {
                if (dirty && !window.confirm('Discard unsaved changes?')) return
                setEditing(false)
                setEditedContent('')
                const raw = e.target.value
                if (raw === 'live') {
                  setSelectedVersion(null)
                } else {
                  setSelectedVersion(parseInt(raw, 10))
                }
              }}
            >
              {/* Live = always-current state. Distinct from any numbered
                  snapshot because in the explicit-snapshot model saves
                  update Live without bumping versions, so Live can be
                  ahead of the latest numbered snapshot. */}
              <option value="live">Live</option>
              {versions.slice().reverse().map((v) => (
                <option key={v} value={v}>
                  v{v}
                </option>
              ))}
            </select>

            {/* Revert: only meaningful when viewing a historical version */}
            {!isCurrent && (
              <button
                type="button"
                onClick={handleRevert}
                disabled={saving}
                className="p-1.5 rounded text-warn hover:text-warn transition-colors cursor-pointer bg-transparent border border-warn/40 disabled:opacity-40"
                title={`Revert to v${selectedVersion}`}
                aria-label={`Revert to v${selectedVersion}`}
              >
                <RotateCcw size={13} />
              </button>
            )}

            {(!editing || previewDuringEdit) && (
              <ReadingWidthToggle value={readingWidth} onToggle={toggleReadingWidth} />
            )}
            <button
              type="button"
              onClick={openInNewTab}
              className="p-1.5 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border border-border"
              title="Open in new tab"
              aria-label="Open in new tab"
            >
              <ExternalLink size={13} />
            </button>
            <button
              type="button"
              onClick={downloadAsHtml}
              className="p-1.5 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border border-border"
              title="Download"
              aria-label="Download"
            >
              <Download size={13} />
            </button>
          </span>
        </div>

        {artifact.description && (
          <div className="mb-3 text-sm text-muted italic">{artifact.description}</div>
        )}

        {showCronWarning && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[13px] text-warn">
            <AlertCircle size={14} className="lucide-inline shrink-0 mt-0.5" />
            <span>
              <strong>Heads up:</strong> this artifact is regenerated by a cron job. Your edits will be preserved in version history, but the next cron run will create a newer version that overrides what you save here.
            </span>
          </div>
        )}

        {saveError && (
          <div className="mb-3 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">
            <strong>Save failed:</strong> {saveError}
          </div>
        )}

        {usesIframe ? (
          <ArtifactBodyIframe artifact={artifact} slug={slug} previewStyle={mdPreviewStyle} />
        ) : (
          <div
            ref={bodyRef}
            className="relative"
            style={mdPreviewStyle}
            onMouseDown={() => { selectingRef.current = true }}
            onMouseUp={() => { selectingRef.current = false; handleMouseUp() }}
          >
            <ArtifactBodyNative
              kind={artifact.kind}
              content={editing ? editedContent : (artifact.content ?? '')}
              editing={editing && !previewDuringEdit}
              onChange={setEditedContent}
              previewRef={previewRef}
            />
            {popover && (
              <CommentPopover
                x={popover.x}
                y={popover.y}
                onSubmit={addComment}
                onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }}
                containerRef={bodyRef}
              />
            )}
          </div>
        )}

        {/* Pending comments list. The 'Iterate' button in the header is
            the primary submit affordance — these only show as a list of
            what's pending. Mesh-1654 Phase 4. */}
        {commentable && comments.length > 0 && (
          <div className="mt-3 space-y-2">
            <CommentList
              comments={comments}
              onEdit={editComment}
              onRemove={removeComment}
              onSubmitAll={iterateWithAgent}
            />
            <div className="text-[12px] text-muted">
              {comments.length} pending comment{comments.length === 1 ? '' : 's'} —
              use the <strong>Iterate</strong> button above to send them to a
              fresh chat session.
            </div>
          </div>
        )}

        <div className="mt-3 text-[12px] text-muted">
          Created {artifact.created_at} &middot; Updated {artifact.updated_at} &middot;{' '}
          {/* "Live" reflects the always-current state. Numbered versions
              are historical snapshots — when one is selected, isCurrent is
              false (because the dropdown is non-Live). */}
          {selectedVersion === null
            ? `Showing Live (v${detailQuery.data?.version ?? '?'})`
            : `Showing v${effectiveVersion} (historical)`}
          {dirty && <span className="ml-2 text-warn">• unsaved changes</span>}
          {commentable && comments.length === 0 && (
            <span className="ml-2 text-muted/80">Tip: select text to add inline comments, or click <strong>Iterate</strong> to chat with the agent.</span>
          )}
          {!commentable && !editing && isCurrent && (
            <span className="ml-2 text-muted/80">Tip: click <strong>Iterate</strong> to chat with the agent about this artifact.</span>
          )}
        </div>

        {/* Phase 5 (Mesh-1654): lifecycle event log + activity timeline. */}
        <div className="mt-6">
          <h3 className="text-[13px] font-semibold text-text-strong mb-2">Activity</h3>
          <ActivityTimeline
            events={eventsQuery.data?.events ?? []}
            navigateToSlot={(slotKey) => {
              dispatch(switchSlot(slotKey))
              navigate('/chat')
            }}
          />
        </div>
      </div>
    </>
  )
}
