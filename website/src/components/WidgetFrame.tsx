import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { motion } from 'framer-motion'
import { Maximize2, Minimize2, ExternalLink, Download, Bookmark } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { effectiveWidgetSlug } from '../lib/widgetSlug'
import { api, ApiError } from '../api/client'

const MIN_HEIGHT = 80
const MAX_HEIGHT = 500

// Height cache is theme-independent: every entry in THEME_VAR_NAMES is a
// color var, never a size. If a length/size var is ever added to the list,
// include it in the cache key so heights don't get reused across themes.
const heightCache = new Map<string, number>()

function contentHash(html: string): string {
  let h = 0
  for (let i = 0; i < html.length; i++) {
    h = ((h << 5) - h + html.charCodeAt(i)) | 0
  }
  return String(h)
}

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

interface WidgetFrameProps {
  html: string
  title?: string
  /** Explicit slug attribute on `<mcwidget slug="...">`. When the agent
   * re-emits a previously-saved artifact it MUST include this attribute so
   * the impression binds to the same artifact. For brand-new emissions the
   * agent may omit it and we derive a stable slug from `messageTs +
   * widgetIndex` instead. */
  slug?: string
  /** Parent message timestamp. Threaded through from AssistantMessage so
   * widgets without an explicit slug get a stable, location-anchored
   * identity that survives refreshes and prevents save-then-refresh
   * duplicate creation. */
  messageTs?: string
  /** 0-based ordinal of this widget within the parent message. Two
   * `<mcwidget>` tags in the same message disambiguate by this index. */
  widgetIndex?: number
}

export default function WidgetFrame({ html, title = 'Widget', slug, messageTs, widgetIndex }: WidgetFrameProps) {
  // Re-read theme CSS vars whenever the resolved theme, active color theme,
  // or themeVersion counter changes. themeVersion is the trigger for
  // in-place custom-theme edits via the theme editor: the slug stays the
  // same but the injected CSS values change, so theme/colorTheme alone
  // wouldn't fire the memo. useTheme bumps themeVersion on every
  // loadCustomThemes completion and on every applyTheme.
  const { theme, colorTheme, themeVersion } = useTheme()
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [expanded, setExpanded] = useState(false)
  const key = useMemo(() => contentHash(html), [html])
  const [height, setHeight] = useState(() => heightCache.get(key) ?? 200)
  // theme + colorTheme + themeVersion are deps, not inputs: readThemeVars
  // pulls from document.documentElement, so the memo must re-run whenever
  // anything upstream has mutated the computed CSS vars there. React's
  // exhaustive-deps rule can't see through external state sync.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const srcdoc = useMemo(
    () => buildSrcdoc({ html, themeVars, mode: theme, includeHeightReporter: true }),
    [html, themeVars, theme],
  )

  // Use blob: URL instead of srcdoc to avoid inheriting parent CSP.
  // Parent CSP script-src 'self' blocks inline scripts in srcdoc iframes
  // (per W3C spec, srcdoc inherits parent CSP). blob: has its own opaque
  // origin and is not subject to the parent's CSP restrictions.
  // Managed in useEffect (not useMemo) to ensure create/revoke are always
  // paired, even under concurrent rendering or strict mode.
  const [blobUrl, setBlobUrl] = useState<string | null>(null)

  useEffect(() => {
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc])

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type === 'mc-widget-height' && typeof e.data.height === 'number') {
        const h = Math.min(Math.max(e.data.height, MIN_HEIGHT), MAX_HEIGHT)
        setHeight(h)
        heightCache.set(key, h)
      }
      if (e.data?.type === 'mc-widget-action') {
        const { action, payload } = e.data
        const text = payload && Object.keys(payload).length > 0
          ? `[UI] ${action}: ${JSON.stringify(payload)}`
          : `[UI] ${action}`
        window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text } }))
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [key])

  const openInNewTab = useCallback(() => {
    // Build the wrapper via DOM API instead of template literals: the browser
    // handles attribute escaping and HTML serialization, so LLM-generated
    // srcdoc/title can't break out of the document. The blob page sandboxes
    // the inner iframe so its origin doesn't grant LLM content access to the
    // parent app's cookies/storage.
    const doc = document.implementation.createHTMLDocument(title)
    const charsetMeta = doc.createElement('meta')
    charsetMeta.setAttribute('charset', 'utf-8')
    doc.head.insertBefore(charsetMeta, doc.head.firstChild)
    doc.body.style.margin = '0'
    doc.body.style.height = '100vh'
    const iframe = doc.createElement('iframe')
    iframe.setAttribute('sandbox', 'allow-scripts')
    iframe.setAttribute('srcdoc', srcdoc)
    iframe.style.width = '100%'
    iframe.style.height = '100%'
    iframe.style.border = 'none'
    doc.body.appendChild(iframe)

    const html = `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }, [srcdoc, title])

  const downloadAsHtml = useCallback(() => {
    // Note: downloaded HTML runs with file:// origin when opened locally.
    // This is expected for an explicit download action — the user chose
    // to save the file. The content is LLM-generated, same as any code
    // the agent writes to disk.
    const blob = new Blob([srcdoc], { type: 'text/html' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${title.replace(/[^a-zA-Z0-9-_ ]/g, '')}.html`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
  }, [srcdoc, title])

  // Save the widget body as a persistent artifact. We send the *inner* HTML
  // (the user-visible widget body, not the wrapped srcdoc with theme blocks),
  // so the artifact stays portable across themes and renders identically when
  // opened on /artifacts/<slug>.

  // Determine the effective slug for this impression. Priority:
  //  1. Explicit `slug` attribute from the agent (used when re-emitting a
  //     known saved artifact — see artifacts skill).
  //  2. Derived from `messageTs + widgetIndex` — stable across refreshes,
  //     so saving once and refreshing doesn't create a duplicate.
  // Returns null only when neither is available (streaming/detached
  // widgets, or test fixtures); in that case bookmark is disabled.
  const effectiveSlug = useMemo(
    () => effectiveWidgetSlug({
      explicitSlug: slug,
      messageTs,
      widgetIndex,
    }),
    [slug, messageTs, widgetIndex],
  )
  // savedSlug === effectiveSlug when the artifact exists on the server,
  // null when it doesn't. Initialized optimistically from the explicit
  // slug attribute (so re-emissions render filled before the GET round-
  // trip completes), then reconciled with server truth via the mount /
  // visibilitychange effect below.
  const [savedSlug, setSavedSlug] = useState<string | null>(slug ?? null)
  // Sync savedSlug eagerly when the agent re-emits this widget with a
  // different explicit slug (e.g. after artifact_update renames it). The
  // verify effect below also reconciles via GET, but that's an async
  // round-trip — until it lands, the title link would still point at the
  // stale slug. This keeps the title link in lock-step with the prop.
  useEffect(() => {
    if (slug) setSavedSlug(slug)
  }, [slug])
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Track mount status so async save/remove callbacks can skip side-effects
  // when the component has been unmounted mid-flight (e.g. user navigated
  // away, or the chat scrolled the widget out of view between the bookmark
  // click and the API response).
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // Reconcile bookmark state with server truth. Fires on mount and on
  // tab visibilitychange, so bookmarks across tabs / sessions catch up
  // when the user returns to a tab (covering the cross-tab consistency
  // case without WebSockets / BroadcastChannel complexity).
  //
  // GET 200 → artifact exists at this slug → bookmark filled.
  // GET 404 → no artifact at this slug → bookmark empty.
  // No effective slug (no explicit attr + no message context) → leave
  // savedSlug alone; the bookmark will be disabled in the UI.
  useEffect(() => {
    if (!effectiveSlug) return
    let cancelled = false
    const verify = async () => {
      try {
        await api.artifact(effectiveSlug)
        if (cancelled || !mountedRef.current) return
        setSavedSlug(effectiveSlug)
      } catch (e) {
        if (cancelled || !mountedRef.current) return
        if (e instanceof ApiError && e.status === 404) {
          setSavedSlug(null)
        }
        // Other status codes (auth, server error, network): leave state
        // untouched so a transient hiccup doesn't flap the icon.
      }
    }
    verify()
    const onVisibility = () => {
      if (document.visibilityState === 'visible') verify()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [effectiveSlug])

  const saveAsArtifact = useCallback(async () => {
    if (saving || savedSlug || !effectiveSlug) return
    // Atomic one-click save with a deterministic slug — POST goes to the
    // exact slug for this widget impression. If the artifact already
    // exists at that slug (e.g. a rapid double-click race, or the user
    // saved in another tab and the verify-on-mount hasn't reconciled
    // yet), the server returns 409 and we treat that as "already saved":
    // sync local state to match server truth, no error shown.
    const name = title && title !== 'Widget' ? title : 'Widget'
    setSaving(true)
    setSaveError(null)
    try {
      const r = await api.createArtifact({
        name,
        content: html,
        kind: 'widget',
        source: 'chat',
        slug: effectiveSlug,
      })
      if (!mountedRef.current) return
      setSavedSlug(r?.slug || effectiveSlug)
    } catch (e) {
      if (!mountedRef.current) return
      // 409 conflict → artifact already exists at this slug, treat as
      // already-saved (covers double-click races + cross-tab saves where
      // the verify-on-mount hasn't reconciled yet).
      if (e instanceof ApiError && e.status === 409) {
        setSavedSlug(effectiveSlug)
      } else {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [html, title, saving, savedSlug, effectiveSlug])

  const removeArtifact = useCallback(async () => {
    if (saving || !savedSlug) return
    // Atomic un-bookmark — no confirm. Mirrors the one-click save: clicking
    // the bookmark again removes the artifact. If the user wants the heavier
    // "are you sure" prompt, they can use the Delete button on the Artifacts
    // library page (/artifacts), which keeps a confirm because it's an
    // explicit destructive action far from the save context.
    setSaving(true)
    setSaveError(null)
    try {
      await api.deleteArtifact(savedSlug)
      if (!mountedRef.current) return
      setSavedSlug(null)
    } catch (e) {
      if (!mountedRef.current) return
      // 404 → already gone (deleted from another tab / library page).
      // Reconcile to the empty state silently.
      if (e instanceof ApiError && e.status === 404) {
        setSavedSlug(null)
      } else {
        setSaveError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }, [savedSlug, saving])

  const toggleArtifact = savedSlug ? removeArtifact : saveAsArtifact

  return (
    <motion.div
      initial={{ scale: 0.95, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      className={`rounded-xl border border-border bg-card overflow-hidden transition-all my-2 ${expanded ? 'fixed inset-4 z-50 shadow-2xl' : 'max-h-[540px]'}`}
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-bg-elevated">
        <span className="text-[13px] font-medium text-text truncate">
          {savedSlug ? (
            <a
              href={`/artifacts/${savedSlug}`}
              className="text-text hover:text-accent hover:underline"
              title={`Open artifact "${savedSlug}"`}
            >{title}</a>
          ) : (
            title
          )}
          {saveError && <span className="ml-2 text-[12px] text-danger" title={saveError}>save failed</span>}
        </span>
        <div className="flex items-center gap-1">
          <button
            onClick={toggleArtifact}
            disabled={saving || !effectiveSlug}
            className={`p-1 rounded transition-colors cursor-pointer bg-transparent border-none ${savedSlug ? 'text-accent' : 'text-muted hover:text-text'} ${saving ? 'opacity-50 cursor-wait' : ''} ${!effectiveSlug ? 'opacity-30 cursor-not-allowed' : ''}`}
            title={
              !effectiveSlug
                ? 'Cannot save: widget has no slug or message context'
                : savedSlug
                  ? `Saved as "${savedSlug}" — click to remove`
                  : 'Save as artifact'
            }
            aria-label={savedSlug ? `Remove artifact ${savedSlug} from library` : 'Save as artifact'}
          >
            <Bookmark size={13} fill={savedSlug ? 'currentColor' : 'none'} />
          </button>
          <button onClick={downloadAsHtml} className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none" title="Download as HTML" aria-label="Download as HTML">
            <Download size={13} />
          </button>
          <button onClick={openInNewTab} className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none" title="Open in new tab" aria-label="Open in new tab">
            <ExternalLink size={13} />
          </button>
          <button onClick={() => setExpanded(!expanded)} className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none" title={expanded ? 'Minimize' : 'Expand'} aria-label={expanded ? 'Minimize' : 'Expand'}>
            {expanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
          </button>
        </div>
      </div>

      {blobUrl && <iframe
        ref={iframeRef}
        src={blobUrl}
        sandbox="allow-scripts"
        className="w-full border-none bg-card"
        style={{ height: expanded ? 'calc(100% - 36px)' : Math.min(height, MAX_HEIGHT) }}
        title={title}
      />}

      {expanded && (
        <div className="fixed inset-0 bg-black/40 -z-10" onClick={() => setExpanded(false)} />
      )}
    </motion.div>
  )
}
