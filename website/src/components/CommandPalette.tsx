import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Search, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import type { Result, ResourceProvider } from './commandPalette/types'
import { registerProvider } from './commandPalette/providers'
import { usePaletteActions } from './commandPalette/paletteActions'
import { useAllAggregator } from './commandPalette/providers/allAggregator'
import { useSessionsProvider } from './commandPalette/providers/sessionsProvider'
import type { SessionRef } from './commandPalette/providers/sessionsProvider'
import { usePagesProvider } from './commandPalette/providers/pagesProvider'
import { useActionsProvider } from './commandPalette/providers/actionsProvider'
import { useKnowledgeProvider } from './commandPalette/providers/knowledgeProvider'
import type { KnowledgeRef } from './commandPalette/providers/knowledgeProvider'
import { useSkillsProvider } from './commandPalette/providers/skillsProvider'
import { usePromptsProvider } from './commandPalette/providers/promptsProvider'

/**
 * Search Everywhere command palette (Mesh-2151).
 *
 * A portal-rendered, centered modal — the IntelliJ-style "search everything"
 * surface. It owns:
 *  - the search input,
 *  - a tab strip (All · Sessions · Knowledge · Skills · Prompts, with Pages +
 *    Actions riding along),
 *  - a result list with matched-character highlighting.
 *
 * Keyboard model (reuses {@link useListKeyboardNav} verbatim for the row
 * selection + scroll-into-view that the picker menus already share):
 *  - Arrow Up / Down       — move the selection.
 *  - Enter                 — `dispatchEnter(result, false)` (then close).
 *  - ⌘/Ctrl + Enter        — `dispatchEnter(result, true)` — the modifier
 *                            branch of the §2 Enter matrix (always-new-session /
 *                            attach-as-context); then close.
 *  - ⌥/Alt + Enter         — `result.onAltActivate()` (read / preview, e.g.
 *                            open SKILL.md); does not close the palette.
 *  - Tab / Shift+Tab       — cycle the active category tab (scopes results to
 *                            one provider). Never "chooses" a row here.
 *  - Escape                — close.
 *
 * Enter and ⌘/Ctrl+Enter both flow through one central {@link dispatchEnter}
 * ({@link OnEnter}): the shared {@link useListKeyboardNav} hook computes the
 * `withModifier` flag (`metaKey || ctrlKey`) on the Enter keypress and threads
 * it into `onChoose(index, withModifier)`; `dispatchEnter` looks the row up and
 * switches on its declarative {@link EnterAction} (`result.enter`), falling back
 * to the legacy `on*Activate` closures while providers are migrated.
 *
 * `useListKeyboardNav` maps Tab to "choose" (its picker-menu default). To take
 * over Tab (cycle category) and ⌥/Alt+Enter (preview) without forking the
 * shared hook, this component registers a *window*-phase capture listener:
 * window-capture fires before document-capture, so it can
 * `stopImmediatePropagation()` those two keys before the hook's document-level
 * listener sees them.
 *
 * Highlighting renders matched indices as React `<strong>` nodes split out of
 * the title — never `dangerouslySetInnerHTML` (AUTOSDE `frontend-security`).
 * Visuals reuse the shared design tokens + the portal/result-row pattern from
 * `SkillPickerMenu.tsx`; no hardcoded Tailwind colors.
 */

export interface CommandPaletteProps {
  /** Whether the palette is shown. */
  open: boolean
  /** Close the palette (Escape, overlay click, or after activation). */
  onClose: () => void
  /**
   * Open the keyboard-shortcuts help modal (backs the Actions provider's
   * "Open Shortcuts" command). Supplied by the app shell, which owns the
   * `ShortcutsModal` open state. Defaults to a no-op.
   */
  openShortcuts?: () => void
  /**
   * Open a session in a split pane / Session Grid (⌘Enter on a session row).
   * Optional — when omitted, ⌘Enter on a session falls back to plain open.
   */
  openInSplit?: (ref: SessionRef) => void
}

/** Stable no-op so `useActionsProvider` always gets a defined callback. */
const NOOP = () => {}

/**
 * Render `text` with the characters at `indices` emphasised. Each character is
 * its own span/strong node keyed by position — safe (no HTML string building)
 * and good enough for short titles.
 */
function Highlighted({ text, indices }: { text: string; indices: number[] }): ReactNode {
  if (indices.length === 0) return text
  const hit = new Set(indices)
  const nodes: ReactNode[] = []
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    nodes.push(
      hit.has(i) ? (
        <strong key={i} className="text-text-strong font-semibold">
          {ch}
        </strong>
      ) : (
        <span key={i}>{ch}</span>
      ),
    )
  }
  return <>{nodes}</>
}

export default function CommandPalette({
  open,
  onClose,
  openShortcuts,
  openInSplit,
}: CommandPaletteProps) {
  // P0 providers. Each is memoized inside its hook, so identities are stable.
  const all = useAllAggregator()
  const sessions = useSessionsProvider({ openInSplit })
  const pages = usePagesProvider()
  const actions = useActionsProvider({ openShortcuts: openShortcuts ?? NOOP })

  // Live chat-store/router actions backing the §2 Enter matrix. `usePaletteActions`
  // exposes the same composer-insert (`setPendingInput`) and new-session
  // (`createSlot` + `setPendingInput`) paths the inline `$`/`@` pickers use, plus
  // the context-aware `enterInsertOrNewSession` helper (insert into the active
  // chat, else open a new seeded session). Token resolution stays server-side on
  // submit (BSC1) — the palette only ever emits a plain `$skill` / `@prompt` /
  // `@knowledge` string. Used by the `insert-token` + `open-knowledge` branches
  // of {@link dispatchEnter}.
  const { enterInsertOrNewSession, newSessionWithToken } = usePaletteActions()

  // ⌘Enter on a Knowledge row attaches the entry as context to the active chat
  // (Mesh-2151 §2 / task 26). There is no global attach-by-id API — the shipped
  // path is the `@knowledge <query>` composer prefix the chat surface intercepts
  // (`useKnowledgeFetch.extractKnowledgeQuery`) to pull entries in as context.
  // So we reuse that entry-point: seed the active chat (else a new session) with
  // `@knowledge <title>`. BSC1: a plain token string — the FE picker / server
  // search resolve it; no filesystem access from user-controlled values.
  const attachKnowledgeAsContext = useCallback(
    (ref: KnowledgeRef) => enterInsertOrNewSession(`@knowledge ${ref.title}`),
    [enterInsertOrNewSession],
  )

  // P1 providers (Knowledge · Skills · Prompts). Each is memoized inside its
  // hook, so identities are stable across renders. Knowledge gets the
  // attach-as-context callback so its ⌘Enter is bound (task 26).
  const knowledge = useKnowledgeProvider({ attachAsContext: attachKnowledgeAsContext })
  const skills = useSkillsProvider()
  const prompts = usePromptsProvider()

  // Tab strip order (Mesh-2151 §1): All · Sessions · Knowledge · Skills ·
  // Prompts, with Pages + Actions riding along after the v1 corpus.
  const tabs = useMemo<ResourceProvider[]>(
    () => [all, sessions, knowledge, skills, prompts, pages, actions],
    [all, sessions, knowledge, skills, prompts, pages, actions],
  )

  // Make the per-category providers discoverable by the All aggregator, which
  // fans the query out to everything in the registry (so the P1 providers are
  // interleaved into the All blend purely by being registered here).
  // Re-registration is idempotent (keyed by provider id).
  useEffect(() => {
    registerProvider(sessions)
    registerProvider(knowledge)
    registerProvider(skills)
    registerProvider(prompts)
    registerProvider(pages)
    registerProvider(actions)
  }, [sessions, knowledge, skills, prompts, pages, actions])

  const [query, setQuery] = useState('')
  const [activeTab, setActiveTab] = useState(0)
  const resultsRef = useRef<Result[]>([])
  const tabCountRef = useRef(tabs.length)
  tabCountRef.current = tabs.length

  const inputRef = useRef<HTMLInputElement | null>(null)
  const activeProvider = tabs[activeTab] ?? tabs[0]

  /**
   * Central Enter dispatcher (Mesh-2151 §2, {@link OnEnter}). Switches on the
   * result's declarative {@link EnterAction} (`result.enter`) and routes to the
   * per-type branch; `withModifier` is `true` for ⌘/Ctrl+Enter (the
   * always-new-session / attach-as-context branch of the matrix).
   *
   * Migration-safe: providers that have not yet been ported to populate
   * `result.enter` fall through to the legacy `onActivate` / `onCmdActivate`
   * closures, so current behavior is unchanged. The typed branches start as
   * stubs (no-op + `console.warn`) and are implemented one per type by
   * tasks 23–27. ⌥/Alt+Enter (preview) is intentionally NOT handled here — it
   * stays a separate, non-closing path in the window-capture listener below.
   *
   * Always closes the palette after dispatching (Enter / ⌘Enter both close;
   * only preview keeps it open).
   */
  const dispatchEnter = useCallback(
    (result: Result, withModifier: boolean) => {
      const action = result.enter
      if (action) {
        switch (action.kind) {
          case 'open-session':
            // Sessions: Enter opens/switches, ⌘Enter opens in split (Grid).
            if (withModifier && result.onCmdActivate) {
              result.onCmdActivate()
            } else {
              result.onActivate()
            }
            break
          case 'insert-token': {
            // Skills / Prompts (Mesh-2151 §2 / task 24). Primary Enter is
            // context-aware: insert the token (`$<skill>` / `@<prompt>`, from
            // the result payload) into the active chat composer, or — when no
            // chat is active — open a new session seeded with it.
            // `enterInsertOrNewSession` encodes that branch off the live
            // `hasActiveChat` predicate. ⌘Enter is always "new seeded session".
            // The token is a plain string; allowlisted resolution happens
            // server-side on submit (BSC1, see paletteActions.ts).
            if (withModifier) {
              newSessionWithToken(action.token)
            } else {
              enterInsertOrNewSession(action.token)
            }
            break
          }
          case 'open-knowledge':
            // Knowledge (Mesh-2151 §2 / task 26). Primary Enter opens /
            // navigates to the entry; ⌘Enter attaches it as context to the
            // active chat. Both reuse the provider-bound closures
            // (`onActivate` → openEntry, `onCmdActivate` → attachAsContext);
            // `action.entryId`/`title` are the declarative, testable payload.
            // When the host supplies no attach callback (`onCmdActivate`
            // undefined), ⌘Enter degrades to opening the entry.
            if (withModifier && result.onCmdActivate) {
              result.onCmdActivate()
            } else {
              result.onActivate()
            }
            break
          case 'navigate':
            // Pages (Mesh-2151 §2 / task 27): navigate to the page route. Pages
            // are pure navigation targets — ⌘Enter takes NO distinct action, so
            // `withModifier` is intentionally ignored (⌘Enter == Enter). The
            // provider bound `navigate(action.route)` into `onActivate`; reuse
            // it as the execution path (the declarative `action.route` is the
            // testable payload), mirroring the `open-session` branch.
            result.onActivate()
            break
          case 'invoke':
            // Actions (Mesh-2151 §2 / task 27): run the free action callback.
            // Actions are pure command-invocations — ⌘Enter takes NO distinct
            // action, so `withModifier` is intentionally ignored
            // (⌘Enter == Enter). The callback rides on the declarative payload,
            // so invoke it directly.
            action.run()
            break
          default: {
            // Exhaustiveness guard — every EnterAction kind must have a branch.
            const _exhaustive: never = action
            console.warn('[CommandPalette] dispatchEnter: unhandled enter action', _exhaustive)
          }
        }
      } else if (withModifier && result.onCmdActivate) {
        // Legacy closure fallback (pre-`enter` providers): ⌘Enter.
        result.onCmdActivate()
      } else {
        // Legacy closure fallback (pre-`enter` providers): primary Enter.
        result.onActivate()
      }
      onClose()
    },
    [onClose, enterInsertOrNewSession, newSessionWithToken],
  )

  // Enter / ⌘Enter from the shared hook: look up the chosen row and dispatch,
  // threading the modifier flag through to {@link dispatchEnter}.
  // Search via React Query — handles caching, cancellation, dedup automatically.
  const { data: results = [], isLoading: loading } = useQuery({
    queryKey: ['palette', 'search', activeProvider.id, query],
    queryFn: () => Promise.resolve(activeProvider.search(query)),
    enabled: open,
    placeholderData: (prev) => prev ?? [],
    staleTime: 10_000,
  })

  // Keep resultsRef in sync for imperative reads (Enter handler).
  useEffect(() => {
    resultsRef.current = results
  }, [results])

  const onChoose = useCallback(
    (idx: number, withModifier: boolean) => {
      const r = resultsRef.current
      const item = r[idx >= r.length ? 0 : idx]
      if (!item) return
      dispatchEnter(item, withModifier)
    },
    [dispatchEnter],
  )

  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    wrap: true,
    onChoose,
    onClose,
  })

  // Reset selection to top when results change (new query or tab switch).
  useEffect(() => { setSelected(0) }, [results, setSelected])

  // Reset query + active tab each time the palette (re)opens, and focus input.
  useEffect(() => {
    if (!open) return
    setQuery('')
    setActiveTab(0)
    const id = requestAnimationFrame(() => inputRef.current?.focus())
    return () => cancelAnimationFrame(id)
  }, [open])

  // Window-capture listener for the two keys the shared hook doesn't own the
  // way the palette needs: Tab (cycle category) and ⌥/Alt+Enter (preview).
  // window-capture runs before the hook's document-capture listener, so
  // stopImmediatePropagation here keeps the hook from also acting on them.
  useEffect(() => {
    if (!open) return
    const onWinKey = (e: KeyboardEvent) => {
      if (e.key === 'Tab') {
        e.preventDefault()
        e.stopImmediatePropagation()
        const n = tabCountRef.current
        const dir = e.shiftKey ? -1 : 1
        setActiveTab((prev) => (prev + dir + n) % n)
      } else if (e.key === 'Enter' && e.altKey && !e.metaKey && !e.ctrlKey) {
        e.preventDefault()
        e.stopImmediatePropagation()
        const r = resultsRef.current
        const item = r[selectedRef.current] ?? r[0]
        item?.onAltActivate?.()
        // Preview deliberately leaves the palette open.
      }
    }
    window.addEventListener('keydown', onWinKey, true)
    return () => window.removeEventListener('keydown', onWinKey, true)
  }, [open, selectedRef])

  if (!open) return null

  const emptyState = loading ? (
    <div className="px-3 py-6 text-center text-[12px] text-muted">Searching…</div>
  ) : (
    <div className="px-3 py-6 text-center text-[12px] text-muted">
      {query.trim() ? 'No matches' : 'Type to search'}
    </div>
  )

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-sm animate-rise"
      role="dialog"
      aria-modal="true"
      aria-label="Search everywhere"
      onMouseDown={onClose}
    >
      <div
        className="mt-[12vh] w-full max-w-xl mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col max-h-[70vh]"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border">
          <Search size={16} className="shrink-0 text-muted lucide-inline" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search sessions, knowledge, skills, prompts…"
            aria-label="Search everywhere"
            className="flex-1 bg-transparent border-none outline-none text-[14px] text-text placeholder:text-muted"
          />
          <button
            type="button"
            className="text-muted cursor-pointer hover:text-text bg-transparent border-none"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Tab strip */}
        <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border" role="tablist">
          {tabs.map((t, i) => (
            <button
              type="button"
              key={t.id}
              role="tab"
              aria-selected={i === activeTab}
              onClick={() => setActiveTab(i)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium cursor-pointer transition-colors ${
                i === activeTab
                  ? 'bg-accent-subtle text-text'
                  : 'text-muted hover:bg-bg-hover hover:text-text'
              }`}
            >
              {t.icon}
              <span>{t.label}</span>
            </button>
          ))}
        </div>

        {/* Result list */}
        <div className="overflow-y-auto py-1" role="listbox">
          {/* Recents-on-empty-query: when nothing is typed yet the palette
              opens onto recents (the All aggregator returns recent sessions,
              and each per-category tab shows its full/recent list). Label the
              section so it reads as "recents", not a search result set. */}
          {query.trim() === '' && results.length > 0 && (
            <div
              role="presentation"
              className="px-4 pt-1 pb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted"
            >
              Recent
            </div>
          )}
          {results.length === 0
            ? emptyState
            : results.map((r, i) => (
                <div
                  role="option"
                  aria-selected={i === selected}
                  tabIndex={-1}
                  key={r.id}
                  ref={(el) => {
                    itemRefs.current[i] = el
                  }}
                  className={`w-full text-left px-4 py-2 flex items-center gap-3 cursor-pointer transition-colors ${
                    i === selected
                      ? 'bg-accent-subtle text-text'
                      : 'text-muted hover:bg-bg-hover hover:text-text'
                  }`}
                  title={r.subtitle || r.title}
                  onMouseEnter={() => setSelected(i)}
                  onMouseDown={(e) => {
                    e.preventDefault()
                    dispatchEnter(r, e.metaKey || e.ctrlKey)
                  }}
                >
                  <span className="shrink-0 flex items-center">{r.icon}</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] truncate">
                      <Highlighted text={r.title} indices={r.indices} />
                    </div>
                    {r.subtitle && (
                      <div className="text-[11px] text-muted truncate">{r.subtitle}</div>
                    )}
                  </div>
                </div>
              ))}
        </div>
      </div>
    </div>,
    document.body,
  )
}
