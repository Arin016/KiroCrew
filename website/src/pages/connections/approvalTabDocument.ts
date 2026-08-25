/**
 * The approval tab's placeholder document — MARKUP AND CSS ONLY.
 *
 * A Connect click opens a blank tab before anyone knows where it should go (see
 * `oneClickTab.ts`), and that tab needs something to show for the couple of
 * seconds until the mint answers. `document.write` needs a serialized document,
 * so this module is unavoidably a bag of tag and style literals.
 *
 * Keep it that way. Every user-visible word arrives through `ApprovalTabCopy`,
 * already translated by the caller, and is HTML-escaped on the way in. This
 * module must never gain copy of its own: it carries an i18n-lint exemption for
 * its markup (see `eslint.i18n.config.js`), so a hardcoded English string added
 * here would ship untranslated and ungated.
 */

/** Placeholder copy, translated by the caller — this module owns no strings. */
export interface ApprovalTabCopy {
  title: string
  message: string
  /** BCP-47 tag for the placeholder document's `lang`. */
  lang: string
}

const HTML_ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

/** Escape translated copy for the placeholder document. */
export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => HTML_ESCAPES[character] ?? character)
}

/**
 * Serialize the placeholder document.
 *
 * Every interpolated value is escaped translated copy. No URL is ever written
 * into this document: the approval URL reaches the tab through `location`, so it
 * never becomes markup, link text, or a referrer.
 */
export function placeholderHtml(copy: ApprovalTabCopy): string {
  return [
    '<!doctype html>',
    `<html lang="${escapeHtml(copy.lang || 'en')}">`,
    '<head>',
    '<meta charset="utf-8">',
    '<meta name="referrer" content="no-referrer">',
    `<title>${escapeHtml(copy.title)}</title>`,
    '<style>',
    'body{margin:0;display:flex;align-items:center;justify-content:center;',
    'min-height:100vh;background:#0b0d12;color:#e6e8ee;',
    'font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}',
    'main{max-width:34rem;padding:2rem;text-align:center}',
    'h1{margin:0 0 .5rem;font-size:1.05rem;font-weight:600}',
    'p{margin:0;color:#9aa3b2}',
    '@media(prefers-color-scheme:light){body{background:#fff;color:#1a1d24}p{color:#5b6472}}',
    '</style>',
    '</head>',
    `<body><main><h1>${escapeHtml(copy.title)}</h1>`,
    `<p>${escapeHtml(copy.message)}</p></main></body></html>`,
  ].join('')
}
