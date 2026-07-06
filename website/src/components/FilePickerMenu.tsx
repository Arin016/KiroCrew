import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { FileText, Eye } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'

interface FileResult {
  path: string
  name: string
  size: number
  mtime: number
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  onSelect: (info: { path: string; relativePath: string }) => void
  onClose: () => void
  onFileOpen?: (path: string) => void
  project?: string
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes + 'B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + 'KB'
  return (bytes / (1024 * 1024)).toFixed(1) + 'MB'
}

function formatAge(mtime: number): string {
  const diff = Date.now() / 1000 - mtime
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago'
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago'
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + 'd ago'
  return new Date(mtime * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function makeRelative(path: string, root: string): string {
  if (!root) return path
  const r = root.endsWith('/') ? root : root + '/'
  return path.startsWith(r) ? path.slice(r.length) : path
}

export default function FilePickerMenu({ query, anchorRef, open, onSelect, onClose, onFileOpen, project }: Props) {
  const [results, setResults] = useState<FileResult[]>([])
  const [loading, setLoading] = useState(false)
  const rootRef = useRef('')
  const resultsRef = useRef<FileResult[]>([])
  const onFileOpenRef = useRef(onFileOpen)
  onFileOpenRef.current = onFileOpen

  const choose = useCallback((idx: number) => {
    const r = resultsRef.current
    const f = r[idx >= r.length ? 0 : idx]
    if (f) onSelect({ path: f.path, relativePath: makeRelative(f.path, rootRef.current) })
  }, [onSelect])

  // Cmd/Ctrl+Enter opens the file in the viewer instead of inserting it.
  // Returns true to signal the hook to skip the default choose.
  const altEnter = useCallback((idx: number): boolean => {
    const f = resultsRef.current[idx]
    if (f && onFileOpenRef.current) { onFileOpenRef.current(f.path); onClose(); return true }
    return false
  }, [onClose])

  // Shared Arrow/Enter/Tab/Escape + scroll-into-view (see useListKeyboardNav).
  const { selected, setSelected, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
    onAltEnter: altEnter,
  })

  useEffect(() => {
    if (!open || query.length < 2) { setResults([]); resultsRef.current = []; setLoading(false); return }
    setLoading(true)
    const controller = new AbortController()
    const timer = setTimeout(() => {
      api.fileSearch(query, project, controller.signal)
        .then(d => { setResults(d.results || []); resultsRef.current = d.results || []; rootRef.current = d.root || ''; setSelected(0) })
        .catch(() => { if (!controller.signal.aborted) { setResults([]); resultsRef.current = [] } })
        .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    }, 200)
    return () => { clearTimeout(timer); controller.abort() }
  }, [query, open, project, setSelected])

  if (!open || !anchorRef.current) return null

  const rect = anchorRef.current.getBoundingClientRect()
  const menuH = Math.min((results.length || 1) * 48 + 8, 320)
  const above = rect.top - menuH - 4
  const top = above > 0 ? above : rect.bottom + 4

  const empty = query.length < 2
    ? <div className="px-3 py-3 text-[12px] text-muted">Type 2+ chars to search files…</div>
    : loading
    ? <div className="px-3 py-3 text-[12px] text-muted">Searching…</div>
    : <div className="px-3 py-3 text-[12px] text-muted">No matches</div>

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left: rect.left, width: Math.min(rect.width, 420), maxHeight: 320 }}
    >
      {results.length === 0 ? empty : results.map((f, i) => (
        <div
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          key={f.path}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          title={f.path}
          onMouseEnter={() => setSelected(i)}
          onMouseDown={e => { e.preventDefault(); onSelect({ path: f.path, relativePath: makeRelative(f.path, rootRef.current) }) }}
        >
          <FileText size={14} className="shrink-0 lucide-inline" />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-mono font-semibold truncate">{f.name}</div>
            <div className="text-[11px] text-muted truncate">{f.path}</div>
          </div>
          <span className="text-[11px] text-muted shrink-0 whitespace-nowrap">{formatSize(f.size)} · {formatAge(f.mtime)}</span>
          {onFileOpen && (
            <button
              type="button"
              aria-label="Open in viewer"
              tabIndex={-1}
              className="shrink-0 p-1 rounded hover:bg-bg-hover text-muted hover:text-text cursor-pointer bg-transparent border-none"
              title="Open in viewer"
              onMouseDown={e => { e.preventDefault(); e.stopPropagation(); onFileOpen(f.path); onClose() }}
            >
              <Eye size={16} />
            </button>
          )}
        </div>
      ))}
    </div>,
    document.body
  )
}
