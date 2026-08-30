import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { animate, motionValue } from 'framer-motion'
import { useDrawerSwipe } from '../hooks/useDrawerSwipe'

/** Events carry an explicit timeStamp: velocity is a real branch of the
 *  release decision, and jsdom stamps events created in a loop within the same
 *  millisecond, which would pin every gesture's velocity at 0. */
function touch(type: string, clientX: number, clientY = 0, timeStamp = 0): TouchEvent {
  const t = { clientX, clientY } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  if (type === 'touchend' || type === 'touchcancel') init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  Object.defineProperty(e, 'timeStamp', { value: timeStamp })
  return e
}

describe('useDrawerSwipe', () => {
  let el: HTMLDivElement
  let ref: { current: HTMLDivElement }
  let x: ReturnType<typeof motionValue<number>>
  let onGestureOpen: ReturnType<typeof vi.fn>
  let onSettle: ReturnType<typeof vi.fn>

  /** Viewport width doubles as the gesture's full travel, so closed is -400. */
  const CLOSED = -400
  /** A right-anchored panel runs the same travel with the opposite sign. */
  const CLOSED_RIGHT = 400

  beforeEach(() => {
    el = document.createElement('div')
    document.body.appendChild(el)
    ref = { current: el }
    x = motionValue(0)
    onGestureOpen = vi.fn()
    onSettle = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  function mount(open = false, side: 'left' | 'right' = 'left') {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, side, open, x, onGestureOpen, onSettle,
    }))
  }

  /** Dispatch inside act(): the axis lock flips React state mid-gesture. */
  function fire(target: EventTarget, e: TouchEvent) {
    act(() => { target.dispatchEvent(e) })
  }

  // ── The behaviour the predecessor could not express ──────────────────────
  // useSwipeEdge read displacement once on touchend, so nothing tracked the
  // finger and a reconsidered drag still committed. These three are the point
  // of the rewrite.

  it('moves the panel with the finger instead of waiting for release', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))   // past AXIS_LOCK -> locks, mounts
    expect(x.get()).toBe(CLOSED + 20)
    fire(el, touch('touchmove', 240))
    expect(x.get()).toBe(CLOSED + 200)
  })

  it('mounts the panel at the axis lock, not at touchstart', () => {
    mount()
    fire(el, touch('touchstart', 40))
    expect(onGestureOpen).not.toHaveBeenCalled()
    fire(el, touch('touchmove', 44))   // below AXIS_LOCK — still undecided
    expect(onGestureOpen).not.toHaveBeenCalled()
    fire(el, touch('touchmove', 60))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('cancels when the finger comes back, however far out it went', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 100))   // most of the way open
    fire(el, touch('touchmove', 45, 0, 400))    // ...and back again, slowly
    fire(el, touch('touchend', 45, 0, 500))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('keeps the base it locked with when the mount flips `open` mid-gesture', () => {
    // The real sequence, and the one jsdom will not produce on its own: the
    // opening drag mounts the panel from inside the touchmove handler, the
    // browser flushes that synchronously, and `open` is already true when the
    // same handler reaches the tracking line. Re-reading it there recomputed the
    // base as 0 (an already-open panel), the offset clamped to 0, and the panel
    // appeared at rest — the snap this hook exists to remove. Only the browser
    // harness caught it, so this pins the invariant here too.
    const { rerender } = renderHook(
      ({ open }: { open: boolean }) => useDrawerSwipe(ref, {
        enabled: true, open, x, onGestureOpen, onSettle,
      }),
      { initialProps: { open: false } },
    )
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 60))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)

    // What the synchronous mount does to the hook's view of the world.
    rerender({ open: true })

    fire(el, touch('touchmove', 190))
    expect(x.get()).toBe(CLOSED + 150)   // NOT 0
  })

  // ── Release decision ────────────────────────────────────────────────────

  it('commits open past the halfway point', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 200))
    fire(el, touch('touchend', 300, 0, 400))    // stale sample -> no flick
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('a flick commits open from well short of halfway', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 70, 0, 10))
    fire(el, touch('touchmove', 110, 0, 20))    // 4 px/ms, far above COMMIT_VELOCITY
    fire(el, touch('touchend', 110, 0, 25))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('a hold at the same spot does not inherit the flick that got it there', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 110, 0, 20))    // fast...
    fire(el, touch('touchend', 110, 0, 300))    // ...then held for 280ms
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('closes an open panel on a leftward drag past halfway', async () => {
    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 380, 0, 0))
    fire(el, touch('touchmove', 100, 0, 200))
    expect(x.get()).toBe(-280)
    fire(el, touch('touchend', 100, 0, 400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  // ── What the gesture must NOT claim ─────────────────────────────────────

  it('leaves the platform back-swipe band at the bezel alone', () => {
    mount()
    fire(el, touch('touchstart', 8))   // inside the OS gesture's own strip
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('opens from anywhere in the pane, not just an edge band', () => {
    mount()
    // 300px on a 400px viewport — nowhere near the left edge. The predecessor
    // armed only inside 24-120px, which is why the gesture was hard to find:
    // a drag begun mid-screen, where the thumb naturally lands, did nothing.
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 330))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
    expect(x.get()).toBe(CLOSED + 30)
  })

  it('leaves the platform forward-swipe band at the FAR bezel alone too', () => {
    mount()
    // Within 24px of the right edge, dragging the direction that WOULD open the
    // left drawer: the far bezel only became reachable once the opening band
    // spanned the pane, and the OS owns that strip for its own forward gesture.
    fire(el, touch('touchstart', 390))
    fire(el, touch('touchmove', 430))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('leaves the far bezel alone for the panel anchored THERE as well', () => {
    // The right panel's own opening drag starts near the right edge, which is
    // exactly where the platform's gesture lives — so the dead zone has to hold
    // for the side that most wants to reach past it.
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 390))
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(CLOSED_RIGHT)
  })

  it('a LEFT panel ignores a leftward drag — direction is what selects a panel', () => {
    mount()
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 100))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('yields to a vertical scroll', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 50, 40))   // dy 40 > dx 10
    fire(el, touch('touchmove', 200, 40))  // abandoned — cannot be reclaimed
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('ignores a leftward drag while closed and a rightward one while open', () => {
    const closed = mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 10))
    expect(onGestureOpen).not.toHaveBeenCalled()
    closed.unmount()

    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 320))
    expect(x.get()).toBe(0)
  })

  // ── Giving up a gesture that already owns the panel ─────────────────────
  // A cancelled gesture is not a released one: the release handler never runs,
  // so if abandoning only stopped tracking, the panel would be stranded
  // wherever the finger left it — mounted, half-open, scrim half-dimmed, with
  // no animation coming. Each of these asserts it goes back to where the
  // gesture STARTED.

  it('slides an interrupted opening drag back closed', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 300, 0, 100))   // most of the way open
    expect(x.get()).toBe(CLOSED + 260)
    fire(el, touch('touchcancel', 300, 0, 120))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
  })

  it('slides an interrupted closing drag back open', async () => {
    x.set(0)
    mount(true)
    fire(el, touch('touchstart', 380, 0, 0))
    fire(el, touch('touchmove', 120, 0, 100))
    expect(x.get()).toBe(-260)
    fire(el, touch('touchcancel', 120, 0, 120))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
    expect(x.get()).toBe(0)
  })

  it('treats a second finger mid-drag as an interruption, not a freeze', async () => {
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 100))
    const pinch = new TouchEvent('touchmove', {
      bubbles: true,
      touches: [{ clientX: 200, clientY: 0 } as Touch, { clientX: 250, clientY: 0 } as Touch],
    })
    Object.defineProperty(pinch, 'timeStamp', { value: 120 })
    fire(el, pinch)
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
    // And the gesture is usable again rather than stuck mid-flight.
    fire(el, touch('touchstart', 40, 0, 200))
    fire(el, touch('touchmove', 60, 0, 220))
    expect(onGestureOpen).toHaveBeenCalledTimes(2)
  })

  it('gives the panel up the moment the second finger LANDS, before it moves', async () => {
    // A pinch that holds still emits no further touchmove. Waiting for one left
    // the panel owned and stranded for as long as the fingers rested, so the
    // multi-touch check runs before the phase guard in touchstart.
    mount()
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 100))
    const land = new TouchEvent('touchstart', {
      bubbles: true,
      touches: [{ clientX: 200, clientY: 0 } as Touch, { clientX: 250, clientY: 0 } as Touch],
    })
    Object.defineProperty(land, 'timeStamp', { value: 110 })
    fire(el, land)
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
    expect(x.get()).toBe(CLOSED)
  })

  it('a mid-gesture unbind leaves the next bind able to start a gesture', () => {
    // `phase` is a ref, so it outlives the listener teardown. Left at 'locked'
    // it made every later touchstart bail at the idle guard — the gesture was
    // dead for the rest of the mount, with one stray jump from a stale startX
    // on the way.
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useDrawerSwipe(ref, {
        enabled, open: false, x, onGestureOpen, onSettle,
      }),
      { initialProps: { enabled: true } },
    )
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 200))        // locked, panel owned
    rerender({ enabled: false })             // e.g. crossing out of mobile
    rerender({ enabled: true })              // ...and back
    onGestureOpen.mockClear()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 200))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('takes the value over from an animation the CONSUMER started', async () => {
    // The toggle, the backdrop tap and the session-selected close all animate
    // this same value from outside the hook, and discard the stop handle.
    // `x.set()` does not cancel an animation, so a drag begun inside one of
    // those windows had the drag and the animation both writing every frame.
    // Tracking only the hook's own settles could not see this one.
    mount()
    const programmatic = animate(x, 0, { duration: 0.4 })
    expect(programmatic.time).toBeDefined()   // it is live
    fire(el, touch('touchstart', 40, 0, 0))
    fire(el, touch('touchmove', 200, 0, 40))  // locks -> must seize the value
    const seized = x.get()
    expect(seized).toBe(CLOSED + 160)
    // Let real time pass. If the animation were still running it would drag the
    // value back toward 0 behind the finger's back.
    await new Promise(r => setTimeout(r, 120))
    expect(x.get()).toBe(seized)
  })

  it('binds nothing when disabled', () => {
    renderHook(() => useDrawerSwipe(ref, {
      enabled: false, open: false, x, onGestureOpen, onSettle,
    }))
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('resets on touchcancel', () => {
    mount()
    fire(el, touch('touchstart', 40))
    fire(el, touch('touchcancel', 40))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  // ── The right-anchored panel: same gesture, mirrored ────────────────────
  // Only the SIGNS differ, so these pin the mirror rather than re-testing the
  // machinery: closed sits at +travel, a leftward drag opens, a rightward one
  // closes, and a flick is judged against this side's own opening direction.

  it('opens a RIGHT panel on a leftward drag, tracking the finger', () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300))
    fire(el, touch('touchmove', 280))          // dx -20, past AXIS_LOCK
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
    expect(x.get()).toBe(CLOSED_RIGHT - 20)
    fire(el, touch('touchmove', 100))
    expect(x.get()).toBe(CLOSED_RIGHT - 200)
  })

  it('a RIGHT panel ignores a rightward drag', () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 100))
    fire(el, touch('touchmove', 300))
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(CLOSED_RIGHT)
  })

  it('commits a RIGHT panel past halfway, and never past its own edge', async () => {
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 40, 0, 200))   // dx -260 -> 65% of the travel
    expect(x.get()).toBe(CLOSED_RIGHT - 260)
    fire(el, touch('touchmove', -200, 0, 400)) // dragged well past open
    expect(x.get()).toBe(0)                    // clamped at its rest position
    fire(el, touch('touchend', -200, 0, 600))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('closes an open RIGHT panel on a rightward drag past halfway', async () => {
    x.set(0)
    mount(true, 'right')
    fire(el, touch('touchstart', 20, 0, 0))
    fire(el, touch('touchmove', 300, 0, 200))
    expect(x.get()).toBe(280)
    fire(el, touch('touchend', 300, 0, 400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('reads a flick against the RIGHT panel\'s own opening direction', async () => {
    // Same leftward flick that would be a CLOSE on the left drawer: barely 8%
    // of the travel, so only the velocity branch can commit it.
    x.set(CLOSED_RIGHT)
    mount(false, 'right')
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 270, 0, 10))
    fire(el, touch('touchmove', 268, 0, 12))   // -1 px/ms, past COMMIT_VELOCITY
    fire(el, touch('touchend', 268, 0, 15))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  // ── A panel narrower than the screen ────────────────────────────────────
  // The sessions drawer leaves a strip of chat uncovered, so its travel is its
  // own width. Everything the gesture decides divides by that: leave it at the
  // viewport width and the drag runs past the panel's edge while the commit
  // point sits inboard of the real halfway mark.

  /** Bind with an explicit travel narrower than the 400px viewport. */
  function mountNarrow(open = false) {
    return renderHook(() => useDrawerSwipe(ref, {
      enabled: true, travel: () => 360, open, x, onGestureOpen, onSettle,
    }))
  }

  it('rests closed at its OWN width, not the viewport width', () => {
    mountNarrow()
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 260))          // dx 60 past the axis lock
    expect(x.get()).toBe(-360 + 60)
  })

  it('clamps a drag at the panel edge that travel names', () => {
    mountNarrow()
    fire(el, touch('touchstart', 200))
    fire(el, touch('touchmove', 900))          // far past open
    expect(x.get()).toBe(0)
  })

  it('measures the commit share against the PANEL, not the screen', () => {
    // A fifth of a 360px panel is 72px; a fifth of the 400px screen would be
    // 80px. 76px therefore commits only if the share divides by the travel it
    // was given.
    mountNarrow()
    fire(el, touch('touchstart', 200, 0, 0))
    fire(el, touch('touchmove', 215, 0, 200))
    fire(el, touch('touchmove', 276, 0, 1000))   // dx 76 of 360
    fire(el, touch('touchend', 276, 0, 1400))    // slow: only distance decides
    return waitFor(() => expect(onSettle).toHaveBeenCalledWith(true))
  })

  it('still refuses a release short of that fifth', async () => {
    mountNarrow()
    fire(el, touch('touchstart', 200, 0, 0))
    fire(el, touch('touchmove', 215, 0, 200))
    fire(el, touch('touchmove', 268, 0, 1000))   // dx 68 of 360
    fire(el, touch('touchend', 268, 0, 1400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  it('asks the same fifth of a CLOSING drag, measured from its own start', async () => {
    // The reformulation this pins. Read as an absolute position instead —
    // "commit while the panel is more than a fifth open" — the same 76px pull
    // leaves this panel 79% open and therefore refuses to close, so a light
    // threshold for opening would have become an 80% threshold for closing.
    x.set(0)
    mountNarrow(true)
    fire(el, touch('touchstart', 300, 0, 0))
    fire(el, touch('touchmove', 285, 0, 200))
    fire(el, touch('touchmove', 224, 0, 1000))   // dx -76 of 360
    fire(el, touch('touchend', 224, 0, 1400))
    await waitFor(() => expect(onSettle).toHaveBeenCalledWith(false))
  })

  // ── Horizontal scroller ownership (carried over from useSwipeEdge) ───────
  // A wide code block or a card strip under the finger owns the gesture while
  // it still has somewhere to scroll. Losing this makes every horizontal pan
  // inside a message close or open the drawer.

  function appendScroller(scrollLeft: number, scrollWidth = 900, clientWidth = 300): HTMLDivElement {
    const sc = document.createElement('div')
    sc.style.overflowX = 'auto'
    Object.defineProperty(sc, 'scrollWidth', { configurable: true, value: scrollWidth })
    Object.defineProperty(sc, 'clientWidth', { configurable: true, value: clientWidth })
    Object.defineProperty(sc, 'scrollLeft', { configurable: true, writable: true, value: scrollLeft })
    el.appendChild(sc)
    return sc
  }

  it('does not close over a scroller that can still reveal more', () => {
    const sc = appendScroller(0)
    x.set(0)
    mount(true)
    expect(sc.scrollWidth - sc.clientWidth).toBe(600)
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(0)
  })

  it('does not close when the scroller consumed the gesture', () => {
    const sc = appendScroller(600)
    x.set(0)
    mount(true)
    fire(sc, touch('touchstart', 200))
    sc.scrollLeft = 540
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(0)
  })

  it('closes over a scroller already at its end that did not move', () => {
    const sc = appendScroller(600)
    x.set(0)
    mount(true)
    expect(sc.scrollLeft).toBe(sc.scrollWidth - sc.clientWidth)
    fire(sc, touch('touchstart', 200))
    fire(sc, touch('touchmove', 100))
    expect(x.get()).toBe(-100)
  })

  it('opens from the left band over a scroller already at its start', () => {
    const sc = appendScroller(0)
    mount()
    fire(sc, touch('touchstart', 40))
    fire(sc, touch('touchmove', 200))
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })
})
