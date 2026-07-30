/**
 * Map a DOM keyboard event to a Playwright key name for the browse mirror.
 *
 * Kept separate from the panel (like `terminalKeys.ts` is from the terminal) so
 * the mapping is unit-testable without rendering, and so the allowlist here can
 * be checked against the backend's — `build_input_payload` validates the same
 * shape server-side, and a key this produces but the server rejects would be a
 * silently dead keystroke.
 *
 * Returns `null` for keys we deliberately do NOT forward, which the caller treats
 * as "let the browser handle it". That set is small on purpose: swallowing
 * everything would trap keyboard users and break dashboard shortcuts.
 */
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'

/** Named keys the backend accepts (must stay in sync with `_NAMED_KEYS`). */
const NAMED: Record<string, string> = {
  Enter: 'Enter',
  Tab: 'Tab',
  Backspace: 'Backspace',
  Delete: 'Delete',
  Insert: 'Insert',
  ArrowUp: 'ArrowUp',
  ArrowDown: 'ArrowDown',
  ArrowLeft: 'ArrowLeft',
  ArrowRight: 'ArrowRight',
  Home: 'Home',
  End: 'End',
  PageUp: 'PageUp',
  PageDown: 'PageDown',
  ' ': 'Space',
}

export function browserKeyFromEvent(e: ReactKeyboardEvent<HTMLElement>): string | null {
  const { key } = e
  // Bare modifier presses carry no keystroke of their own.
  if (key === 'Shift' || key === 'Control' || key === 'Alt' || key === 'Meta') return null

  const mods: string[] = []
  if (e.ctrlKey) mods.push('Control')
  if (e.altKey) mods.push('Alt')
  if (e.metaKey) mods.push('Meta')
  // Shift is implicit in the character the browser already gave us (`A` not
  // `Shift+a`), so only send it for named keys where it changes meaning.
  const named = NAMED[key]
  if (named && e.shiftKey) mods.push('Shift')

  if (named) return [...mods, named].join('+')
  // Single printable character. Longer `key` values are named keys we don't
  // forward (F-keys are reserved for the dashboard, Escape is the release).
  if (key.length === 1) return [...mods, key].join('+')
  return null
}
