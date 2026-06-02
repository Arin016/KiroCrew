import { describe, it, expect } from 'vitest'
import { buildSrcdoc, THEME_VAR_NAMES } from '../lib/widgetSrcdoc'

describe('widgetSrcdoc', () => {
  it('embeds the html body', () => {
    const out = buildSrcdoc({
      html: '<p>hello world</p>',
      themeVars: {},
      mode: 'dark',
    })
    expect(out).toContain('<p>hello world</p>')
  })

  it('applies the dark mode class', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    expect(out).toContain('<body class="dark">')
  })

  it('applies the light mode class', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'light' })
    expect(out).toContain('<body class="light">')
  })

  it('serializes theme vars into a :root style block', () => {
    const out = buildSrcdoc({
      html: '<x/>',
      themeVars: { '--bg': '#000', '--text': '#fff' },
      mode: 'dark',
    })
    expect(out).toMatch(/--bg:#000/)
    expect(out).toMatch(/--text:#fff/)
    expect(out).toContain('color-scheme:dark')
  })

  it('omits the height reporter by default', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'light' })
    expect(out).not.toContain('mc-widget-height')
  })

  it('includes the height reporter when requested', () => {
    const out = buildSrcdoc({
      html: '',
      themeVars: {},
      mode: 'light',
      includeHeightReporter: true,
    })
    expect(out).toContain('mc-widget-height')
    expect(out).toContain('mc-widget-action')
  })

  it('includes the Tailwind CDN script and dark-mode config', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    expect(out).toContain('cdn.tailwindcss.com')
    expect(out).toContain("darkMode:'class'")
  })

  it('sets the strict CSP', () => {
    const out = buildSrcdoc({ html: '', themeVars: {}, mode: 'dark' })
    expect(out).toContain("default-src 'none'")
    expect(out).toContain("connect-src 'none'")
    expect(out).toContain("base-uri 'none'")
  })

  it('exports the canonical theme variable names', () => {
    expect(THEME_VAR_NAMES).toContain('--bg')
    expect(THEME_VAR_NAMES).toContain('--text')
    expect(THEME_VAR_NAMES).toContain('--accent')
    expect(THEME_VAR_NAMES).toContain('--danger')
  })
})
