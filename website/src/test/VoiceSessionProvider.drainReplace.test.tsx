/**
 * Regression tests for the STREAMING drain-and-replace path
 * (GPT 5.6 round-8 blocking finding on fix/voice-session-above-router).
 *
 * BUG: ChatPage's unmount cleanup stopped a hot STREAMING mic with `cancel()`.
 * `useStreamingStt.cancel()` is the Esc/discard path — it marks the session
 * cancelled so `ws.onclose` returns WITHOUT firing `onFinal`. Navigating away
 * mid-dictation therefore threw away everything spoken after the last emitted
 * partial, plus Transcribe's corrections to that partial.
 *
 * FIX: unmount now takes the graceful `stop()` (which leaves the socket open so
 * the backend flushes its in-flight finals) and ARMS a `PendingStreamFinal`
 * descriptor telling the provider where the drained final should land.
 *
 * The subtle half is that the final must REPLACE the hypothesis, not append to
 * it: every streaming partial was already spliced into the composer AND
 * persisted to the draft, so a plain append reads back as "hello hello". The
 * descriptor carries the FROZEN pre-dictation base + caret, so re-splicing the
 * final into that base overwrites the hypothesis exactly. These tests pin that
 * replace-not-append property — the thing that made `cancel()` look correct in
 * the first place.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, act } from '@testing-library/react'
import { useEffect, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  VoiceSessionProvider,
  useVoiceSession,
  type VoiceSink,
  type PendingStreamFinal,
} from '../providers/VoiceSessionProvider'

const captured = vi.hoisted(() => ({
  onText: undefined as ((text: string, sessionId: string | null) => void) | undefined,
}))
const draftStore = vi.hoisted(() => ({} as Record<string, string>))

vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: (text: string, sessionId: string | null) => void) => {
    captured.onText = onText
    return {
      recording: false, transcribing: false, sessionOwner: null, streamEnabled: true,
      error: null, level: 0, deviceLabel: '', partial: '', deviceSwitchIsLive: false,
      sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
      toggle: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(), clearError: vi.fn(), switchDevice: vi.fn(),
    }
  },
  voiceInputSupported: true,
}))
vi.mock('../hooks/mic', () => ({ createAudioSample: () => ({ level: 0, centroid: 0.5, onset: 0 }) }))
vi.mock('../store', () => ({ useAppSelector: (sel: (s: unknown) => unknown) => sel({ chat: { activeSlot: 'slot-active' } }) }))
vi.mock('../api/client', () => ({ api: { sttConfig: vi.fn().mockResolvedValue({ streaming: true }) } }))
vi.mock('../utils/chatDrafts', () => ({
  loadDrafts: () => ({ ...draftStore }),
  setDraft: (d: Record<string, string>, id: string, text: string) => { d[id] = text },
  saveDrafts: (d: Record<string, string>) => {
    for (const k of Object.keys(draftStore)) delete draftStore[k]
    Object.assign(draftStore, d)
  },
}))

/** Handle onto the provider's arm fn, as ChatPage's unmount cleanup would use it. */
const armRef = { current: null as ((p: PendingStreamFinal | null) => void) | null }

function Arm() {
  const { armStreamFinalReplace } = useVoiceSession()
  useEffect(() => { armRef.current = armStreamFinalReplace }, [armStreamFinalReplace])
  return null
}

function renderProvider(children: ReactNode = null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <VoiceSessionProvider><Arm />{children}</VoiceSessionProvider>
    </QueryClientProvider>,
  )
}

describe('VoiceSessionProvider — drained streaming final replaces its hypothesis', () => {
  beforeEach(() => {
    for (const k of Object.keys(draftStore)) delete draftStore[k]
    captured.onText = undefined
    armRef.current = null
  })

  it('REPLACES the persisted hypothesis instead of appending after it', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    // The draft already holds `base + hypothesis` — onPartial spliced it in and
    // the persist/unmount flush wrote it out before ChatPage went away.
    draftStore['slot-7'] = 'notes so far hello wor'
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'notes so far', caret: null, hypothesis: 'hello wor' }))
    // The drain delivers Transcribe's corrected, COMPLETE utterance.
    act(() => captured.onText!('hello world today', 'slot-7'))
    expect(draftStore['slot-7']).toBe('notes so far hello world today')
    // The bug this pins: appending onto the draft would double the hypothesis.
    expect(draftStore['slot-7']).not.toContain('hello wor hello')
  })

  it('lands the final at the hypothesis caret, not at the end', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    // User dictated mid-sentence: caret sat right after "alpha ".
    draftStore['slot-7'] = 'alpha spoke bravo'
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'alpha bravo', caret: { start: 6, end: 6 }, hypothesis: 'spoke' }))
    act(() => captured.onText!('spoken words', 'slot-7'))
    expect(draftStore['slot-7']).toBe('alpha spoken words bravo')
  })

  it('hands the descriptor to a REMOUNTED sink so the composer can replace too', async () => {
    const received: Array<[string, string | null, PendingStreamFinal | null | undefined]> = []
    function Sink() {
      const { registerVoiceSink } = useVoiceSession()
      useEffect(() => registerVoiceSink({
        onText: (t, s, r) => { received.push([t, s, r]) },
      } as VoiceSink), [registerVoiceSink])
      return null
    }
    renderProvider(<Sink />)
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'kept', caret: null, hypothesis: 'hyp' }))
    act(() => captured.onText!('drained final', 'slot-7'))
    expect(received).toHaveLength(1)
    expect(received[0][0]).toBe('drained final')
    // Without the 3rd arg the remounted composer would splice onto the rehydrated
    // draft (which still holds the hypothesis) and double the text.
    expect(received[0][2]).toEqual({ sessionId: 'slot-7', base: 'kept', caret: null, hypothesis: 'hyp' })
  })

  it('consumes the descriptor once — a later final appends normally', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    // The base is by definition text that IS already in the composer/draft, so a
    // realistic fixture seeds it (an empty draft with a non-empty base cannot
    // happen, and the replace verification correctly refuses it).
    draftStore['slot-7'] = 'base'
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'base', caret: null, hypothesis: '' }))
    act(() => captured.onText!('first', 'slot-7'))
    expect(draftStore['slot-7']).toBe('base first')
    // Second final must NOT rebase onto `base` again (that would drop "first").
    act(() => captured.onText!('second', 'slot-7'))
    expect(draftStore['slot-7']).toBe('base first second')
  })

  it('PRESERVES text typed after the last partial (round-10 finding)', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    // Spoke, then typed a suffix, THEN navigated away.
    draftStore['slot-7'] = 'notes hello wor and typed after'
    act(() => armRef.current!({
      sessionId: 'slot-7', base: 'notes', caret: null, hypothesis: 'hello wor',
    }))
    act(() => captured.onText!('hello world today', 'slot-7'))
    // The hypothesis run is upgraded AND the typed suffix survives. Splicing the
    // final straight into the frozen base would have deleted ' and typed after'.
    expect(draftStore['slot-7']).toBe('notes hello world today and typed after')
  })

  it('DECLINES when the hypothesis run is no longer present (user edited it)', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    // The user rewrote the dictated region — the run cannot be located.
    draftStore['slot-7'] = 'completely different text'
    act(() => armRef.current!({
      sessionId: 'slot-7', base: 'notes', caret: null, hypothesis: 'hello wor',
    }))
    act(() => captured.onText!('hello world today', 'slot-7'))
    // Left untouched rather than clobbering user-authored words.
    expect(draftStore['slot-7']).toBe('completely different text')
  })

  it('ignores a descriptor armed for a DIFFERENT slot', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    draftStore['slot-9'] = 'nine draft'
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'seven base', caret: null, hypothesis: 'x' }))
    act(() => captured.onText!('nine words', 'slot-9'))
    // slot-9 appends to its OWN draft; slot-7's snapshot must not leak into it.
    expect(draftStore['slot-9']).toBe('nine draft nine words')
    expect(draftStore['slot-9']).not.toContain('seven base')
  })

  it('clears with null so an abandoned drain cannot rebase a later transcript', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    draftStore['slot-7'] = 'typed since'
    act(() => armRef.current!({ sessionId: 'slot-7', base: 'stale base', caret: null, hypothesis: 'x' }))
    act(() => armRef.current!(null))
    act(() => captured.onText!('new session', 'slot-7'))
    expect(draftStore['slot-7']).toBe('typed since new session')
  })
})
