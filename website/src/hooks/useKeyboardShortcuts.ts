import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot } from '../store/chatSlice'

export const SHORTCUTS_ENABLED_KEY = 'mc-keyboard-shortcuts'
export const SHORTCUTS_ENABLED_EVENT = 'mc-keyboard-shortcuts-changed'

export interface ShortcutDef {
  id: string
  key: string
  alt?: boolean
  shift?: boolean
  label: string
  group: 'Chat Navigation' | 'Panel Navigation' | 'Actions'
}

export const DEFAULT_SHORTCUTS: ShortcutDef[] = [
  // Chat navigation
  { id: 'chat-1', key: '1', alt: true, label: 'Jump to chat 1', group: 'Chat Navigation' },
  { id: 'chat-2', key: '2', alt: true, label: 'Jump to chat 2', group: 'Chat Navigation' },
  { id: 'chat-3', key: '3', alt: true, label: 'Jump to chat 3', group: 'Chat Navigation' },
  { id: 'chat-4', key: '4', alt: true, label: 'Jump to chat 4', group: 'Chat Navigation' },
  { id: 'chat-5', key: '5', alt: true, label: 'Jump to chat 5', group: 'Chat Navigation' },
  { id: 'chat-6', key: '6', alt: true, label: 'Jump to chat 6', group: 'Chat Navigation' },
  { id: 'chat-7', key: '7', alt: true, label: 'Jump to chat 7', group: 'Chat Navigation' },
  { id: 'chat-8', key: '8', alt: true, label: 'Jump to chat 8', group: 'Chat Navigation' },
  { id: 'chat-9', key: '9', alt: true, label: 'Jump to chat 9', group: 'Chat Navigation' },
  { id: 'chat-prev', key: 'ArrowLeft', alt: true, label: 'Previous chat', group: 'Chat Navigation' },
  { id: 'chat-next', key: 'ArrowRight', alt: true, label: 'Next chat', group: 'Chat Navigation' },
  { id: 'chat-mru', key: '`', alt: true, label: 'Last visited chat (MRU)', group: 'Chat Navigation' },
  { id: 'chat-mru-back', key: '`', alt: true, shift: true, label: 'Walk back MRU history', group: 'Chat Navigation' },
  // Panel navigation
  { id: 'nav-chat', key: 'c', alt: true, label: 'Chats panel', group: 'Panel Navigation' },
  { id: 'nav-notifications', key: 'n', alt: true, label: 'Notifications panel', group: 'Panel Navigation' },
  { id: 'nav-projects', key: 'p', alt: true, label: 'Projects panel', group: 'Panel Navigation' },
  { id: 'nav-schedule', key: 's', alt: true, label: 'Schedule panel', group: 'Panel Navigation' },
  // Actions
  { id: 'focus-input', key: 'Enter', alt: true, label: 'Focus text input', group: 'Actions' },
  { id: 'new-chat', key: 'n', alt: true, shift: true, label: 'New chat', group: 'Actions' },
  { id: 'shortcuts-modal', key: 'k', alt: true, label: 'Open shortcuts help', group: 'Actions' },
  { id: 'open-settings', key: ',', alt: true, label: 'Open settings', group: 'Actions' },
  { id: 'cycle-agent', key: 'a', alt: true, shift: true, label: 'Cycle agent', group: 'Actions' },

  { id: 'cycle-approval', key: 'd', alt: true, shift: true, label: 'Cycle approval mode', group: 'Actions' },
  { id: 'cycle-prev-agent', key: 'z', alt: true, shift: true, label: 'Previous agent', group: 'Actions' },

  { id: 'cycle-prev-approval', key: 'c', alt: true, shift: true, label: 'Previous approval mode', group: 'Actions' },
]

const isMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

export function formatShortcut(def: ShortcutDef): string {
  const mac = isMac()
  const parts: string[] = []
  if (def.alt) parts.push(mac ? '\u2325' : 'Alt')
  if (def.shift) parts.push(mac ? '\u21e7' : 'Shift')
  const keyLabel = def.key === 'ArrowLeft' ? '\u2190' : def.key === 'ArrowRight' ? '\u2192' : def.key === '`' ? '`' : def.key === 'Enter' ? (mac ? '\u23ce' : 'Enter') : def.key === ',' ? ',' : def.key.toUpperCase()
  parts.push(keyLabel)
  return parts.join(mac ? '' : ' + ')
}

interface UseKeyboardShortcutsOpts {
  onToggleShortcutsModal: () => void
  onNewChat: () => void
  onCycleAgent?: () => void
  onCycleApprovalMode?: () => void
  onCyclePrevAgent?: () => void
  onCyclePrevApprovalMode?: () => void
  disabled?: boolean
}

export function useKeyboardShortcuts({ onToggleShortcutsModal, onNewChat, onCycleAgent, onCycleApprovalMode, onCyclePrevAgent, onCyclePrevApprovalMode, disabled }: UseKeyboardShortcutsOpts) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const slotHistory = useAppSelector(s => s.chat.slotHistory)
  const mruIndexRef = useRef(-1)
  const [enabled, setEnabled] = useState(() => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')

  // Listen for toggle changes from Settings
  useEffect(() => {
    const onToggle = () => setEnabled(localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
    window.addEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
    return () => window.removeEventListener(SHORTCUTS_ENABLED_EVENT, onToggle)
  }, [])

  // Reset MRU walk index when Alt is released
  useEffect(() => {
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Alt') mruIndexRef.current = -1
    }
    document.addEventListener('keyup', onKeyUp)
    return () => document.removeEventListener('keyup', onKeyUp)
  }, [])

  const handler = useCallback((e: KeyboardEvent) => {
    const tag = (e.target as HTMLElement)?.tagName
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable

    if (!e.altKey || e.ctrlKey || e.metaKey) return

    // Use e.code for all matching — on Mac, Option+key produces unicode chars in e.key
    const code = e.code

    // Alt+K: Shortcuts modal — always works, even when disabled or in input
    if (code === 'KeyK' && !e.shiftKey) {
      e.preventDefault()
      onToggleShortcutsModal()
      return
    }

    // Alt+,: Settings — always works so user can re-enable shortcuts
    if (code === 'Comma' && !e.shiftKey) {
      e.preventDefault()
      navigate('/settings')
      return
    }

    // Suppress all shortcuts when globally disabled via settings
    if (!enabled) return

    // Suppress all other shortcuts when disabled (e.g. modal open)
    if (disabled) return

    // Alt+Shift+A: Cycle agent
    if (e.shiftKey && code === 'KeyA') {
      e.preventDefault()
      onCycleAgent?.()
      return
    }

    // Alt+Shift+D: Cycle approval mode
    if (e.shiftKey && code === 'KeyD') {
      e.preventDefault()
      onCycleApprovalMode?.()
      return
    }



    if (e.shiftKey && code === 'KeyZ') { e.preventDefault(); onCyclePrevAgent?.(); return }

    if (e.shiftKey && code === 'KeyC') { e.preventDefault(); onCyclePrevApprovalMode?.(); return }

    // Alt+Enter: Focus text input — works even from other inputs
    if (code === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()
      return
    }

    // Alt+Shift+N: New chat (check before Alt+N panel nav)
    if (e.shiftKey && code === 'KeyN') {
      e.preventDefault()
      onNewChat()
      return
    }

    // Alt+Shift+`: Walk back MRU history
    if (e.shiftKey && code === 'Backquote') {
      e.preventDefault()
      if (slotHistory.length === 0) return
      mruIndexRef.current = Math.min(mruIndexRef.current + 1, slotHistory.length - 1)
      const target = slotHistory[slotHistory.length - 1 - mruIndexRef.current]
      if (target) { dispatch(switchSlot(target)); navigate('/chat') }
      return
    }

    // Alt+`: MRU toggle (last visited)
    if (code === 'Backquote' && !e.shiftKey) {
      e.preventDefault()
      const prev = slotHistory.length > 0 ? slotHistory[slotHistory.length - 1] : null
      if (prev && prev !== activeSlot) { dispatch(switchSlot(prev)); navigate('/chat') }
      return
    }

    // Alt+1-9: Jump to chat N
    if (code >= 'Digit1' && code <= 'Digit9' && !e.shiftKey) {
      const idx = parseInt(code.charAt(5)) - 1
      e.preventDefault()
      if (idx < slots.length) { dispatch(switchSlot(slots[idx].key)); navigate('/chat') }
      return
    }

    // Alt+←/→: Previous/next chat (skip when in text input to preserve word-jump)
    if ((code === 'ArrowLeft' || code === 'ArrowRight') && !isInput) {
      e.preventDefault()
      if (slots.length === 0) return
      const curIdx = activeSlot ? slots.findIndex(s => s.key === activeSlot) : -1
      const nextIdx = code === 'ArrowLeft'
        ? (curIdx <= 0 ? slots.length - 1 : curIdx - 1)
        : (curIdx >= slots.length - 1 ? 0 : curIdx + 1)
      dispatch(switchSlot(slots[nextIdx].key))
      navigate('/chat')
      return
    }

    // Skip remaining shortcuts if user is in an input field
    if (isInput) return

    // Panel navigation
    const panelMap: Record<string, string> = { KeyC: '/chat', KeyN: '/notifications', KeyP: '/projects', KeyS: '/schedule' }
    if (!e.shiftKey && panelMap[code]) {
      e.preventDefault()
      navigate(panelMap[code])
      return
    }
  }, [dispatch, navigate, slots, activeSlot, slotHistory, onToggleShortcutsModal, onNewChat, onCycleAgent, onCycleApprovalMode, onCyclePrevAgent, onCyclePrevApprovalMode, disabled, enabled])

  useEffect(() => {
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [handler])
}
