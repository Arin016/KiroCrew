/**
 * Regression for the board-card rename focus bug: right-click a card → Rename,
 * and the caret did not land in the rename input — you had to click the box
 * before typing. Root cause: the rename menus were migrated to Radix
 * (ContextMenu/DropdownMenu). Radix restores focus to its trigger (the card) on
 * close via onCloseAutoFocus, and that restore fires AFTER the rename input
 * mounts, stealing the input's autoFocus back to the card.
 *
 * Fix: a useEffect keyed on renamingSlot re-grabs focus on the next animation
 * frame (rAF fires after Radix's restore) and selects the text for immediate
 * overtype — the same rAF pattern already used for the new-chat textarea.
 *
 * The load-bearing assertion is the text selection: autoFocus alone focuses the
 * input but never selects it, so without the fix selectionEnd stays at the end
 * of the value. Asserting a full 0..length selection distinguishes the fix from
 * the plain autoFocus that the regression left in place. This lives in the
 * integration suite (MSW + renderWithProviders) because that harness opens
 * Radix menus reliably; the inline-mock src/test harness does not.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import ChatSidebar from '../src/pages/ChatSidebar'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../src/components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../src/pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const SLOT_KEY = 'slot-1'
const TITLE = 'My Session Title'
const slots = [{
  key: SLOT_KEY, title: TITLE, running: false, agent: 'kiroclaw',
  created: '2026-04-08T01:00:00Z', last_ts: '2026-04-08T02:00:00Z', folder_id: '', tags: [],
}]
const props = {
  slots, activeSlot: SLOT_KEY, unreadSlots: [] as string[],
  history: [], historyHasMore: false, defaultAgent: 'kiroclaw',
  installedAgents: [{ name: 'kiroclaw', source: 'builtin' }],
}

beforeEach(() => {
  localStorage.clear()
  server.use(
    http.get('/api/chat/tags', () => HttpResponse.json([])),
    http.get('/api/chat/tag-columns', () => HttpResponse.json([])),
    http.get('/api/chat/folders', () => HttpResponse.json([])),
    http.get('/api/chat/slots', () => HttpResponse.json(slots)),
  )
})

function rowFor(container: HTMLElement): HTMLElement {
  const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"] .session-row`)
  expect(row).toBeTruthy()
  return row as HTMLElement
}

function renameInput(): HTMLInputElement {
  const input = Array.from(document.querySelectorAll('input'))
    .find(i => (i as HTMLInputElement).value === TITLE) as HTMLInputElement
  expect(input).toBeTruthy()
  return input
}

describe('board card rename focus', () => {
  it('right-click → Rename focuses and selects the input (no extra click needed)', async () => {
    const { container } = renderWithProviders(<ChatSidebar {...(props as any)} />)

    fireEvent.contextMenu(rowFor(container))
    const rename = await screen.findByRole('menuitem', { name: /Rename/ })
    await act(async () => { fireEvent.click(rename) })
    // The focus effect defers to the next animation frame (to land after
    // Radix's onCloseAutoFocus trigger-restore), so wait one frame before
    // asserting. One rAF mirrors the single requestAnimationFrame in the fix;
    // if that ever becomes a chained/longer defer, bump this to a waitFor on
    // document.activeElement rather than adding more frames here.
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })

    const input = renameInput()
    expect(document.activeElement).toBe(input)
    // Full selection is the fix's fingerprint: autoFocus alone would leave the
    // caret at the end (selectionStart === selectionEnd === length).
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe(TITLE.length)
  })
})
