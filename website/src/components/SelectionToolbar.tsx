import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquareQuote, Copy, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'

export interface SelectionAction {
  id: string
  icon: React.ReactNode
  label: string
  /** Called with selected text and the bounding rect of the selection */
  onClick: (text: string, rect: DOMRect) => void
}

interface SelectionToolbarProps {
  /** Container element to listen for text selection within */
  containerRef: React.RefObject<HTMLElement | null>
  /** Actions to show in the toolbar */
  actions: SelectionAction[]
}

/** Generic floating toolbar that appears when user selects text within a container.
 *  Extensible — pass any actions (quote, copy, etc.) via the `actions` prop. */
export default function SelectionToolbar({ containerRef, actions }: SelectionToolbarProps) {
  const [visible, setVisible] = useState(false)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const selectedTextRef = useRef('')
  const toolbarRef = useRef<HTMLDivElement>(null)

  const selectionRectRef = useRef<DOMRect | null>(null)

  const lastMouseRef = useRef({ x: 0, y: 0 })
  const triggeredByMouseRef = useRef(false)

  const checkSelection = useCallback(() => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      setVisible(false)
      return
    }

    const container = containerRef.current
    if (!container) { setVisible(false); return }

    // Ensure selection is within our container
    const range = sel.getRangeAt(0)
    if (!container.contains(range.commonAncestorContainer)) {
      setVisible(false)
      return
    }

    const text = sel.toString().trim()
    if (!text) { setVisible(false); return }

    selectedTextRef.current = text

    const rect = range.getBoundingClientRect()
    selectionRectRef.current = rect
    const x = triggeredByMouseRef.current
      ? lastMouseRef.current.x
      : rect.left + rect.width / 2
    const y = triggeredByMouseRef.current
      ? lastMouseRef.current.y + 8
      : rect.bottom + 8
    setPos({ x, y })
    setVisible(true)
  }, [containerRef])

  useEffect(() => {
    const onMouseUp = (e: MouseEvent) => {
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      triggeredByMouseRef.current = true
      lastMouseRef.current = { x: e.clientX, y: e.clientY }
      // Small delay to let selection finalize
      setTimeout(checkSelection, 10)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setVisible(false); return }
      // Check selection on Shift+Arrow keys (keyboard selection)
      if (e.shiftKey) {
        triggeredByMouseRef.current = false
        setTimeout(checkSelection, 10)
      }
    }

    const onMouseDown = (e: MouseEvent) => {
      // Don't dismiss if clicking inside the toolbar
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      // Don't dismiss if starting a new selection inside our container
      if (containerRef.current && containerRef.current.contains(e.target as Node)) return
      setVisible(false)
    }

    document.addEventListener('mouseup', onMouseUp)
    document.addEventListener('keyup', onKeyUp)
    document.addEventListener('mousedown', onMouseDown)
    return () => {
      document.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('keyup', onKeyUp)
      document.removeEventListener('mousedown', onMouseDown)
    }
  }, [checkSelection])

  const handleAction = useCallback((action: SelectionAction) => {
    const text = selectedTextRef.current
    if (!text) return
    const rect = selectionRectRef.current || new DOMRect(0, 0, 0, 0)
    action.onClick(text, rect)
    if (action.id === 'copy') {
      setCopiedId('copy')
      setTimeout(() => setCopiedId(null), 1500)
    } else {
      setVisible(false)
      window.getSelection()?.removeAllRanges()
    }
  }, [])

  return createPortal(
    <AnimatePresence>
      {visible && (
        <motion.div
          ref={toolbarRef}
          initial={{ opacity: 0, y: 4, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, scale: 0.95 }}
          transition={{ duration: 0.15 }}
          className="fixed z-[9999] pointer-events-auto"
          style={{ left: pos.x, top: pos.y, transform: 'translate(-50%, 0)' }}
        >
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg bg-bg-elevated border border-border shadow-lg">
            {actions.map(action => (
              <button
                key={action.id}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium text-text hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer whitespace-nowrap"
                onMouseDown={e => e.preventDefault()}
                onClick={() => handleAction(action)}
                aria-label={action.label}
                title={action.label}
              >
                {copiedId === action.id ? <Check size={12} className="text-ok" /> : action.icon}
                {action.label}
              </button>
            ))}
          </div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
  )
}

/** Pre-built actions for common use cases */
export function useSelectionActions(onQuote?: (text: string, rect: DOMRect) => void): SelectionAction[] {
  const actions: SelectionAction[] = []

  if (onQuote) {
    actions.push({
      id: 'quote',
      icon: <MessageSquareQuote size={12} />,
      label: 'Quote',
      onClick: onQuote,
    })
  }

  actions.push({
    id: 'copy',
    icon: <Copy size={12} />,
    label: 'Copy',
    onClick: (text) => { copyToClipboard(text) },
  })

  return actions
}
