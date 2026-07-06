import { useState, useEffect, useLayoutEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api/client'
import { useAppDispatch } from '../../store'
import { sseSlotColor } from '../../store/dashboardSlice'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { colorName } from '../../utils/sessionColors'

export default function SessionColorPicker({ slotKey, colorIndex }: { slotKey?: string; colorIndex?: number | null }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()

  const color = colorIndex != null && colorIndex >= 0 && colorIndex < paletteColors.length ? paletteColors[colorIndex] : null

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (btnRef.current?.contains(e.target as Node)) return
      if (popoverRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onKey)
    const t = setTimeout(() => document.addEventListener('click', close), 0)
    return () => { clearTimeout(t); document.removeEventListener('click', close); document.removeEventListener('keydown', onKey) }
  }, [open])

  useLayoutEffect(() => {
    if (open && popoverRef.current && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect()
      const h = popoverRef.current.offsetHeight
      popoverRef.current.style.top = (r.bottom + 6 + h > window.innerHeight ? r.top - h - 6 : r.bottom + 6) + 'px'
      popoverRef.current.style.left = Math.max(8, r.left - 40) + 'px'
      popoverRef.current.style.visibility = 'visible'
    }
  }, [open])

  if (!slotKey) return null

  return (
    <>
      <button ref={btnRef} className="shrink-0 cursor-pointer transition-all hover:scale-125 pl-1" onClick={() => setOpen(!open)} title="Session color" aria-label="Session color" aria-expanded={open}>
        <span className="block w-3 h-3 rounded-full border-[1.5px] transition-colors" style={color ? { background: color, borderColor: color, boxShadow: `0 0 4px ${color}` } : { background: 'transparent', borderColor: 'var(--muted)' }} />
      </button>
      {open && btnRef.current && createPortal(
        <div ref={popoverRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg p-2.5 w-fit animate-slide-up" style={{ visibility: 'hidden' }}>
          <div className="flex flex-wrap items-center gap-1.5">
          <button type="button" aria-label="No color" aria-pressed={colorIndex == null} className={`w-6 h-6 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${colorIndex == null ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => { dispatch(sseSlotColor({ key: slotKey, color_index: null })); api.setSlotColor(slotKey, null).catch(() => {}); setOpen(false) }} title="No color" />
          {paletteColors.map((c, i) => (
            <button type="button" key={i} aria-label={colorName(c)} aria-pressed={colorIndex === i} className={`w-6 h-6 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${colorIndex === i ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: c }} onClick={() => { dispatch(sseSlotColor({ key: slotKey, color_index: i })); api.setSlotColor(slotKey, i).catch(() => {}); setOpen(false) }} title={colorName(c)} />
          ))}
          </div>
          <div className="text-[11px] text-muted mt-1.5">Change your color palette in Display settings</div>
        </div>,
        document.body
      )}
    </>
  )
}
