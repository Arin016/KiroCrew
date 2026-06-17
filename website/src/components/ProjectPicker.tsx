import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft, Clock } from 'lucide-react'
import { api } from '../api/client'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef?: RefObject<HTMLElement | null>
  anchorRect?: DOMRect | null
  onSelect: (path: string) => void
}

export default function ProjectPicker({ open, onOpenChange, anchorRef, anchorRect, onSelect }: Props) {
  const [tab, setTab] = useState<'recent' | 'browse'>('recent')
  const [input, setInput] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [recentDirs, setRecentDirs] = useState<string[]>([])
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  const anchorRectRef = useRef<DOMRect | null>(anchorRect ?? null)
  anchorRectRef.current = anchorRect ?? null
  const getAnchorRect = useCallback((): DOMRect | null => {
    if (btnRef?.current && typeof btnRef.current.getBoundingClientRect === 'function') {
      return btnRef.current.getBoundingClientRect()
    }
    return anchorRectRef.current
  }, [btnRef])

  const browse = useCallback((path?: string) => {
    api.browseDirs(path).then(d => {
      setBrowsePath(d.path); setBrowseParent(d.parent); setBrowseDirs(d.dirs); setInput(d.path)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!open) return
    api.recentProjects().then(d => {
      setRecentDirs(d.dirs || [])
      setTab(d.dirs?.length ? 'recent' : 'browse')
    }).catch(() => setTab('browse'))
    browse()
  }, [open, browse])

  useEffect(() => {
    if (!open) return
    let cleanup = () => {}
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && dropRef.current.contains(e.target as Node)) return
        const target = e.target as Node | null
        const live = btnRef?.current
        if (live && typeof (live as Element).contains === 'function' && (live as Element).contains(target)) return
        const r = getAnchorRect()
        if (r && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
        onOpenChange(false)
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    return () => { clearTimeout(timer); cleanup() }
  }, [open, onOpenChange, btnRef, getAnchorRect])

  const select = (path: string) => { onSelect(path); onOpenChange(false) }

  const anchorR = getAnchorRect()
  if (!open || !anchorR) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : browseDirs

  return createPortal(
    <div ref={dropRef} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl w-[400px] flex flex-col overflow-hidden animate-slide-up" style={(() => {
      const dropMinH = 200
      const spaceBelow = window.innerHeight - anchorR.bottom - 8
      const flipUp = spaceBelow < dropMinH || anchorR.bottom > window.innerHeight / 2
      const left = Math.max(8, Math.min(anchorR.right - 400, window.innerWidth - 408))
      if (flipUp) {
        const spaceAbove = anchorR.top - 8
        return { bottom: window.innerHeight - anchorR.top + 4, left, maxHeight: Math.min(460, Math.max(200, spaceAbove)) }
      }
      return { top: anchorR.bottom + 4, left, maxHeight: Math.min(460, Math.max(200, spaceBelow)) }
    })()}>
      {/* Tabs */}
      <div className="flex border-b border-border">
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'recent' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('recent') }}>
          <Clock size={12} /> Recent
        </button>
        <button className={`flex-1 px-3 py-2 text-[12px] font-medium flex items-center justify-center gap-1.5 transition-colors ${tab === 'browse' ? 'text-accent border-b-2 border-accent' : 'text-muted hover:text-text'}`} onMouseDown={e => { e.preventDefault(); setTab('browse') }}>
          <FolderOpen size={12} /> Browse
        </button>
      </div>

      {tab === 'recent' ? (
        <div className="overflow-y-auto flex-1 min-h-0">
          {recentDirs.length === 0 ? (
            <div className="px-3 py-6 text-[12px] text-muted text-center">No recent projects</div>
          ) : recentDirs.map(d => (
            <button key={d} className="w-full text-left px-3 py-2 flex items-center gap-2 cursor-pointer hover:bg-bg-hover transition-colors" onMouseDown={e => { e.preventDefault(); select(d) }}>
              <FolderOpen size={12} className="text-accent shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-[13px] font-mono font-semibold text-text truncate">{d.split('/').pop()}</div>
                <div className="text-[11px] text-muted truncate">{d}</div>
              </div>
            </button>
          ))}
        </div>
      ) : (
        <>
          <div className="p-2 border-b border-border flex gap-1 items-center">
            {browseParent && browseParent !== browsePath && (
              <button onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title="Back"><ChevronLeft size={16} /></button>
            )}
            <input autoFocus type="text" placeholder="/path/to/project" value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && input.trim()) select(input.trim()); if (e.key === 'Escape') onOpenChange(false) }} className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus:border-accent" />
            <button disabled={!input.trim() && !browsePath} onMouseDown={e => { e.preventDefault(); select(input.trim() || browsePath) }} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed shrink-0">Select</button>
          </div>
          <div className="overflow-y-auto flex-1 min-h-0">
            {filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">No subdirectories</div>}
            {filteredBrowse.map(d => (
              <button key={d.path} className="w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer hover:bg-bg-hover transition-colors" onClick={() => browse(d.path)}>
                <FolderOpen size={12} className="text-accent shrink-0" />
                <span className="text-[13px] font-mono text-text truncate">{d.name}</span>
                <ChevronRight size={12} className="text-muted ml-auto shrink-0" />
              </button>
            ))}
          </div>
        </>
      )}
    </div>,
    document.body
  )
}
