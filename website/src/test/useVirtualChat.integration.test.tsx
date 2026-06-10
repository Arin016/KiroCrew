// Feature: chat-virtualizer — useVirtualChat composing-hook integration tests.
//
// The pure pieces (FollowController, WindowCalculator, HeightCache) are unit-
// tested in isolation. This suite covers the WIRING that those unit tests
// can't reach — the effects/refs that historically caused the follow/yank
// regressions: append-pin while followed, a user scroll-up releasing follow so
// a later append does NOT yank, and a slot switch force-pinning to the bottom.
//
// jsdom has no layout engine, so scrollTop/scrollHeight/clientHeight are faked
// on a controlled detached scroller element passed via `externalScrollerRef`.
// The follow logic reads `scrollerRef.current` + live geometry synchronously
// inside layout effects, so these assertions are deterministic — they do not
// depend on rAF, ResizeObserver, or IntersectionObserver timing. (ResizeObserver
// is intentionally undefined in the test env, so the RO auto-pin never fires and
// can't perturb the result.)

import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

/** A detached div with controllable, mutable scroll geometry. */
function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  // forcePin/pinAuto write `el.scrollTop` directly; scrollToBottom may use
  // scrollTo — map it onto the same backing state for completeness.
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

function render(geom: Geom, items: Item[], sessionId: string) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const initialProps: UseVirtualChatOptions<Item> = {
    items,
    sessionId,
    getKey,
    externalScrollerRef: ref,
  }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps },
  )
  return { el, state, view }
}

describe('useVirtualChat integration: follow / pin wiring', () => {
  beforeEach(() => localStorage.clear())

  it('pins to the new bottom when items append while followed', () => {
    // Mount at the bottom (content == viewport). Slot-entry forcePin lands at 0.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 400, clientHeight: 400 },
      mkItems(5),
      'append-followed',
    )
    expect(el.scrollTop).toBe(0)

    // A new message arrives: content grows and the item count increases.
    act(() => {
      state.scrollHeight = 900
      view.rerender({ items: mkItems(6), sessionId: 'append-followed', getKey, externalScrollerRef: { current: el } })
    })

    // The append layout effect pinned to the new bottom (900 - 400).
    expect(el.scrollTop).toBe(500)
  })

  it('does NOT yank the user back to the bottom after a scroll-up, on a later append', () => {
    // Tall content, mounted at the bottom: forcePin → 2000 - 400 = 1600.
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
      'scrollup-release',
    )
    expect(el.scrollTop).toBe(1600)

    // User scrolls up to read history (well away from the bottom).
    act(() => { state.scrollTop = 600 })

    // A new message appends. The race-proof guard in pinAuto reads the live
    // scrollTop, sees the user moved up (distance from bottom >> epsilon), and
    // releases follow instead of pinning.
    act(() => {
      state.scrollHeight = 2200
      view.rerender({ items: mkItems(6), sessionId: 'scrollup-release', getKey, externalScrollerRef: { current: el } })
    })

    // Position preserved — no yank back to 1800.
    expect(el.scrollTop).toBe(600)
  })

  it('force-pins to the bottom on slot switch even if the previous slot was scrolled up', () => {
    const { el, state, view } = render(
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
      'slot-a',
    )
    expect(el.scrollTop).toBe(1600)

    // User scrolled up in slot A…
    act(() => { state.scrollTop = 600 })

    // …then switches to slot B. Slot entry deterministically force-pins to the
    // true bottom (does not inherit the previous slot's scroll position).
    act(() => {
      view.rerender({ items: mkItems(5), sessionId: 'slot-b', getKey, externalScrollerRef: { current: el } })
    })

    expect(el.scrollTop).toBe(1600)
  })
})
