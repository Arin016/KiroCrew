# Simplified Chinese style guide

Normative rules for `src/i18n/locales/zh-CN.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

The authority here is **W3C CLReq** (*Requirements for Chinese Text Layout*), which is
grounded in **GB/T 15834—2011**. It is cited rather than a vendor style guide because it
is openly licensed, versioned, and distinguishes rules by **region** rather than by
Simplified/Traditional — which matters, since zh-CN and zh-TW disagree about quotation
marks while sharing most other conventions.

- CLReq — <https://www.w3.org/TR/clreq/>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## 1. Punctuation

Chinese-style (full-width) punctuation throughout, because the surrounding text is
Chinese. CLReq: *"Chinese style punctuation… should be used in principle since the main
text is Chinese."*

| use | not | note |
|---|---|---|
| `，` | `,` | |
| `。` | `.` | |
| `：` | `:` | |
| `；` | `;` | |
| `？` | `?` | |
| `！` | `!` | |
| `（…）` | `(…)` | see §1.1 |
| `“…”` then `‘…’` | `「…」` | double-first is zh-**CN**; `「」` is zh-TW |
| `……` (two U+2026) | `…` | CLReq: six dots |
| `——` (two-em dash) | `-`, `–`, `—` | |
| `、` | `,` | enumeration comma, between list items |

### 1.1 Parentheses must not mix widths

A full-width open with a half-width close renders as `待审阅（3)`. This happens when the
opening paren is in the catalog and the closing one is a bare JSX literal — the codemod's
sentence split produced several. **Both halves belong in the same key**, so the correct
fix is one key containing `（{{count}}）`, not a matched pair of fragments.

Checked by `qa.test.ts` → `unbalanced-delimiter`.

### 1.2 Never store full-width Latin letters or digits

Full-width **punctuation** is correct; full-width **alphanumerics** are not. CLReq:
*"现今在文本储存时，应避免使用该区段的拉丁字母及数字字符，交由排版引擎处理"* — when storing
text, avoid the full-width alphabetic and numeric block; leave it to the layout engine.

Write `MCP 服务器 3 个`, never `ＭＣＰ服务器３个`.

Checked by `qa.test.ts` → `fullwidth-alphanumeric`, which is at zero and gates outright.

---

## 2. Spacing between Han and Latin

**Do not insert a space.** CLReq §6.3.3 specifies *"tracking or spacing between an
adjacent Han character and a Western character or a European numeral of **up to 1/4
em**"* — a typographic gap the layout engine produces, not a `U+0020` an author types.
The elastic range is 1/8 em to 1/2 em, and it is **not** applied around Chinese commas
and full stops, nor inside Chinese brackets.

A hand-typed space actively harms the result: CSS `text-autospace` inserts the gap *"only
if there are no existing spaces"*, so a literal space makes the engine stand down and you
get a Western word space instead — wrong width, wrong justification priority, and wrong at
a line end.

Write `使用 MCP 服务器`… no. Write `使用MCP服务器` and let the engine space it.

> Where this collides with the existing catalog: several values already contain
> hand-typed spaces around Latin runs. They are not being mass-edited in this PR, since
> the rendered difference is small and the churn across 3900 keys is not. New and touched
> strings should follow the rule.

---

## 3. Terminology

`src/i18n/glossary.json` is the source of truth. It exists because the same English term
currently has divergent Chinese renderings across keys — measured on this catalog:

| English | renderings in use |
|---|---|
| `active` | 启用 / 已启用 / 活跃 |
| `Back` | 上一步 / 后退 / 返回 |
| `Created` | 创建 / 创建于 / 已创建 |
| `Review` | 审查 / 审阅 |
| `OK` | 成功 / 确定 |

**39 English values have more than one Chinese rendering, 31 of them term-level.** A
single-language contributor cannot see this — they are editing one file out of ten — which
is why it is a data file with a test rather than a convention.

Checked by `glossary.test.ts`, which fails on any use of a `forbidden` rendering.

---

## 4. Do not translate

Product names, protocol names, CLI tokens, file paths and code identifiers stay in Latin
script. Mozilla's guide is the precedent: trademarks keep their original script *"in
prominent UI and at first use, even for non-Latin locales"*, and the meaning of a product
name is never translated.

The list lives in `glossary.json` under `dnt: true`. `KiroCrew`, `MCP`, `Slack`,
`GitHub`, `Playwright` and the `kirocrew` CLI verbs are all on it.

Two consequences worth knowing:

- **Do not put spaces around a DNT term** to "separate" it from Chinese text — §2.
- A Latin run inside Chinese text needs no `lang` attribute for WCAG: SC 3.1.2 explicitly
  exempts *"proper names, technical terms, words of indeterminate language"*. It does
  affect CJK font selection, which is handled in CSS rather than in the catalog.

---

## 5. Register and tone

- Address the user as **你**, not **您**. The product's English voice is direct and
  unceremonious; **您** reads as service-desk formality and is inconsistent with it.
- No trailing period on a button, a label, or a single-clause tooltip. Full stops belong
  in prose sentences.
- Prefer a verb phrase for an action (`导出配置`) over a noun phrase (`配置的导出`).
- Translate meaning, not words. Mozilla: *"avoid literal translation at all costs."* A
  literal rendering that is grammatical but unidiomatic is a defect, not a nitpick.

---

## 6. Plurals

Never hand-roll. Chinese has one CLDR plural category (`other`), so a `{{count}}` key
needs exactly one form — but the English source needs `_one`/`_other`, and Russian needs
four. Pass `{{ count }}` and let `Intl.PluralRules` select; the registry of which keys are
plural is `pluralKeys.json`.

`量词` (measure words) belong inside the single Chinese form: `{{count}} 个会话`, not
`{{count}} 会话`.

---

## 7. What is mechanically enforced today

| rule | gate |
|---|---|
| balanced brackets and quotes, incl. mixed width | `qa.test.ts` |
| no full-width Latin or digits | `qa.test.ts` |
| no leading/trailing space, no doubled space | `qa.test.ts` |
| no lone connector or morpheme as a whole value | `qa.test.ts` |
| forbidden terminology | `glossary.test.ts` |
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories per language | `catalogParity.test.ts` |

Everything in §2, §4 and §5 is review-only. Those are the judgements a human has to make,
and the reason a translation PR still needs a reader who speaks the language.
