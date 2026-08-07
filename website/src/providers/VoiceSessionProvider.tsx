import { createContext, useCallback, useContext, useMemo, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useVoiceInput } from '../hooks/useVoiceInput'
import { createAudioSample } from '../hooks/mic'
import { useAppSelector } from '../store'
import { api } from '../api/client'
import { loadDrafts, saveDrafts, setDraft } from '../utils/chatDrafts'
import { replaceDictationHypothesis, type DictationCaret } from '../utils/dictationSplice'

/**
 * VoiceSessionProvider — owns the ONE `useVoiceInput` session ABOVE the router.
 *
 * Why it lives here and not in ChatPage: each top-level view is its own route
 * (`/chat`, `/schedule`, `/artifacts`, …), so navigating away UNMOUNTS ChatPage.
 * When the voice hook lived inside ChatPage, that unmount tore down the recorder
 * and orphaned any in-flight transcription — the user saw "transcribing"
 * silently cancel and the words vanish. Hoisting the session above `<Routes>`
 * means a route change no longer destroys it: the in-flight transcription and
 * its "transcribing" state survive, so when the user returns to /chat the
 * spinner + transcript are still there. An ACTIVE recording is a different
 * matter — its meter and stop control unmount with ChatPage, so ChatPage stops
 * a hot mic on unmount (it must not keep capturing off-route); that stop still
 * transcribes and the words land in the originating slot's draft.
 *
 * ChatPage still owns everything about the LIVE composer (caret-splice, frozen
 * snapshots, per-slot draft routing). It registers those as a "sink" via
 * `registerVoiceSink` while mounted. When NO sink is mounted (the user is on
 * Schedule/Artifacts/etc.), the final transcript falls back to the originating
 * slot's PERSISTED draft — see `deliverToDraft` — so it is never lost.
 */

/**
 * Where a DRAINED streaming final should be written, captured by ChatPage at the
 * moment it unmounts mid-dictation.
 *
 * Navigating away used to `cancel()` the stream, which skips the graceful drain —
 * `useStreamingStt.cancel()` marks the session cancelled so `ws.onclose` returns
 * without firing `onFinal`. Anything spoken after the last emitted partial (and
 * Transcribe's corrections to it) was therefore discarded. We now `stop()`
 * instead, which lets the backend flush its in-flight finals.
 *
 * That drain needs somewhere to land, and it must REPLACE the hypothesis rather
 * than append to it — the partial is already in the composer and the draft, so a
 * plain append would read back as "hello hello". `base` is ChatPage's FROZEN
 * pre-dictation snapshot and `caret` the offset the hypothesis was spliced at, so
 * re-splicing the final into that same base overwrites the hypothesis exactly.
 */
export interface PendingStreamFinal {
  /** Slot the recording belonged to; the final is only applied to this slot. */
  sessionId: string
  /** Pre-dictation composer text the live hypothesis was spliced into. */
  base: string
  /** Offset the hypothesis was spliced at (null = append at end). */
  caret: DictationCaret | null
  /**
   * The last streaming hypothesis, i.e. the text now sitting in the composer and
   * draft. Carried so the final can REPLACE exactly that run and leave anything
   * the user typed after it intact — see `replaceDictationHypothesis`.
   */
  hypothesis: string
}

export interface VoiceSink {
  /**
   * Deliver a final transcript to the live composer (splice / per-slot route).
   * `replaceFrom` is set only for a streaming final drained across an unmount:
   * splice into ITS base/caret rather than the live composer value, so the final
   * replaces the hypothesis instead of doubling it.
   */
  onText?: (text: string, sessionId: string | null, replaceFrom?: PendingStreamFinal | null) => void
  /** Live streaming hypothesis for the on-screen composer. */
  onPartial?: (text: string, sessionId: string | null) => void
  /** Semantic-endpointing verdict: auto-submit the composer. */
  onEndpoint?: () => void
}

type VoiceHook = ReturnType<typeof useVoiceInput>

export interface VoiceSessionContextValue extends VoiceHook {
  /** Install the live-composer sink; returns an unregister fn for unmount. */
  registerVoiceSink: (sink: VoiceSink) => () => void
  /**
   * Arm (or clear, with null) the landing site for a streaming final that will
   * drain AFTER the caller unmounts. Called by ChatPage's unmount cleanup right
   * before it stops a hot streaming mic. Consumed once, by the first matching
   * final; pass null when starting a new recording so a stale descriptor from an
   * abandoned session can never rebase a later transcript.
   */
  armStreamFinalReplace: (pending: PendingStreamFinal | null) => void
}

const VoiceSessionContext = createContext<VoiceSessionContextValue | null>(null)

/**
 * Write a transcript into the originating slot's persisted draft when no live
 * composer sink is mounted. The draft store is module-level + localStorage
 * backed, so it survives ChatPage unmount; the remounted ChatPage picks the text
 * up via `loadDrafts()`.
 *
 * Two modes:
 * - APPEND (`replaceFrom` omitted) — a batch capture that finished off-route.
 *   Batch emits no partial, so nothing of it is in the draft yet and appending is
 *   unambiguous.
 * - REPLACE (`replaceFrom` set) — a STREAMING final drained after nav-away. Its
 *   hypothesis is already in the draft, so we swap that run for the final and
 *   keep anything the user typed around it. If the run can no longer be found the
 *   draft is left UNCHANGED: it still holds the hypothesis, so declining costs an
 *   uncorrected transcript rather than deleted user text.
 */
function deliverToDraft(
  sessionId: string | null,
  text: string,
  replaceFrom?: PendingStreamFinal | null,
): void {
  if (!sessionId || !text) return
  const drafts = loadDrafts()
  let next: string
  if (replaceFrom) {
    const swapped = replaceDictationHypothesis(
      drafts[sessionId] ?? '', replaceFrom.base, replaceFrom.caret, replaceFrom.hypothesis, text,
    )
    if (swapped === null) return
    next = swapped.value
  } else {
    const base = drafts[sessionId] ?? ''
    next = base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text
  }
  setDraft(drafts, sessionId, next)
  saveDrafts(drafts)
}

export function VoiceSessionProvider({ children }: { children: ReactNode }) {
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const { data: sttCfg } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean }>,
  })
  const streaming = !!sttCfg?.streaming

  // The live composer sink installed by whichever ChatPage is currently mounted.
  // Null when no chat surface is on screen. A ref (not state) so the hook's
  // stable callbacks always read the CURRENT sink without re-creating the hook.
  const sinkRef = useRef<VoiceSink | null>(null)
  const registerVoiceSink = useCallback((sink: VoiceSink) => {
    sinkRef.current = sink
    return () => { if (sinkRef.current === sink) sinkRef.current = null }
  }, [])

  // Landing site for a streaming final that drains after ChatPage unmounts (see
  // PendingStreamFinal). A ref, not state: it is armed from inside an unmount
  // cleanup, where a setState would be a no-op update on an unmounting tree.
  const pendingStreamFinalRef = useRef<PendingStreamFinal | null>(null)
  const armStreamFinalReplace = useCallback((pending: PendingStreamFinal | null) => {
    pendingStreamFinalRef.current = pending
  }, [])

  const onText = useCallback((text: string, sessionId: string | null) => {
    // Consume an armed drain descriptor, but ONLY for the slot it was armed for —
    // a final belonging to another slot must not be rebased onto this slot's
    // snapshot. Cleared on consume so it can never apply twice.
    const pending = pendingStreamFinalRef.current
    const replaceFrom = pending && (sessionId === null || pending.sessionId === sessionId) ? pending : null
    if (replaceFrom) pendingStreamFinalRef.current = null
    const sink = sinkRef.current
    // A sink is mounted again (the user navigated BACK before the drain landed).
    // Hand the descriptor over so the composer replaces the hypothesis it
    // re-hydrated from the draft instead of appending the final after it.
    if (sink?.onText) { sink.onText(text, sessionId, replaceFrom); return }
    // No live composer (chat route unmounted). Persist EVERY sinkless final to
    // the originating slot's draft so the words survive the navigation,
    // regardless of the current STT mode. Keying the drop on the live streaming
    // flag would silently discard an in-flight BATCH result if the user flipped
    // streaming on mid-transcription.
    deliverToDraft(sessionId ?? replaceFrom?.sessionId ?? null, text, replaceFrom)
  }, [])
  const onPartial = useCallback((text: string, sessionId: string | null) => {
    sinkRef.current?.onPartial?.(text, sessionId)
  }, [])
  const onEndpoint = useCallback(() => {
    sinkRef.current?.onEndpoint?.()
  }, [])

  const voice = useVoiceInput(onText, { streaming, sessionId: activeSlot, onPartial, onEndpoint })

  const value = useMemo<VoiceSessionContextValue>(
    () => ({ ...voice, registerVoiceSink, armStreamFinalReplace }),
    [voice, registerVoiceSink, armStreamFinalReplace],
  )
  return <VoiceSessionContext.Provider value={value}>{children}</VoiceSessionContext.Provider>
}

/**
 * Inert "voice unavailable" session used as the fallback when `useVoiceSession`
 * is read outside a provider. Degrading gracefully (mic controls simply do
 * nothing) is preferable to throwing, which would white-screen the whole view.
 * In the real app main.tsx always mounts <VoiceSessionProvider> above <Routes>,
 * so this is only reached in isolation — e.g. a unit test that renders a chat
 * surface without wrapping it in the provider.
 */
const INERT_VOICE_SESSION: VoiceSessionContextValue = {
  recording: false,
  transcribing: false,
  sessionOwner: null,
  streamEnabled: false,
  error: null,
  level: 0,
  deviceLabel: '',
  partial: '',
  deviceSwitchIsLive: false,
  sampleRef: { current: createAudioSample() },
  toggle: () => {},
  cancel: () => {},
  prewarm: () => {},
  clearError: () => {},
  switchDevice: async () => {},
  registerVoiceSink: () => () => {},
  armStreamFinalReplace: () => {},
}

/** Consume the hoisted voice session; inert fallback (see above) outside a provider. */
export function useVoiceSession(): VoiceSessionContextValue {
  return useContext(VoiceSessionContext) ?? INERT_VOICE_SESSION
}
