#!/usr/bin/env node
/**
 * Translation driver: renders the committed prompt per (locale, shard), validates
 * filled shards, and drives `i18n-shard.mjs join` across every shipped locale.
 *
 * `i18n-shard.mjs` is deliberately single-locale — `join <dir> <tag>` writes one
 * catalog per invocation. That is the right shape for the primitive and the wrong
 * shape for the job: Phase 1 adds ~1767 keys, and `catalogParity.test.ts` demands
 * every one of them in all 9 non-English catalogs in the SAME commit, so a
 * translation run is inherently a 9-way fan-out. Doing that by hand is nine
 * chances to skip a locale and discover it as a red `Frontend Tests`.
 *
 * This script does NOT call a model. It renders the prompt and checks the answer;
 * sending it is the caller's job (an agent, a contributor, whatever). That split is
 * deliberate — it keeps the pipeline reproducible and testable with no network, no
 * credentials, and no vendor pinned into the repo.
 *
 * Usage:
 *   node scripts/i18n-translate.mjs plan [pathPrefix]        # what needs translating
 *   node scripts/i18n-translate.mjs emit <baseDir> [--locales a,b]
 *   node scripts/i18n-translate.mjs verify <baseDir> --locale <tag>
 *   node scripts/i18n-translate.mjs merge <baseDir>          # ADD keys (Phase 1)
 *   node scripts/i18n-translate.mjs join-all <baseDir>       # rewrite whole catalogs
 *
 * Use `merge`, not `join-all`, when adding keys. `i18n-shard.mjs join` rewrites the
 * catalog from the shards, and the shards are keyed off the ENGLISH corpus — so any
 * form the locale has and English does not is silently dropped. Measured: a join
 * round-trip removes 108 lines from `ru.json` and 45 keys from each of es/fr/pt/it,
 * all of them `_few`/`_many` CLDR plural forms. `merge` is additive and cannot.
 *
 * Layout under <baseDir> (keep it OUTSIDE the worktree — a dirty tree blocks
 * worktree pruning, see website/AGENTS.md Rule 9):
 *   en/       shard-NN.json + shard-NN.context.json   <- `i18n-shard.mjs split` output
 *   prompts/  <locale>/shard-NN.prompt.md             <- `emit` writes these
 *   <locale>/ shard-NN.json                           <- filled translations
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src/i18n')

const PROMPT_FILE = path.join(SRC, 'TRANSLATION-PROMPT.md')
const GLOSSARY_FILE = path.join(SRC, 'glossary.json')
const CONTEXT_FILE = path.join(SRC, 'en.context.json')
const STYLE_DIR = path.join(SRC, 'style')
const LANGUAGES_FILE = path.join(SRC, 'languages.ts')
const BASELINE_FILE = path.join(SRC, 'untranslated-baseline.json')
const PLURAL_KEYS_FILE = path.join(SRC, 'pluralKeys.json')
const LOCALES_DIR = path.join(SRC, 'locales')

/**
 * Categories `check-i18n-strings.mjs` assigns that Phase 1 owns. The other three
 * — `template`, `object-prop`, `array` — need a change of shape rather than a
 * local edit, so they belong to Phase 6 and must not inflate a Phase 1 estimate.
 */
export const PHASE1_CATEGORIES = ['expression', 'prose', 'attribute', 'status-call']
export const PHASE6_CATEGORIES = ['template', 'object-prop', 'array']

/**
 * Parse the shipped locales out of `languages.ts` rather than duplicating them.
 * A second hardcoded list is a second thing to forget when language #11 ships;
 * `translateDriver.test.ts` asserts this parse against the real
 * `SUPPORTED_LANGUAGES` so a format change here fails loudly instead of silently
 * translating eight languages out of nine.
 */
export function parseLanguages(source) {
  const block = source.match(/SUPPORTED_LANGUAGES[^=]*=\s*\[([\s\S]*?)\n\]/)
  if (!block) throw new Error('could not locate SUPPORTED_LANGUAGES in languages.ts')
  const out = []
  for (const m of block[1].matchAll(/\{\s*code:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'([^}]*)\}/g)) {
    if (/devOnly:\s*true/.test(m[3])) continue
    out.push({ code: m[1], label: m[2] })
  }
  if (out.length === 0) throw new Error('parsed zero languages from languages.ts')
  return out
}

const ALL_LANGUAGES = parseLanguages(fs.readFileSync(LANGUAGES_FILE, 'utf-8'))
const DEFAULT_LANGUAGE = 'en'
const TARGET_LANGUAGES = ALL_LANGUAGES.filter(l => l.code !== DEFAULT_LANGUAGE)

// ---------------------------------------------------------------------------
// pure helpers (exported for the test suite)
// ---------------------------------------------------------------------------

export function flatten(obj, prefix = '') {
  const out = {}
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) Object.assign(out, flatten(v, key))
    else out[key] = v
  }
  return out
}

/** i18next interpolation, tags, and `$t()` nesting — the things that are code. */
export function placeholders(value) {
  return [
    ...String(value).matchAll(/\{\{[^}]*\}\}/g),
    ...String(value).matchAll(/<\/?[^>]+>/g),
    ...String(value).matchAll(/\$t\([^)]*\)/g),
  ].map(m => m[0]).sort()
}

/**
 * Every rule `verify` can decide mechanically. Ordered so the cheapest and most
 * certain failures report first.
 *
 * `dnt` is word-boundary, matching `glossary.test.ts` — a substring check would
 * flag any word that merely contains `Git`, and a term absent from the English
 * is not required to appear in the translation.
 */
export function checkValue({ key, en, tr, dnt = [], categories = null, pluralBases = null }) {
  const findings = []
  const push = (rule, detail) => findings.push({ key, rule, detail })

  if (typeof tr !== 'string') return [{ key, rule: 'not-a-string', detail: typeof tr }]
  if (tr.trim() === '') return [{ key, rule: 'empty', detail: 'value is empty or whitespace' }]

  const ph = { en: placeholders(en), tr: placeholders(tr) }
  if (ph.en.join('|') !== ph.tr.join('|')) {
    push('placeholder-parity', `en=[${ph.en.join(' ')}] tr=[${ph.tr.join(' ')}]`)
  }

  const nl = s => (String(s).match(/\n/g) ?? []).length
  if (nl(en) !== nl(tr)) push('newline-count', `en=${nl(en)} tr=${nl(tr)}`)

  for (const term of dnt) {
    const re = new RegExp(`(?<![\\p{L}\\p{N}])${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\p{L}\\p{N}])`, 'u')
    if (re.test(en) && !re.test(tr)) push('dnt-missing', `"${term}" is in the English but not the translation`)
  }

  // The next three rules are RELATIVE to the English, not absolute. Measured
  // against the shipped catalogs, absolute forms produced 142 findings on
  // already-approved translations — and every one was inherited: the English
  // itself carries 77 doubled spaces (JSX text extraction kept the source
  // indentation) and 64 unbalanced brackets (the D1 fragment keys, `'Findings ('`
  // + N + `')'`). Those are SOURCE defects that Phase 3 repairs by de-fragmenting
  // the key; reporting them against the translator is noise that would get this
  // whole check switched off. Fire only on a defect the translation introduces.
  if (tr !== tr.trim() && en === String(en).trim()) {
    push('edge-whitespace', 'leading or trailing whitespace the English does not have')
  }
  if (/ {2}/.test(tr) && !/ {2}/.test(String(en))) {
    push('doubled-space', 'two consecutive spaces the English does not have')
  }

  // Full-width LETTERS and DIGITS only. The whole U+FF01-FF5E block would also
  // catch full-width punctuation, which is correct CJK typography and is what the
  // style guides ask for — flagging `（` here would fight the rule it implements.
  // Absolute rather than relative: English never contains these.
  if (/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/.test(tr)) {
    push('fullwidth-latin', 'full-width Latin letter or digit')
  }

  // Bracket balance, pooled across widths, as a DELTA against the English. Two
  // separate calibrations are baked in here, both measured against the shipped
  // catalogs:
  //
  //  - DELTA, not balance: the D1 fragment keys (`'Findings ('` + N + `')'`) are
  //    deliberately unbalanced in the source, so demanding balance reported 64
  //    findings against already-approved translations.
  //  - POOLED across widths, not per width: zh-CN correctly rewrites `(` as `（`,
  //    which its own style guide asks for. Comparing per width called all 60 of
  //    those a defect.
  //
  // What remains a defect is MIXING the widths inside one value — `发现 (3）`, the
  // failure `qa.test.ts` exists for — which pooling would otherwise hide, so that
  // is checked separately below.
  const strip = s => String(s).replace(/\{\{[^}]*\}\}/g, '').replace(/<\/?[^>]+>/g, '')
  const delta = (s, opens, closes) => {
    const chars = [...strip(s)]
    return chars.filter(c => opens.includes(c)).length - chars.filter(c => closes.includes(c)).length
  }
  const FAMILIES = [
    { name: 'round', opens: '(（', closes: ')）' },
    { name: 'square', opens: '[【', closes: ']】' },
    { name: 'curly', opens: '{', closes: '}' },
  ]
  for (const { name, opens, closes } of FAMILIES) {
    const dEn = delta(en, opens, closes)
    const dTr = delta(tr, opens, closes)
    if (dEn !== dTr) {
      push('unbalanced-bracket', `${name} bracket balance ${dTr >= 0 ? '+' : ''}${dTr}, English is ${dEn >= 0 ? '+' : ''}${dEn}`)
    }
    // Consistent conversion is fine; using both widths of one family in a single
    // value is the mixed-width pair the QA suite rejects.
    const chars = [...strip(tr)]
    const ascii = chars.some(c => c === opens[0] || c === closes[0])
    const wide = opens.length > 1 && chars.some(c => c === opens[1] || c === closes[1])
    if (ascii && wide) push('mixed-width-bracket', `${name} brackets use both ASCII and full-width forms`)
  }

  // A key is a plural form only if its BASE is in `pluralKeys.json`. Shape alone is
  // not enough: the slug generator ends a key with the sentence's last word, so
  // `"…to add one"` becomes `..._add_one` and looked like an impossible `_one` form
  // in three approved zh-CN values. With no registry supplied, stay silent rather
  // than guess — a false blocker here costs more than a missed one.
  if (categories && pluralBases) {
    const m = key.match(/^(.*)_(zero|one|two|few|many|other)$/)
    if (m && pluralBases.includes(m[1]) && !categories.includes(m[2])) {
      push('impossible-plural', `_${m[2]} is not a CLDR category for this locale (${categories.join(', ')})`)
    }
  }

  return findings
}

/**
 * English passthrough is reported separately from `checkValue`'s findings because
 * it is not always wrong — a proper noun or a symbol legitimately survives
 * translation — but a shard where most values match English is a shard nobody
 * translated, and that is worth a hard failure.
 */
export function passthroughRatio(en, tr) {
  const keys = Object.keys(en)
  if (keys.length === 0) return 0
  return keys.filter(k => tr[k] === en[k]).length / keys.length
}

export function renderPrompt(template, slots) {
  let out = template
  for (const [name, value] of Object.entries(slots)) {
    out = out.split(`{{${name}}}`).join(value)
  }
  const leftover = out.match(/\{\{[A-Z_]+\}\}/g)
  if (leftover) throw new Error(`unfilled prompt slot(s): ${[...new Set(leftover)].join(', ')}`)
  return out
}

/** The prompt body between the two markers — the doc's prose is not part of it. */
export function extractPromptBody(doc) {
  const m = doc.match(/^## PROMPT BEGIN\s*$([\s\S]*?)^## PROMPT END\s*$/m)
  if (!m) throw new Error('TRANSLATION-PROMPT.md is missing its "## PROMPT BEGIN" / "## PROMPT END" markers')
  return m[1].trim()
}

// ---------------------------------------------------------------------------
// absent-tolerant inputs
// ---------------------------------------------------------------------------

/**
 * The glossary, style guides and context sidecar each land in their own PR, so a
 * translation run must not hard-fail because one has not merged yet. Same
 * contract `i18n-shard.mjs` uses for the sidecar: degrade, but say so loudly —
 * silently producing context-free, glossary-free output is the exact failure
 * these files exist to prevent.
 */
function readOptional(file, what) {
  if (fs.existsSync(file)) return fs.readFileSync(file, 'utf-8')
  console.warn(`warning: ${path.relative(ROOT, file)} is missing, so ${what}`)
  return null
}

function dntTerms() {
  const raw = readOptional(GLOSSARY_FILE, 'do-not-translate terms cannot be enforced')
  return raw ? (JSON.parse(raw).dnt ?? []) : []
}

function pluralCategories(code) {
  try {
    return new Intl.PluralRules(code).resolvedOptions().pluralCategories
  } catch {
    return ['other']
  }
}

// ---------------------------------------------------------------------------
// subcommands
// ---------------------------------------------------------------------------

function cmdPlan(prefix) {
  if (!fs.existsSync(BASELINE_FILE)) {
    console.error(
      `${path.relative(ROOT, BASELINE_FILE)} does not exist.\n`
      + 'It is generated by `node scripts/check-i18n-strings.mjs --update` and is the\n'
      + 'Phase 1 worklist. Without it there is nothing to plan against.',
    )
    process.exit(2)
  }
  const { files } = JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf-8'))
  const rows = Object.entries(files)
    .filter(([f]) => !prefix || f.startsWith(prefix))
    .map(([f, cats]) => ({
      file: f,
      phase1: PHASE1_CATEGORIES.reduce((n, c) => n + (cats[c] ?? 0), 0),
      phase6: PHASE6_CATEGORIES.reduce((n, c) => n + (cats[c] ?? 0), 0),
      cats,
    }))
    .filter(r => r.phase1 > 0)
    .sort((a, b) => b.phase1 - a.phase1)

  const p1 = rows.reduce((n, r) => n + r.phase1, 0)
  const p6 = rows.reduce((n, r) => n + r.phase6, 0)
  console.log(`${rows.length} file(s)${prefix ? ` under ${prefix}` : ''} with Phase 1 work`)
  console.log(`  Phase 1 (this phase):    ${p1}`)
  console.log(`  Phase 6 (deferred):      ${p6}`)
  console.log(`  x ${TARGET_LANGUAGES.length} locales:          ${p1 * TARGET_LANGUAGES.length} translated values\n`)
  for (const r of rows.slice(0, 40)) {
    const detail = Object.entries(r.cats)
      .filter(([c]) => c !== 'total')
      .map(([c, n]) => `${c}:${n}`)
      .join(' ')
    console.log(`  ${String(r.phase1).padStart(4)} p1  ${String(r.phase6).padStart(4)} p6  ${r.file}`)
    console.log(`             ${detail}`)
  }
  if (rows.length > 40) console.log(`  … and ${rows.length - 40} more`)
}

function cmdEmit(baseDir, locales) {
  const enDir = path.join(baseDir, 'en')
  if (!fs.existsSync(enDir)) {
    console.error(
      `${enDir} does not exist.\n`
      + `Run \`node scripts/i18n-shard.mjs split ${enDir}\` first — that is what produces\n`
      + 'the English shards and their translator-context sidecars.',
    )
    process.exit(2)
  }
  const template = extractPromptBody(fs.readFileSync(PROMPT_FILE, 'utf-8'))
  const dnt = dntTerms()
  const shards = fs.readdirSync(enDir)
    .filter(f => /^shard-\d+\.json$/.test(f))
    .sort()
  if (shards.length === 0) {
    console.error(`no shard-NN.json found in ${enDir}`)
    process.exit(2)
  }

  let written = 0
  for (const { code, label } of locales) {
    const style = readOptional(path.join(STYLE_DIR, `${code}.md`), `${code} has no style guide to follow`)
    const existing = fs.existsSync(path.join(LOCALES_DIR, `${code}.json`))
      ? flatten(JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, `${code}.json`), 'utf-8')))
      : {}
    const examples = Object.entries(existing)
      .filter(([, v]) => typeof v === 'string' && v.length > 2 && v.length < 60)
      .slice(0, 12)
    const outDir = path.join(baseDir, 'prompts', code)
    fs.mkdirSync(outDir, { recursive: true })

    for (const shard of shards) {
      const stem = shard.replace(/\.json$/, '')
      const ctxFile = path.join(enDir, `${stem}.context.json`)
      const ctx = fs.existsSync(ctxFile) ? fs.readFileSync(ctxFile, 'utf-8') : null
      const body = renderPrompt(template, {
        LOCALE: code,
        LANGUAGE_LABEL: label,
        PLURAL_CATEGORIES: pluralCategories(code).join(', '),
        STYLE_GUIDE: style ?? '_(no style guide has landed for this locale yet — apply the general rules above.)_',
        DNT_TERMS: dnt.length
          ? dnt.map(t => `- \`${t}\``).join('\n')
          : '_(no glossary has landed yet — keep product names and proper nouns in Latin script.)_',
        CONTEXT: ctx
          ? `\`\`\`json\n${ctx.trim()}\n\`\`\``
          : '_(no context entries apply to this shard.)_',
        SHARD_JSON: `\`\`\`json\n${fs.readFileSync(path.join(enDir, shard), 'utf-8').trim()}\n\`\`\``,
        EXAMPLES: examples.length
          ? `\`\`\`json\n${JSON.stringify(Object.fromEntries(examples), null, 2)}\n\`\`\``
          : '_(this locale has no catalog yet — you are establishing the terminology.)_',
      })
      fs.writeFileSync(path.join(outDir, `${stem}.prompt.md`), `${body}\n`)
      written += 1
    }
    console.log(`${code}: ${shards.length} prompt(s) -> ${path.relative(process.cwd(), outDir)}`)
  }
  console.log(`\n${written} prompt(s) for ${locales.length} locale(s). Answers go in <baseDir>/<locale>/shard-NN.json.`)
}

/**
 * Keys a locale is not expected to carry, because the English plural form has no
 * counterpart in this locale's CLDR categories. `en.json` holds `x_one` + `x_other`;
 * zh-CN has only `other`, so `x_one` is legitimately absent and demanding it would
 * report 45 phantom missing keys against the approved catalog.
 *
 * Mirrors how `catalogParity.test.ts` builds `EN_SINGULAR_KEYS` — the registry is
 * the source of truth for which keys are counted, so a plural-shaped key that is
 * NOT registered stays mandatory.
 */
export function expectedAbsent(enKeys, pluralBases, categories) {
  const bases = new Set(pluralBases)
  const cats = new Set(categories)
  return new Set(enKeys.filter(k => {
    const m = k.match(/^(.*)_(zero|one|two|few|many|other)$/)
    return m !== null && bases.has(m[1]) && !cats.has(m[2])
  }))
}

function cmdVerify(baseDir, code) {
  const enDir = path.join(baseDir, 'en')
  const trDir = path.join(baseDir, code)
  for (const [dir, hint] of [[enDir, 'run `i18n-shard.mjs split` first'], [trDir, 'no filled shards for this locale']]) {
    if (!fs.existsSync(dir)) {
      console.error(`${dir} does not exist — ${hint}.`)
      process.exit(2)
    }
  }
  const dnt = dntTerms()
  const categories = pluralCategories(code)
  const pluralBases = fs.existsSync(PLURAL_KEYS_FILE)
    ? JSON.parse(fs.readFileSync(PLURAL_KEYS_FILE, 'utf-8'))
    : []
  const read = d => Object.assign(
    {},
    ...fs.readdirSync(d)
      .filter(f => /^shard-\d+\.json$/.test(f))
      .sort()
      .map(f => JSON.parse(fs.readFileSync(path.join(d, f), 'utf-8'))),
  )
  const en = read(enDir)
  const tr = read(trDir)
  const absentOk = expectedAbsent(Object.keys(en), pluralBases, categories)

  const findings = []
  const missing = Object.keys(en).filter(k => !(k in tr) && !absentOk.has(k))
  const extra = Object.keys(tr).filter(k => !(k in en))
  for (const k of missing) findings.push({ key: k, rule: 'missing-key', detail: 'absent from the filled shards' })
  for (const k of extra) findings.push({ key: k, rule: 'unknown-key', detail: 'not in the English corpus' })
  for (const k of Object.keys(en)) {
    if (k in tr) findings.push(...checkValue({ key: k, en: en[k], tr: tr[k], dnt, categories, pluralBases }))
  }

  const comparable = Object.fromEntries(Object.entries(en).filter(([k]) => k in tr))
  const ratio = passthroughRatio(comparable, tr)
  const PASSTHROUGH_LIMIT = 0.5
  const ratioFailed = ratio > PASSTHROUGH_LIMIT

  console.log(
    `[${code}] ${Object.keys(en).length} key(s)`
    + `${absentOk.size ? `, ${absentOk.size} plural form(s) not applicable to this locale` : ''}`
    + `, ${findings.length} finding(s), ${(ratio * 100).toFixed(1)}% identical to English`,
  )
  if (findings.length) {
    const byRule = {}
    for (const f of findings) (byRule[f.rule] ??= []).push(f)
    for (const [rule, items] of Object.entries(byRule).sort((a, b) => b[1].length - a[1].length)) {
      console.error(`\n  ${rule} (${items.length})`)
      for (const it of items.slice(0, 8)) console.error(`    ${it.key}: ${it.detail}`)
      if (items.length > 8) console.error(`    … and ${items.length - 8} more`)
    }
  }
  if (ratioFailed) {
    console.error(
      `\n  passthrough: ${(ratio * 100).toFixed(1)}% of values are byte-identical to English, above the `
      + `${PASSTHROUGH_LIMIT * 100}% limit.\n  That is the shape of a shard nobody translated.`,
    )
  }
  if (findings.length || ratioFailed) process.exit(1)
  console.log(`OK: ${code} is ready to join.`)
}

/**
 * Additive merge of translated keys into an existing catalog.
 *
 * `i18n-shard.mjs join` writes the whole catalog from the shards, which is right
 * for a full re-translation and WRONG for Phase 1. Measured on the shipped
 * catalogs: a join round-trip drops 108 lines from `ru.json` and 45 keys from each
 * of es/fr/pt/it, because those are locale-specific CLDR plural forms (`_few`,
 * `_many`) that the English corpus does not contain and so no shard can carry. The
 * documented split/join workflow silently deletes them.
 *
 * Phase 1 ADDS keys. So merge instead: existing values win nothing and lose
 * nothing, new values land, and every form the locale already had survives.
 */
export function mergeCatalog(existing, additions) {
  const out = structuredClone(existing)
  for (const [key, value] of Object.entries(additions)) {
    const parts = key.split('.')
    const leaf = parts.pop()
    let node = out
    for (const p of parts) {
      if (p === '__proto__' || p === 'constructor' || p === 'prototype') {
        throw new Error(`Refusing to nest key '${key}': unsafe object-key segment.`)
      }
      if (typeof node[p] !== 'object' || node[p] === null || Array.isArray(node[p])) node[p] = {}
      node = node[p]
    }
    if (leaf === '__proto__' || leaf === 'constructor' || leaf === 'prototype') {
      throw new Error(`Refusing to nest key '${key}': unsafe object-key segment.`)
    }
    node[leaf] = value
  }
  return out
}

export function sortDeep(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return obj
  return Object.fromEntries(Object.keys(obj).sort().map(k => [k, sortDeep(obj[k])]))
}

function cmdMerge(baseDir, locales) {
  const results = []
  for (const { code } of locales) {
    const dir = path.join(baseDir, code)
    const target = path.join(LOCALES_DIR, `${code}.json`)
    if (!fs.existsSync(dir)) {
      results.push({ code, ok: false, why: 'no shard directory' })
      continue
    }
    if (!fs.existsSync(target)) {
      results.push({ code, ok: false, why: `${path.relative(ROOT, target)} does not exist` })
      continue
    }
    const additions = Object.assign(
      {},
      ...fs.readdirSync(dir)
        .filter(f => f.endsWith('.json') && !f.endsWith('.context.json'))
        .sort()
        .map(f => flatten(JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')))),
    )
    const empty = Object.entries(additions).filter(([, v]) => typeof v !== 'string' || !v.trim())
    if (empty.length) {
      // Fail closed, exactly as `join` does. A blank value merged into a catalog
      // renders as nothing at all, which is worse than the English fallback it
      // replaces — and `returnEmptyString: false` makes it invisible in review.
      results.push({ code, ok: false, why: `${empty.length} empty value(s), e.g. ${empty[0][0]}` })
      continue
    }
    const before = JSON.parse(fs.readFileSync(target, 'utf-8'))
    const beforeCount = Object.keys(flatten(before)).length
    const merged = sortDeep(mergeCatalog(before, additions))
    const afterCount = Object.keys(flatten(merged)).length
    fs.writeFileSync(target, `${JSON.stringify(merged, null, 2)}\n`)
    results.push({
      code,
      ok: true,
      why: `${Object.keys(additions).length} key(s) merged, ${beforeCount} -> ${afterCount}`,
    })
  }

  for (const r of results) console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.code.padEnd(6)} ${r.why}`)
  const failed = results.filter(r => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} locale(s) merged.`)
  if (failed.length) {
    console.error(
      `\n${failed.length} locale(s) did not merge: ${failed.map(r => r.code).join(', ')}.\n`
      + 'catalogParity.test.ts requires every locale, so this commit is not shippable yet.',
    )
    process.exit(1)
  }
}

function cmdJoinAll(baseDir, locales) {
  const results = []
  for (const { code } of locales) {
    const dir = path.join(baseDir, code)
    if (!fs.existsSync(dir)) {
      results.push({ code, ok: false, why: 'no shard directory' })
      continue
    }
    try {
      const out = execFileSync(
        process.execPath,
        [path.join(__dirname, 'i18n-shard.mjs'), 'join', dir, code],
        { cwd: ROOT, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] },
      )
      results.push({ code, ok: true, why: out.trim().split('\n').pop() ?? 'joined' })
    } catch (err) {
      const why = `${err.stderr ?? ''}${err.stdout ?? ''}`.trim().split('\n').filter(Boolean).pop()
      results.push({ code, ok: false, why: why ?? 'join failed' })
    }
  }

  for (const r of results) console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.code.padEnd(6)} ${r.why}`)
  const failed = results.filter(r => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} locale(s) joined.`)
  if (failed.length) {
    // Fail closed on a partial run. A catalog written for 7 of 9 locales still
    // fails `catalogParity.test.ts` for the other 2, and reporting that here is
    // cheaper than finding it as a red Frontend Tests job.
    console.error(
      `\n${failed.length} locale(s) did not join: ${failed.map(r => r.code).join(', ')}.\n`
      + 'catalogParity.test.ts requires every locale, so this commit is not shippable yet.',
    )
    process.exit(1)
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function resolveLocales(argv) {
  const arg = argv.find(a => a.startsWith('--locales='))
  if (!arg) return TARGET_LANGUAGES
  const want = arg.slice('--locales='.length).split(',').map(s => s.trim()).filter(Boolean)
  const known = new Map(TARGET_LANGUAGES.map(l => [l.code, l]))
  const unknown = want.filter(c => !known.has(c))
  if (unknown.length) {
    console.error(
      `unknown locale(s): ${unknown.join(', ')}\n`
      + `shipped targets: ${TARGET_LANGUAGES.map(l => l.code).join(', ')}`,
    )
    process.exit(2)
  }
  return want.map(c => known.get(c))
}

function main(argv) {
  const [cmd, ...rest] = argv
  const positional = rest.filter(a => !a.startsWith('-'))

  // An unrecognised flag must not fall through to a run that writes files with
  // default settings — the same footgun `i18n-codemod.mjs` closes.
  const KNOWN = ['--locales=', '--locale=']
  const unknown = rest.filter(a => a.startsWith('-') && !KNOWN.some(k => a.startsWith(k)))
  if (unknown.length) {
    console.error(`unknown flag(s): ${unknown.join(', ')}\nknown: ${KNOWN.join(' ')}`)
    process.exit(2)
  }

  switch (cmd) {
    case 'plan':
      return cmdPlan(positional[0])
    case 'emit':
      if (!positional[0]) return usage('emit needs a <baseDir>')
      return cmdEmit(positional[0], resolveLocales(rest))
    case 'verify': {
      const localeArg = rest.find(a => a.startsWith('--locale='))
      const code = localeArg?.slice('--locale='.length)
      if (!positional[0] || !code) return usage('verify needs a <baseDir> and --locale=<tag>')
      if (!TARGET_LANGUAGES.some(l => l.code === code)) {
        console.error(`unknown locale: ${code}\nshipped targets: ${TARGET_LANGUAGES.map(l => l.code).join(', ')}`)
        process.exit(2)
      }
      return cmdVerify(positional[0], code)
    }
    case 'merge':
      if (!positional[0]) return usage('merge needs a <baseDir>')
      return cmdMerge(positional[0], resolveLocales(rest))
    case 'join-all':
      if (!positional[0]) return usage('join-all needs a <baseDir>')
      return cmdJoinAll(positional[0], resolveLocales(rest))
    default:
      return usage(cmd ? `unknown command: ${cmd}` : 'no command given')
  }
}

function usage(problem) {
  console.error(
    `${problem}\n\n`
    + 'usage:\n'
    + '  node scripts/i18n-translate.mjs plan [pathPrefix]\n'
    + '  node scripts/i18n-translate.mjs emit <baseDir> [--locales=a,b]\n'
    + '  node scripts/i18n-translate.mjs verify <baseDir> --locale=<tag>\n'
    + '  node scripts/i18n-translate.mjs merge <baseDir> [--locales=a,b]     # additive, for Phase 1\n'
    + '  node scripts/i18n-translate.mjs join-all <baseDir> [--locales=a,b]  # whole-catalog rewrite\n',
  )
  process.exit(2)
}

// Only dispatch when run as a script, so the helpers above stay importable by the
// test suite without executing anything.
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main(process.argv.slice(2))
}
