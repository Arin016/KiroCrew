import { useState, useEffect, useLayoutEffect, useCallback } from 'react'

export type FontFamily = 'sans' | 'mono' | 'system'

const MIN = 80, MAX = 150, STEP = 10
const FONT_MIN = 100, FONT_MAX = 250, FONT_STEP = 10
const FAMILIES: FontFamily[] = ['sans', 'mono', 'system']
const FAMILY_MAP: Record<FontFamily, string> = {
  sans: "'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif",
  mono: "'JetBrains Mono',ui-monospace,SFMono-Regular,monospace",
  system: "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
}

export function useZoom() {
  const [zoom, setZoom] = useState(() => parseInt(localStorage.getItem('mc-zoom') || '100', 10))
  const [fontScale, setFontScale] = useState(() => parseInt(localStorage.getItem('mc-font-scale') || '100', 10))
  const [family, setFamily] = useState<FontFamily>(
    () => (localStorage.getItem('mc-font-family') as FontFamily) || 'sans'
  )

  // Zoom: the CSS `zoom` property on #root (a TRUE layout zoom).
  //
  // We deliberately use `zoom`, NOT `transform: scale()`. `transform: scale` is
  // paint-only: it visually resizes pixels but leaves every element's layout box
  // at its original size, so scroll math, text selection / hit-testing, and
  // position:fixed all operate on the unscaled boxes — the UI looks scaled but
  // you can't scroll to or click what you see (the reported bug). `zoom` instead
  // reflows layout: every component gets a genuinely larger/smaller box, so
  // scrolling, selection, and fixed positioning all work interactively. It is
  // supported in all target browsers (Chrome/Edge/Safari always; Firefox 126+).
  // Because `zoom` scales the coordinate system, the shell's `h-screen`/`100vh`
  // resolves correctly at any level with no width/min-height compensation.
  //
  // useLayoutEffect sets it synchronously before paint to avoid first-render flicker.
  useLayoutEffect(() => {
    const el = document.getElementById('root') as HTMLElement | null
    if (!el) return
    const s = zoom / 100
    // setProperty('zoom', …) avoids depending on `zoom` being in the TS DOM lib.
    if (s === 1) {
      el.style.removeProperty('zoom')
    } else {
      el.style.setProperty('zoom', String(s))
    }
    // --mc-vh/--mc-vw: ChatInput caps its textarea against the usable viewport.
    // Under `zoom`, a raw px value is read in the zoomed coordinate space, so
    // innerHeight/s px still maps to the real viewport height — keep the existing
    // semantics so the input cap stays correct at any zoom.
    const updateVh = () => {
      document.documentElement.style.setProperty('--mc-vh', `${window.innerHeight / s}px`)
      document.documentElement.style.setProperty('--mc-vw', `${window.innerWidth / s}px`)
    }
    updateVh()
    window.addEventListener('resize', updateVh)
    return () => {
      window.removeEventListener('resize', updateVh)
      document.documentElement.style.removeProperty('--mc-vh')
      document.documentElement.style.removeProperty('--mc-vw')
      el.style.removeProperty('zoom')
    }
  }, [zoom])

  // Font scale: html font-size scales all rem-based text (Tailwind classes).
  useEffect(() => {
    document.documentElement.style.fontSize = `${fontScale}%`
    return () => { document.documentElement.style.removeProperty('font-size') }
  }, [fontScale])

  useEffect(() => {
    // Apply --font-body from the user's Font Family preference, with one
    // exception: when the dashboard is in CLI mode (data-ui="cli") AND the
    // user is on the default 'sans' (i.e. has never explicitly picked a
    // family), resolve to 'mono' so the CLI surface looks monospace by
    // default. If the user explicitly picks Mono / Sans / System, that
    // choice is honoured everywhere — including CLI mode.
    const html = document.documentElement
    const apply = () => {
      const ui = html.dataset.ui
      // Auto-resolve to mono in CLI mode for the default family ('sans').
      // Explicit 'mono' / 'system' choices are always honoured.
      const isDefaultFamily = family === 'sans'
      const effective: FontFamily =
        (ui === 'cli' && isDefaultFamily) ? 'mono' : family
      html.style.setProperty('--font-body', FAMILY_MAP[effective])
    }
    apply()
    // Re-apply on data-ui changes (e.g. user toggles Interface in Settings).
    const obs = new MutationObserver(apply)
    obs.observe(html, { attributes: true, attributeFilter: ['data-ui'] })
    return () => obs.disconnect()
  }, [family])

  const set = useCallback((v: number) => {
    const c = Math.max(MIN, Math.min(MAX, v))
    localStorage.setItem('mc-zoom', String(c))
    setZoom(c)
  }, [])

  const zoomIn = useCallback(() => set(zoom + STEP), [zoom, set])
  const zoomOut = useCallback(() => set(zoom - STEP), [zoom, set])
  const reset = useCallback(() => set(100), [set])

  const setFontFamily = useCallback((f: FontFamily) => {
    localStorage.setItem('mc-font-family', f)
    setFamily(f)
  }, [])

  const cycleFamily = useCallback(() => {
    const next = FAMILIES[(FAMILIES.indexOf(family) + 1) % FAMILIES.length]
    localStorage.setItem('mc-font-family', next)
    setFamily(next)
  }, [family])

  const setFontScaleVal = useCallback((v: number) => {
    const c = Math.max(FONT_MIN, Math.min(FONT_MAX, v))
    localStorage.setItem('mc-font-scale', String(c))
    setFontScale(c)
  }, [])
  const fontScaleUp = useCallback(() => setFontScaleVal(fontScale + FONT_STEP), [fontScale, setFontScaleVal])
  const fontScaleDown = useCallback(() => setFontScaleVal(fontScale - FONT_STEP), [fontScale, setFontScaleVal])
  const fontScaleReset = useCallback(() => setFontScaleVal(100), [setFontScaleVal])

  return { zoom, zoomIn, zoomOut, reset, fontScale, fontScaleUp, fontScaleDown, fontScaleReset, family, setFontFamily, cycleFamily }
}
