/**
 * Unit tests for the dictation splice helpers.
 *
 * These pin the contract the two callers (ChatPage's live composer and
 * VoiceSessionProvider's sinkless draft write) both depend on, including the
 * edge that GPT 5.6 found in round 11: an EMPTY hypothesis must still consume a
 * captured text selection, because in that case nothing was ever spliced into the
 * composer and `current` still holds the selected range.
 */
import { describe, it, expect } from 'vitest'
import { spliceDictationInto, replaceDictationHypothesis } from './dictationSplice'

describe('spliceDictationInto', () => {
  it('replaces a selected range', () => {
    expect(spliceDictationInto('keep SELECTED tail', 'FINAL', { start: 5, end: 13 }).value)
      .toBe('keep FINAL tail')
  })

  it('appends when no caret is known', () => {
    expect(spliceDictationInto('notes', 'spoken', null).value).toBe('notes spoken')
  })

  it('leaves the base untouched for an empty transcript (never deletes a selection)', () => {
    expect(spliceDictationInto('keep SELECTED tail', '', { start: 5, end: 13 }).value)
      .toBe('keep SELECTED tail')
  })

  it('reports the offset just past the inserted text', () => {
    const r = spliceDictationInto('ab', 'XY', { start: 1, end: 1 })
    expect(r.value.slice(0, r.caret)).toBe('a XY')
  })
})

describe('replaceDictationHypothesis', () => {
  it('swaps the hypothesis run for the final', () => {
    const r = replaceDictationHypothesis('notes hello wor', 'notes', null, 'hello wor', 'hello world')
    expect(r?.value).toBe('notes hello world')
  })

  it('keeps text typed after the hypothesis', () => {
    const r = replaceDictationHypothesis(
      'notes hello wor and typed', 'notes', null, 'hello wor', 'hello world today',
    )
    expect(r?.value).toBe('notes hello world today and typed')
  })

  it('declines when the run is gone (user edited it)', () => {
    expect(replaceDictationHypothesis('rewritten entirely', 'notes', null, 'hello wor', 'x'))
      .toBeNull()
  })

  it('EMPTY hypothesis consumes the selection, matching normal dictation', () => {
    const base = 'keep SELECTED tail'
    const caret = { start: 5, end: 13 }
    const drained = replaceDictationHypothesis(base, base, caret, '', 'FINAL')
    // Round-11 regression: this used to yield 'keep FINALSELECTED tail' — the
    // selection survived AND collided with the final.
    expect(drained?.value).toBe('keep FINAL tail')
    expect(drained?.value).toBe(spliceDictationInto(base, 'FINAL', caret).value)
  })

  it('EMPTY hypothesis never declines — the final is the only copy of the words', () => {
    // Even with text the helper cannot verify, an empty hypothesis must land:
    // no partial ever reached the composer, so declining would lose real speech.
    expect(replaceDictationHypothesis('anything at all', 'other base', null, '', 'spoken words'))
      .not.toBeNull()
  })
})
