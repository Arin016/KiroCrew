import { useState, useRef, useCallback, useEffect } from 'react'

/**
 * Shared keyboard navigation for the autocomplete picker menus
 * (FilePickerMenu, SkillPickerMenu). Extracted to one place so the
 * Arrow/Enter/Tab/Escape handling + scroll-into-view isn't duplicated
 * across pickers (jscpd clone, Mesh-588).
 *
 * Owns:
 *  - `selected` index state (+ a ref mirror for use inside the keydown
 *    listener, which is registered once and would otherwise close over a
 *    stale value).
 *  - the document-level keydown listener (capture phase) while `open`.
 *  - scrolling the active option into view via `itemRefs`.
 *
 * Behaviour:
 *  - ArrowDown / ArrowUp move the selection and clamp at the edges
 *  - Enter / Tab choose the current item via `onChoose(index)`.
 *  - Escape (and Enter/Tab on an empty list) calls `onClose`.
 *  - Optional `onAltEnter(index)` fires for Cmd/Ctrl+Enter (FilePicker's
 *    "open in viewer"); when it returns true the default choose is skipped.
 */
export function useListKeyboardNav(opts: {
  open: boolean
  count: number
  onChoose: (index: number) => void
  onClose: () => void
  onAltEnter?: (index: number) => boolean
}) {
  const { open, count, onChoose, onClose, onAltEnter } = opts
  const [selected, setSelected] = useState(0)
  const selectedRef = useRef(0)
  const itemRefs = useRef<(HTMLElement | null)[]>([])

  // Keep refs current for the once-registered keydown listener.
  const countRef = useRef(count)
  countRef.current = count
  const onChooseRef = useRef(onChoose)
  onChooseRef.current = onChoose
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const onAltEnterRef = useRef(onAltEnter)
  onAltEnterRef.current = onAltEnter

  const setSelectedSynced = useCallback((next: number) => {
    selectedRef.current = next
    setSelected(next)
  }, [])

  // Reset selection whenever the menu (re)opens.
  useEffect(() => {
    if (open) setSelectedSynced(0)
  }, [open, setSelectedSynced])

  const onKey = useCallback((e: KeyboardEvent) => {
    if (!open) return
    const n = countRef.current
    if (n === 0) {
      if (e.key === 'Escape' || e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault(); e.stopPropagation(); onCloseRef.current()
      }
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault(); e.stopPropagation()
      setSelectedSynced(Math.min(selectedRef.current + 1, n - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation()
      setSelectedSynced(Math.max(selectedRef.current - 1, 0))
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault(); e.stopPropagation()
      const idx = selectedRef.current
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && onAltEnterRef.current?.(idx)) {
        return
      }
      onChooseRef.current(idx)
    } else if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation(); onCloseRef.current()
    }
  }, [open, setSelectedSynced])

  useEffect(() => {
    if (!open) return
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open, onKey])

  // Keep the highlighted option visible. block:'nearest' only scrolls when the
  // item is off-screen (no jump when already visible) and handles both
  // directions + wrap-around. Guard scrollIntoView: jsdom (and some older
  // browsers) don't implement it, and an unguarded call throws inside the
  // effect, which crashes the menu render.
  useEffect(() => {
    if (!open) return
    const el = itemRefs.current[selected]
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest' })
    }
  }, [selected, open])

  return { selected, setSelected: setSelectedSynced, selectedRef, itemRefs }
}
