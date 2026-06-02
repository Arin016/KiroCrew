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

  // Zoom: transform scale on #root (layout zoom)
  // useLayoutEffect ensures --mc-vh/--mc-vw and #root styles are set
  // synchronously before paint, preventing layout flicker on first render.
  useLayoutEffect(() => {
    const el = document.getElementById('root') as HTMLElement | null
    if (!el) return
    const s = zoom / 100
    if (s === 1) {
      el.style.transform = ''
      el.style.transformOrigin = ''
      el.style.width = ''
      el.style.minHeight = ''
      el.style.height = ''
    } else {
      el.style.transform = `scale(${s})`
      el.style.transformOrigin = 'top left'
      el.style.width = `${100 / s}vw`
      el.style.minHeight = `${100 / s}vh`
      el.style.height = 'auto'
    }
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
      el.style.transform = ''
      el.style.transformOrigin = ''
      el.style.width = ''
      el.style.minHeight = ''
      el.style.height = ''
    }
  }, [zoom])

  // Font scale: html font-size scales all rem-based text (Tailwind classes).
  useEffect(() => {
    document.documentElement.style.fontSize = `${fontScale}%`
    return () => { document.documentElement.style.removeProperty('font-size') }
  }, [fontScale])

  useEffect(() => {
    document.documentElement.style.setProperty('--font-body', FAMILY_MAP[family])
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
