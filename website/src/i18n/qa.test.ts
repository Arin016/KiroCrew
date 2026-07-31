/**
 * Catalog QA guards — the checks a translation management system would run, for a
 * project that keeps its catalogs in git and has no TMS.
 *
 * `catalogParity.test.ts` proves the catalogs are structurally *aligned* with each
 * other: same keys, same placeholders, right plural categories. It says nothing
 * about whether an individual value is well formed. This file covers that half.
 *
 * ## Why these specific checks
 *
 * They are the intersection of what every major TMS ships as a stock check
 * (Lokalise, Weblate, Crowdin, POEditor all include bracket balance, edge
 * whitespace and doubled spaces) and what a JSON catalog can decide on its own.
 * They matter more here than in a TMS-backed project, not less: translations are
 * machine-generated and then edited piecemeal by contributors who cannot read the
 * other nine languages, so CI is the only reviewer these strings get.
 *
 * ## Why an allowlist rather than a threshold
 *
 * The existing violations are a codemod artifact, not a translation problem — the
 * extractor split sentences at JSX boundaries, so `'Skills ('` and `')'` became
 * separate keys. Fixing those is a large, separate piece of work. Until then this
 * suite is a **ratchet**: it fails on anything new and ignores the frozen set. The
 * allowlist is also the worklist for that later cleanup.
 *
 * The allowlist carries a **staleness guard** — an entry that no longer matches a
 * real violation fails the suite. Without it the file silently accumulates dead
 * exemptions and stops meaning anything.
 *
 * ## Regenerating
 *
 *     I18N_QA_UPDATE_ALLOWLIST=1 npx vitest run src/i18n/qa.test.ts
 *
 * The check logic lives here and nowhere else, so the generator cannot drift from
 * the assertions the way a separate script would.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE } from './languages'

const ALLOWLIST_PATH = join(__dirname, 'qa-allowlist.json')
const UPDATING = process.env.I18N_QA_UPDATE_ALLOWLIST === '1'

/** Catalogs exactly as the runtime composes them, including English's two-file merge. */
const CATALOGS: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS).map(([code, bundle]) => [
    code,
    flatten((bundle as { translation: unknown }).translation),
  ]),
)

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

/**
 * Interpolation placeholders are removed before any punctuation check. `{{count}}`
 * contains braces that would otherwise register as an unbalanced pair.
 */
const stripInterpolation = (v: string) => v.replace(/\{\{[^}]*\}\}/g, '')

const DELIMITER_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ['(', ')'],
  ['[', ']'],
  ['（', '）'],
  ['【', '】'],
  ['「', '」'],
]

/**
 * Values that are a single connector or morpheme. These cannot be translated in
 * isolation — `'s'` is an English plural suffix and `'repl'` is the stem of
 * "replies", so every language ships them verbatim and the UI renders English.
 */
const CONNECTORS = new Set([
  'and', 'or', 'of', 'to', 'in', 'on', 'for', 'with', 'by', 'at', 'from',
  'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
  's', 'es', 'y', 'ies', 'repl',
])

/** Fullwidth digits and Latin letters. Fullwidth *punctuation* is correct in CJK; these are not. */
const FULLWIDTH_ALPHANUMERIC = /[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/

type Check = { id: string; describe: string; violates: (value: string) => boolean }

const CHECKS: Check[] = [
  {
    id: 'unbalanced-delimiter',
    describe: 'brackets and parentheses must be balanced within a single value',
    violates: (v) => {
      const t = stripInterpolation(v)
      return DELIMITER_PAIRS.some(([open, close]) => count(t, open) !== count(t, close))
    },
  },
  {
    id: 'odd-quote-count',
    describe: 'quotation marks must pair within a single value',
    violates: (v) => {
      const t = stripInterpolation(v)
      return (count(t, '“') + count(t, '”')) % 2 === 1 || count(t, '"') % 2 === 1
    },
  },
  {
    id: 'edge-whitespace',
    describe: 'no leading or trailing space or tab',
    // U+00A0 is excluded deliberately: a non-breaking space is a glyph the copy
    // asked for, not accidental padding.
    violates: (v) => v !== v.replace(/^[ \t\n\r]+/, '').replace(/[ \t\n\r]+$/, ''),
  },
  {
    id: 'doubled-space',
    describe: 'no run of two or more spaces',
    // A whitespace run containing a newline is indentation carried over from a
    // multi-line JSX literal; it collapses to one space when rendered and is not
    // a defect. Only newline-free runs are accidental.
    violates: (v) =>
      [...v.matchAll(/[ \t\n\r]{2,}/g)].some((m) => !m[0].includes('\n') && !m[0].includes('\r')),
  },
  {
    id: 'bare-connector',
    describe: 'a value must not be a lone connector word or morpheme',
    violates: (v) => CONNECTORS.has(v.trim().toLowerCase().replace(/[.,;:!?]+$/, '')),
  },
  {
    id: 'fullwidth-alphanumeric',
    describe: 'CJK catalogs must not store fullwidth Latin letters or digits',
    // W3C CLReq: "when storing text, avoid the fullwidth alphabetic and numeric
    // characters of that block; leave it to the layout engine."
    violates: (v) => FULLWIDTH_ALPHANUMERIC.test(v),
  },
]

const count = (haystack: string, needle: string) => haystack.split(needle).length - 1

/** `lang:key` — the unit an allowlist entry addresses. */
const site = (lang: string, key: string) => `${lang}:${key}`

function findViolations(check: Check): string[] {
  const out: string[] = []
  for (const [lang, catalog] of Object.entries(CATALOGS)) {
    for (const [key, value] of Object.entries(catalog)) {
      if (check.violates(value)) out.push(site(lang, key))
    }
  }
  return out.sort()
}

const live: Record<string, string[]> = Object.fromEntries(
  CHECKS.map((c) => [c.id, findViolations(c)]),
)

if (UPDATING) {
  writeFileSync(
    ALLOWLIST_PATH,
    `${JSON.stringify({ _generated: 'I18N_QA_UPDATE_ALLOWLIST=1 npx vitest run src/i18n/qa.test.ts', ...live }, null, 2)}\n`,
  )
}

const allowlist: Record<string, string[]> = JSON.parse(readFileSync(ALLOWLIST_PATH, 'utf-8'))

describe('catalog QA', () => {
  it.each(CHECKS.map((c) => [c.id, c] as const))('%s — no NEW violations', (id, check) => {
    const allowed = new Set(allowlist[id] ?? [])
    const added = live[id].filter((s) => !allowed.has(s))
    expect(
      added,
      `${check.describe}\n\n${added.length} new violation(s). Fix them, or if they are ` +
        `deliberate, regenerate the allowlist with I18N_QA_UPDATE_ALLOWLIST=1.`,
    ).toEqual([])
  })

  it.each(CHECKS.map((c) => [c.id, c] as const))('%s — allowlist has no stale entries', (id) => {
    const found = new Set(live[id])
    const stale = (allowlist[id] ?? []).filter((s) => !found.has(s))
    expect(
      stale,
      `These allowlist entries no longer match a real violation — the strings were ` +
        `fixed or the keys renamed. Regenerate the allowlist so it keeps meaning something.`,
    ).toEqual([])
  })

  it('every check is represented in the allowlist file', () => {
    // Guards against a check being added here and silently never gated because
    // the allowlist file has no key for it.
    const missing = CHECKS.map((c) => c.id).filter((id) => !(id in allowlist))
    expect(missing, 'regenerate the allowlist after adding a check').toEqual([])
  })

  it('English is the reference catalog and is present', () => {
    expect(Object.keys(CATALOGS)).toContain(DEFAULT_LANGUAGE)
    expect(Object.keys(CATALOGS[DEFAULT_LANGUAGE]).length).toBeGreaterThan(3000)
  })
})
