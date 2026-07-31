/**
 * Terminology gate.
 *
 * `glossary.json` exists because the same English term already has divergent Chinese
 * renderings across this catalog — `active` is 启用, 已启用 and 活跃 in different keys.
 * A single-language contributor cannot see that: they are editing one file out of ten.
 * Under an AI-generated, community-edited model with no TMS, this is the one defect class
 * that neither the generator nor the reviewer can catch, so it has to be CI.
 *
 * ## Two things it checks, and one it deliberately does not
 *
 * **Do-not-translate terms** are enforced: if the English value contains `KiroCrew` and
 * the translation does not, the product name was dropped. Matching is word-boundary, not
 * substring, and the list is proper nouns only — `PR`, `CR`, `API`, `CLI` and `URL` are
 * deliberately absent because they behave as common nouns and inflect. Russian declines
 * `PR` to `пул-реквеста`, which is correct.
 *
 * **Forbidden renderings** are enforced only for `ratified` entries. All 34 candidates are
 * currently unratified, which is the honest state: `Back` is 返回 in a dialog and 上一步
 * in a wizard, so forbidding the minority rendering would force a wrong translation.
 * Ratifying an entry means a reader decided it is one term rather than several.
 *
 * **Preferred renderings are not enforced.** A glossary that mandates one word for one
 * English string is the "textual identity is semantic identity" error i18next warns
 * about; the value of this file is the forbidden list and the DNT list.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES } from './languages'
import glossary from './glossary.json'

/**
 * Product names dropped from a translation, at the time this gate went in. Every one is
 * the same handful of English keys repeated across languages, which is the signature of a
 * systematic cause rather than nine independent slips — most likely the codemod's sentence
 * splitting, where the name sits at a fragment boundary.
 *
 * Baselined rather than fixed here: whether dropping a product name is acceptable is a
 * per-language judgement (`Tell Kiro about you` → `介绍一下你自己` reads better without
 * it), and that needs a reader. Ratchet DOWN as they are reviewed.
 */
const DNT_BASELINE = 36

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

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

const catalogs = Object.fromEntries(
  Object.entries(RUNTIME_CATALOGS)
    .filter(([code]) => !GENERATED.has(code))
    .map(([code, bundle]) => [code, flatten((bundle as { translation: unknown }).translation)]),
)
const en = catalogs[DEFAULT_LANGUAGE]

/** Word-boundary match that also refuses to fire inside a dotted identifier (`Node.js`). */
const boundary = (term: string) =>
  new RegExp(`(?<![\\w.])${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w.])`)

describe('glossary', () => {
  it('is well formed', () => {
    expect(Array.isArray(glossary.dnt)).toBe(true)
    expect(glossary.dnt.length).toBeGreaterThan(10)
    expect(Object.keys(glossary.terms).length).toBeGreaterThan(0)
  })

  it('every term entry names a real English value', () => {
    // A stale entry is worse than no entry: it looks like a rule and enforces nothing.
    const values = new Set(Object.values(en))
    const orphans = Object.keys(glossary.terms).filter((t) => !values.has(t))
    expect(orphans, 'glossary entries for English strings that no longer exist').toEqual([])
  })

  it('do-not-translate terms are not dropped in translation', () => {
    const dropped: string[] = []
    for (const [code, catalog] of Object.entries(catalogs)) {
      if (code === DEFAULT_LANGUAGE) continue
      for (const [key, value] of Object.entries(catalog)) {
        const source = en[key]
        if (source === undefined) continue
        for (const term of glossary.dnt) {
          const re = boundary(term)
          if (re.test(source) && !re.test(value)) dropped.push(`${code}:${key} [${term}]`)
        }
      }
    }
    expect(
      dropped.length,
      `${dropped.length} translations dropped a product name (baseline ${DNT_BASELINE}).\n`
        + `${dropped.slice(0, 8).map((d) => `  ${d}`).join('\n')}\n`
        + 'A product name should survive translation. If dropping it genuinely reads better '
        + 'in that language, review the entry and lower the baseline.',
    ).toBeLessThanOrEqual(DNT_BASELINE)
  })

  it('reports the exact DNT count so the baseline can be ratcheted', () => {
    const dropped: string[] = []
    for (const [code, catalog] of Object.entries(catalogs)) {
      if (code === DEFAULT_LANGUAGE) continue
      for (const [key, value] of Object.entries(catalog)) {
        const source = en[key]
        if (source === undefined) continue
        for (const term of glossary.dnt) {
          const re = boundary(term)
          if (re.test(source) && !re.test(value)) dropped.push(`${code}:${key}`)
        }
      }
    }
    expect(
      dropped.length,
      `only ${dropped.length} now, below the baseline of ${DNT_BASELINE} — lower `
        + 'DNT_BASELINE in this file to keep the improvement.',
    ).toBe(DNT_BASELINE)
  })

  it('no translation uses a forbidden rendering of a ratified term', () => {
    const violations: string[] = []
    for (const [english, entry] of Object.entries(glossary.terms)) {
      const spec = entry as {
        ratified?: boolean
        'zh-CN'?: { preferred?: string; forbidden?: string[] }
      }
      if (!spec.ratified) continue
      const forbidden = spec['zh-CN']?.forbidden ?? []
      if (forbidden.length === 0) continue
      const zh = catalogs['zh-CN'] ?? {}
      for (const [key, source] of Object.entries(en)) {
        if (source !== english) continue
        const rendered = zh[key]
        if (rendered && forbidden.includes(rendered)) {
          violations.push(`zh-CN:${key} uses ${JSON.stringify(rendered)}, expected ${JSON.stringify(spec['zh-CN']?.preferred)}`)
        }
      }
    }
    expect(violations, 'forbidden terminology in a ratified entry').toEqual([])
  })
})
