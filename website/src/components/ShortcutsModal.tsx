import { safeSetItem } from '../utils/safeStorage'
import { useEffect, useState } from 'react'
import { X, Keyboard } from 'lucide-react'
import { DEFAULT_SHORTCUTS, formatShortcut, SHORTCUTS_ENABLED_KEY, SHORTCUTS_ENABLED_EVENT, IS_MAC, MAC_CTRL_DIGITS_KEY } from '../hooks/useKeyboardShortcuts'
import { Toggle } from './ui'

const GROUPS = ['Chat Navigation', 'Panel Navigation', 'Actions'] as const

function Kbd({ children }: { children: string }) {
  return <kbd className="inline-flex items-center justify-center min-w-[24px] h-6 px-1.5 rounded-md bg-bg border border-border text-[12px] font-mono font-medium text-text-strong shadow-sm">{children}</kbd>
}

export default function ShortcutsModal({ onClose }: { onClose: () => void }) {
  const [enabled, setEnabled] = useState(() => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0')
  const [macCtrl, setMacCtrl] = useState(() => localStorage.getItem(MAC_CTRL_DIGITS_KEY) !== '0')

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  const toggle = (v: boolean) => {
    safeSetItem(SHORTCUTS_ENABLED_KEY, v ? '1' : '0')
    setEnabled(v)
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
  }

  const toggleMacCtrl = (v: boolean) => {
    safeSetItem(MAC_CTRL_DIGITS_KEY, v ? '1' : '0')
    setMacCtrl(v)
    window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
  }

  return (
    // Backdrop click-to-dismiss is a supplementary mouse affordance; keyboard
    // users close via Escape, already wired through the document keydown
    // listener above, so the dialog role stays keyboard-accessible.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onClick={onClose}>
      {/* onClick only stops propagation so inner clicks don't hit the backdrop
          dismiss handler; it is event plumbing, not an interactive control. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div className="bg-card border border-border rounded-xl p-6 max-w-lg w-full mx-4 shadow-xl max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <div className="flex items-center gap-2 text-sm font-bold text-text-strong"><Keyboard size={16} /> Keyboard Shortcuts</div>
          <button className="text-muted cursor-pointer hover:text-text bg-transparent border-none" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        {GROUPS.map(group => {
          const items = DEFAULT_SHORTCUTS.filter(s => s.group === group).map(s => {
            // When Mac user toggles back to Alt+digit, adjust the display
            if (IS_MAC && !macCtrl && s.id.startsWith('chat-') && s.ctrl) {
              return { ...s, ctrl: false, alt: true }
            }
            return s
          })
          return (
            <div key={group} className="mb-5 last:mb-0">
              <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{group}</div>
              <div className="grid gap-1">
                {items.map(s => {
                  const parts = formatShortcut(s).split(' + ')
                  return (
                    <div key={s.id} className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-bg-hover transition-colors">
                      <span className="text-[13px] text-text">{s.label}</span>
                      <span className="flex items-center gap-1">{parts.map((p, i) => <span key={i} className="flex items-center gap-1">{i > 0 && <span className="text-muted text-[11px]">+</span>}<Kbd>{p}</Kbd></span>)}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
        <div className="mt-4 pt-3 border-t border-border flex items-center justify-between">
          <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
            <Toggle checked={enabled} onChange={toggle} label="Enable shortcuts" />
            <span>Enable shortcuts</span>
          </span>
          <span className="text-[12px] text-muted">
            <Kbd>{/Mac|iPhone|iPad/.test(navigator?.platform ?? '') ? '⌥' : 'Alt'}</Kbd> <span className="text-[11px]">+</span> <Kbd>K</Kbd> always works
          </span>
        </div>
        {IS_MAC && (
          <div className="mt-2 flex items-center">
            <span className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
              <Toggle checked={macCtrl} onChange={toggleMacCtrl} label="Use Ctrl (not Option) for chat 1 to 9" />
              <span>Use ⌃ Ctrl (not ⌥ Option) for chat 1–9</span>
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
