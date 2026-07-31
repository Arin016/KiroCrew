/**
 * The pseudolocale must be resolvable but not selectable.
 *
 * `en-XA` has to be a registered language: `resolveLanguage()` falls back on the primary
 * subtag, so an unregistered `en-XA` silently collapses to `en` and the pseudolocale
 * never activates. But a user of a shipped build must not be able to select a UI where
 * every string is accented and padded.
 *
 * That leaves exactly one failure mode — a botched environment check leaking it into
 * production — so this asserts the complement rather than trusting the check: in a
 * production build the picker offers every registered language *except* the dev-only
 * ones, and the count is asserted, not just the absence.
 *
 * Also guarded here: registering a pseudolocale must not perturb real-language
 * detection. Adding `en-XA` gave primary subtag `en` two candidates, which made
 * `matchConfident()` treat `en-GB` as ambiguous and stop resolving it to `en`. Browser
 * detection therefore reads `DETECTABLE_CODES`, and these tests pin that separation.
 */

import { describe, it, expect } from 'vitest'

import {
  SUPPORTED_LANGUAGES,
  SUPPORTED_CODES,
  DETECTABLE_CODES,
  PICKABLE_LANGUAGES,
  DEFAULT_LANGUAGE,
} from './languages'
import { CATALOGS } from './index'

const PSEUDO = 'en-XA'
const devOnly = SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code)

describe('pseudolocale registration', () => {
  it('is registered as a resolvable language', () => {
    // Without this, `resolveLanguage('en-XA')` returns 'en' by primary-subtag match and
    // the pseudolocale is silently inert.
    expect(SUPPORTED_CODES).toContain(PSEUDO)
  })

  it('has a catalog wired into the runtime', () => {
    expect(Object.keys(CATALOGS)).toContain(PSEUDO)
  })

  it('is the only dev-only language', () => {
    // A second one would mean someone reused the flag for something it was not designed
    // for; the picker and detection exclusions are written for a pseudolocale.
    expect(devOnly).toEqual([PSEUDO])
  })

  it('is NOT detectable from a browser tag', () => {
    // No browser sends `en-XA`, and including it makes every real `en-*` tag ambiguous.
    expect(DETECTABLE_CODES).not.toContain(PSEUDO)
    expect(DETECTABLE_CODES).toContain(DEFAULT_LANGUAGE)
  })

  it('leaves exactly the authored languages detectable', () => {
    expect(DETECTABLE_CODES.length).toBe(SUPPORTED_CODES.length - devOnly.length)
  })

  it('is hidden from the picker unless this is a dev build', () => {
    const codes = PICKABLE_LANGUAGES.map((l) => l.code)
    if (import.meta.env.DEV) {
      expect(codes).toContain(PSEUDO)
    } else {
      expect(codes).not.toContain(PSEUDO)
    }
    // Assert the complement, not just the absence: a broken environment check that
    // dropped every language would also satisfy `not.toContain`.
    expect(codes.length).toBe(
      import.meta.env.DEV ? SUPPORTED_CODES.length : SUPPORTED_CODES.length - devOnly.length,
    )
  })
})
