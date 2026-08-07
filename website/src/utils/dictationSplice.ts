/**
 * Caret-aware dictation splice — shared by the live composer (ChatPage) and the
 * hoisted voice session (VoiceSessionProvider).
 *
 * It lives here rather than inside ChatPage because a STREAMING final can now be
 * drained AFTER ChatPage unmounts (the user navigated away mid-dictation). The
 * provider has to place that final at the same offset the live hypothesis
 * occupied, which means both sides must run the identical algorithm — a second
 * implementation would drift and mis-place the text.
 *
 * The replace-not-append property both callers depend on: every streaming
 * partial is written as `spliceDictation(base, partial)` against a FROZEN
 * pre-dictation snapshot, so applying `spliceDictation(base, final)` to that same
 * snapshot OVERWRITES the hypothesis instead of appending after it.
 */

export interface DictationCaret {
  start: number
  end: number
}

export interface DictationSplice {
  /** The rebuilt composer/draft value. */
  value: string
  /** Caret offset to restore once the value lands. */
  caret: number
}

/**
 * Insert `text` into `base` at `caret`, returning the new value and the caret
 * offset to restore. Appends when `caret` is null (e.g. the composer was never
 * focused, so no offset was ever recorded).
 */
export function spliceDictationInto(
  base: string,
  text: string,
  caret: DictationCaret | null,
): DictationSplice {
  // An empty transcript (e.g. a silent streaming partial) must NOT mutate the
  // draft: splicing "" across a selection would delete the selected range.
  // Leave the base untouched and collapse the caret to the insertion point.
  if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
  if (!caret) {
    const value = base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text
    return { value, caret: value.length }
  }
  const start = Math.min(caret.start, base.length)
  const end = Math.min(caret.end, base.length)
  const before = base.slice(0, start)
  const after = base.slice(end)
  // Leading space only when joining onto a non-space char, so mid-sentence
  // dictation doesn't glue onto the preceding word.
  // Leading/trailing space uses whitespace-class checks (not only ' ') so a
  // caret beside a newline or tab doesn't get an unwanted literal space.
  const lead = before && !/\s$/.test(before) && !/^\s/.test(text) ? ' ' : ''
  const trail = after && !/^\s/.test(after) && !/\s$/.test(text) ? ' ' : ''
  const insert = lead + text
  return { value: before + insert + trail + after, caret: before.length + insert.length }
}

/**
 * Swap a streaming HYPOTHESIS for its corrected final inside `current`, keeping
 * whatever the user has typed on either side of it.
 *
 * Splicing the final straight into the frozen `base` (the obvious move) silently
 * DELETES anything typed after the last partial: the base predates that typing.
 * The window is narrow but real — finish speaking, type a suffix, navigate away
 * while the socket drains — and it is the same hazard `cancelVoice` already
 * guards when it unwinds a dictated region.
 *
 * So this locates the region instead of assuming it. `spliceDictationInto`
 * returns the offset just past the text it inserted, which is exactly the end of
 * the dictated region, so the hypothesis run is `value.slice(0, caret)`. If
 * `current` still starts with that run, everything after it is the user's to keep
 * and only the run is rewritten.
 *
 * Returns `null` when the run cannot be found — the user replaced or edited it.
 * Callers must then leave the text ALONE: a fuzzy match here would eat
 * user-authored words, and the hypothesis is already in the draft, so the cost of
 * declining is a transcript that stays uncorrected rather than one that is lost.
 */
export function replaceDictationHypothesis(
  current: string,
  base: string,
  caret: DictationCaret | null,
  hypothesis: string,
  final: string,
): DictationSplice | null {
  // No hypothesis ever reached the composer — the user navigated away before the
  // first partial. There is therefore no run to swap, and `current` still holds
  // whatever the caret had SELECTED, so the run-replacement below would re-append
  // that selection after the final ("keep FINALSELECTED tail") instead of
  // consuming it. Splice the final in normally, which drops the selected range
  // exactly as an on-screen dictation would.
  //
  // Note this path must never decline: with no partial, the drained final is the
  // ONLY copy of the user's words, so refusing would lose real speech rather than
  // leaving a merely-uncorrected transcript.
  if (!hypothesis) return spliceDictationInto(current, final, caret)
  const withHypothesis = spliceDictationInto(base, hypothesis, caret)
  const run = withHypothesis.value.slice(0, withHypothesis.caret)
  if (!current.startsWith(run)) return null
  const withFinal = spliceDictationInto(base, final, caret)
  return {
    value: withFinal.value.slice(0, withFinal.caret) + current.slice(run.length),
    // End of the FINAL's run, not end of string — the caret must not jump past a
    // suffix the user typed.
    caret: withFinal.caret,
  }
}
