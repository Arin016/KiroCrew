import { useEffect, useRef, useState } from 'react'
import { animate, type MotionValue } from 'framer-motion'
import { holdStreamingFlushes, releaseStreamingFlushes } from '../lib/streamHold'

/**
 * Finger-tracking open/close gesture for a mobile edge panel — the sessions
 * drawer on the left, the side panel on the right.
 *
 * Supersedes `useSwipeEdge`, which was a threshold DETECTOR: it read the
 * displacement once on `touchend` and fired a plain callback, so the panel
 * snapped open after the fact with nothing on screen following the finger, and
 * a drag begun and then reconsidered still committed. This hook drives the
 * panel's offset continuously instead, which is what makes a half-drag
 * readable and a drag-back cancellable.
 *
 * ONE binding covers both directions for its own panel. The accept rule depends
 * on whether the panel is currently open, and `open` is deliberately read from a
 * ref rather than a dependency: the opening drag flips it mid-gesture (see
 * `onGestureOpen`), and a dep would tear the listeners down under the finger —
 * leaving the release to land on nothing while the panel stayed half-open.
 *
 * An opening drag may begin ANYWHERE in the bound element except the platform's
 * own two edge bands (`PLATFORM_EDGE`). It is the DIRECTION that says which
 * panel is meant, so a narrow start band would only make the gesture hard to
 * find — which is what it was, at 24-120px of a 390px screen. Deference to
 * content that legitimately owns a horizontal drag is handled where it belongs,
 * by `findHorizontalScroller` below, not by keeping the band small.
 */

/** Travel, in px, before the gesture's axis is decided. */
const AXIS_LOCK = 10
/**
 * Dead zone at each viewport edge where an OPENING drag may NOT begin.
 *
 * Not 0. The platform's own back/forward swipe lives in the first ~20-30px of
 * either edge and is not cancellable from script — `preventDefault` does not
 * reach it — so a gesture armed at the bezel loses a share of its touches to
 * the browser no matter what this code does. Arming inboard of it means the two
 * gestures mostly do not contend for the same touch.
 *
 * Both edges, not just the near one: with two panels bound to the same element
 * the START POSITION no longer says which panel is meant — DIRECTION does (see
 * the lock rule below) — so the only thing position still has to do is stay off
 * the platform's two bands.
 */
const PLATFORM_EDGE = 24
/**
 * Share of the travel the finger must have DRAGGED — from wherever this gesture
 * started — for a release to commit to the other state.
 *
 * Measured as displacement from the gesture's own base, not as the panel's
 * absolute position, and that is what lets one number serve both directions. As
 * an absolute position it could not: a threshold of 0.2 read as "the panel must
 * be more than 20% open" makes an opening drag commit early (wanted) and a
 * closing drag need 80% of the screen before it will let go (not wanted).
 *
 * A fifth of the travel is a deliberately light ask — 70px of a 350px panel. The
 * gesture is already protected from accident by the axis lock, the direction
 * gate and deference to horizontal scrollers, so the remaining job of this
 * number is only to tell a drag apart from a twitch. Half a panel is far more
 * than that takes, and it makes a slow deliberate pull feel like it snapped back
 * for no reason.
 */
const COMMIT_DRAG_SHARE = 0.2
/** Release faster than this (px/ms) commits regardless of how far it got. */
const COMMIT_VELOCITY = 0.4
/**
 * Trailing window the release speed is measured over.
 *
 * Measured across a window rather than from the last pair of samples: a flick's
 * final frame is often coalesced or already decelerating, so an instantaneous
 * reading under-reports the throw. It also needs no hold cliff — `touchmove`
 * only fires when the finger MOVES, so a finger that stopped contributes no
 * sample inside the window and its speed falls out as zero on its own.
 */
const VELOCITY_WINDOW_MS = 100
/** Shortest base the speed may be measured over. A finger that was still for
 *  most of the window and twitched once just before lifting would otherwise
 *  read as a hard throw off two adjacent samples. */
const VELOCITY_MIN_SPAN_MS = 16
/** Samples retained for the window — 100ms at 60Hz is ~6, with headroom for a
 *  higher-rate digitizer and one baseline sample outside the window. */
const VELOCITY_SAMPLES = 16
/**
 * The settle curve, and the two durations — ONE shape, a reveal and a dismissal
 * that differ only in how long they take.
 *
 * A cubic-bezier rather than a spring for two load-bearing reasons: the shape
 * is stated rather than tuned by feel, and the compositor path below hands the
 * curve to a `KeyframeEffect`, which a spring (no closed form) cannot express
 * without sampling itself on the main thread every frame — the exact thing the
 * compositor path exists to avoid.
 *
 * The shape is iOS's sheet presentation curve: `(0.32, 0.72, 0, 1)`. That is not
 * invented here — it is the curve Ionic ships for its iOS transitions and the one
 * Vaul uses (a drawer library written to reproduce Apple's Sheet, whose author
 * states the curve and 500ms as the iOS match). It is also already this repo's
 * own drawer curve: `components/OverlayDrawer.tsx` has carried
 * `EASE = [0.32, 0.72, 0, 1]` all along, with the reason spelled out there —
 * "near-linear on purpose: a strong ease-out front-loads the travel, which
 * visually freezes the near edges while the far edges are still sweeping."
 *
 * That is the constraint to respect. Three progressively more front-loaded
 * easeOut shapes were tried on a device and each was rejected for reading as the
 * panel appearing rather than sliding: easeOutExpo `(0.19, 1, 0.22, 1)` at 340ms
 * (26% of the travel gone in the first painted frame), easeOutQuint
 * `(0.16, 1, 0.3, 1)` at 320ms (30%), and `(0.1, 0.9, 0.2, 1)` at 320ms (39%).
 * This one spends 10%. The FIRST FRAME is the measurement that tracks the
 * complaint, which is why the guard is stated that way.
 *
 * 420ms rather than the iOS 500ms for the reveal: the shape is what was being
 * judged, and the shorter tail reads better on a side drawer than on a bottom
 * sheet. Either is affordable only because the settle reaches the compositor — a
 * longer tween used to mean more frames exposed to main-thread stalls, so length
 * was a risk to be minimized. It no longer is, and the duration can be chosen
 * for how it reads.
 *
 * The DISMISSAL is the SAME curve, simply shorter — 400ms. Getting here took one
 * wrong turn worth recording, because the obvious reading of the symptom points
 * at the curve and the cause was elsewhere. This shape at 240ms was reported as
 * a snap ("gone in a flash"), and softening the exit to a gentler decelerating
 * curve did quiet it — but the real fault was that the drawer's travel was the
 * VIEWPORT width while the panel is narrower than that, so it left the screen at
 * ~90% of the slide and the deceleration played with nothing on it. With the
 * travel matched to the panel's own width (`DRAWER_UNCOVERED_PX` in ChatPage),
 * this curve stops snapping and the gentler one turns out to be the thing that
 * feels wrong: it decelerates 18x across its span where this one does ~100x, and
 * that contrast between a quick departure and a long glide into the edge IS the
 * "final deceleration" a dismissal is judged on.
 *
 * So: ONE curve everywhere, and only the DURATION varies — 420ms to disclose,
 * the slowest a release can be to dismiss, and a finger-released settle deriving
 * its own from the speed the user threw it at (`gestureSettle`). The first-frame
 * budget below is the reveal's, and the exit is held to the same one; what
 * protects the exit from snapping is the travel matching the width, not a softer
 * curve.
 */
const SETTLE_EASE = [0.32, 0.72, 0, 1] as const
/** Reveal: the panel is disclosing something, so it takes its time. */
const SETTLE_IN_SECS = 0.42

/**
 * The band a finger-released settle may land in.
 *
 * Deliberately WIDER than the tap durations, and shared by both directions.
 * Capping the slow end at the tap duration (0.24s for a dismissal) left a 2.2x
 * band that an ordinary gesture never crossed: with most of a drawer's width
 * still to travel, the derived duration only drops under 0.24s above ~2.7 px/ms,
 * so a gentle drag and a firm throw both landed on the same cap and the throw
 * strength was invisible — the very thing this derivation exists to show.
 *
 * Across a full-width drawer the band is crossed between roughly 1.7 and 6.5
 * px/ms, which is the range a real flick lives in.
 */
const GESTURE_SETTLE_MIN_SECS = 0.12
const GESTURE_SETTLE_MAX_SECS = 0.45

/**
 * Dismissal from a tap: the slowest a RELEASE can be, by definition rather than
 * by coincidence.
 *
 * A tap carries no velocity, so there is no finger to match — and of the speeds
 * a finger could have had, the gentlest is the one whose glide into the edge
 * reads best. Judged side by side on a device: the same curve at 300ms and 400ms
 * both still felt hurried against a slow swipe, which lands on this ceiling.
 *
 * Derived, not a second 0.45: the two are the same quantity, and spelling it
 * twice is how a later tuning of the band would silently leave the tap behind.
 * Consequence worth knowing — this makes a dismissal LONGER than the 420ms
 * reveal, so the "nothing is being disclosed, so it is shorter" rule no longer
 * holds; what governs now is that both are the slowest reading of their own
 * event.
 */
const SETTLE_OUT_SECS = GESTURE_SETTLE_MAX_SECS

/**
 * The travel those two durations were judged on: the mobile sessions drawer.
 *
 * They are a SPEED, not a time. A duration that is right for a panel crossing
 * most of the screen makes a narrow one crawl: the nav drawer travels 231px
 * against the drawer's 350px, so the same 450ms moved it 1.46x slower per pixel
 * and read exactly as "it only goes a short way, why is it taking so long". So a
 * tap settle scales its duration by the distance it actually has to cover, and
 * this is the reference the scaling is anchored to — the drawer itself comes out
 * unchanged at 420/450ms, which is what those numbers were tuned as.
 */
const SETTLE_REFERENCE_TRAVEL_PX = 350

/**
 * The curve a TAP dismissal leaves on — same family, but it launches instead of
 * jumping.
 *
 * A settle that starts from REST and a settle that continues a MOVING finger are
 * different events, and the difference shows up in the first painted frame. The
 * reveal's curve opens at 2.25x its average speed, which at 450ms over 350px
 * puts 31.6px on screen in one frame. For a released gesture that is correct —
 * the panel was ALREADY doing that speed, so the frame is continuous. For a tap
 * it is a jump from a standstill, and read as one.
 *
 * The same 450ms on `(0.3, 0.3, 0, 1)` opens at 1.0x its average — a launch, not
 * a jump — and spends 12% of the travel where the other spent 31%: 15.3px in the
 * first frame. It still decelerates hard (`x2, y2` are the reveal's), holding 90%
 * of the travel behind it for the last 230ms.
 *
 * Note what is NOT shared here: the entry keeps the sharp curve on purpose. A
 * reveal's first frame happens at the screen edge, where the jump is mostly
 * off-screen, and `gestureSettle` needs this curve's slope as the anchor its
 * velocity match is derived from.
 *
 * An earlier attempt at this softened the whole curve rather than just its
 * launch — `(0.4, 0.5, 0.2, 1)`, which cut the first frame to 17px but also cut
 * the glide from 285ms to 208ms and read as merely stopping. Softening the
 * LAUNCH and keeping the tail is the distinction that took three passes to find.
 */
const SETTLE_OUT_EASE = [0.3, 0.3, 0, 1] as const

/** The four control-point coordinates of a cubic-bezier. Kept as a TUPLE, not
 *  `number[]` — framer's `Easing` accepts only the fixed-arity form, so widening
 *  it here surfaces as a type error at the `animate` call. */
type Bezier = readonly [number, number, number, number]

/**
 * Timing for a settle a TAP started, over `distance` px.
 *
 * The two durations are held as a speed: each is scaled by how far this settle
 * actually has to go, against the reference travel they were judged on. Bounded
 * above at the reference duration, so a panel WIDER than the reference (the
 * full-width right overlay) stays at the slowest reading rather than exceeding
 * it, and below at the same floor a released gesture uses.
 *
 * `distance` is optional only so a caller with nothing to measure still gets the
 * reference timing; every real path has it.
 */
function settleFor(to: number, distance?: number): { ease: Bezier; secs: number } {
  const ease = to === 0 ? SETTLE_EASE : SETTLE_OUT_EASE
  const atReference = to === 0 ? SETTLE_IN_SECS : SETTLE_OUT_SECS
  if (distance == null) return { ease, secs: atReference }
  const scaled = atReference * (distance / SETTLE_REFERENCE_TRAVEL_PX)
  return { ease, secs: Math.min(atReference, Math.max(GESTURE_SETTLE_MIN_SECS, scaled)) }
}

/**
 * The band a finger-released settle may land in.
 *
 * Declared above, beside the tap durations, because `SETTLE_OUT_SECS` is derived
 * from this ceiling.
 */

let safeLeftPx: number | null = null
let safeLeftBound = false

/**
 * `env(safe-area-inset-left)` in px.
 *
 * A panel pinned at `left-safe` does not start at the screen edge — on a notched
 * phone held in landscape it starts an inset in, so what it takes to CLEAR the
 * screen is its own width PLUS that inset. Leave the inset out and the panel
 * parks with an inset-wide strip of itself still on screen: the same defect as a
 * travel that undershoots the width, reachable only in landscape.
 *
 * Measured through a throwaway probe rather than guessed, because `env()` is not
 * readable as a custom property — the probe resolves the very expression the
 * panels' own CSS uses. Memoized, and invalidated on the events that can change
 * it: a per-call probe would append and remove a node on every frame of a drag,
 * since the scrim's binding reads the travel per frame.
 */
export function safeAreaLeft(): number {
  if (typeof document === 'undefined') return 0
  if (!safeLeftBound) {
    safeLeftBound = true
    const invalidate = () => { safeLeftPx = null }
    addEventListener('resize', invalidate)
    addEventListener('orientationchange', invalidate)
  }
  if (safeLeftPx != null) return safeLeftPx
  const probe = document.createElement('div')
  // Set per property rather than through one `cssText` declaration blob: a blob is a
  // quoted sentence-shaped literal, and the i18n gate reads those as untranslated copy
  // in a file it cannot exempt wholesale (this hook is behaviour, not a stylesheet).
  const s = probe.style
  s.position = 'fixed'
  s.top = '0'
  s.left = 'env(safe-area-inset-left)'
  s.width = '0'
  s.height = '0'
  s.visibility = 'hidden'
  document.body.appendChild(probe)
  const left = probe.getBoundingClientRect().left
  probe.remove()
  safeLeftPx = Number.isFinite(left) ? Math.max(0, left) : 0
  return safeLeftPx
}

/** A cubic-bezier's opening speed as a multiple of its average — `dy/dx` at
 *  `t=0`, which for `(x1,y1,x2,y2)` is `y1/x1`. */
function initialSlope(e: Bezier): number {
  return e[0] === 0 ? Infinity : e[1] / e[0]
}

/**
 * Timing for a settle the FINGER released, rather than one a tap started.
 *
 * Two things differ from `settleFor`, and both follow from the same fact: the
 * panel is ALREADY MOVING, at a speed the user chose.
 *
 * The CURVE decelerates in both directions. The exit's own easeIn (slow, then
 * fast) is right for a tap, which starts from rest — but after a flick it makes
 * the panel visibly stall and then race away, so the harder it is thrown the
 * less it follows the finger.
 *
 * The DURATION is derived so the first painted frame continues at the finger's
 * own speed: a cubic-bezier opens at `initialSlope` times its average speed, so
 * requiring that opening speed to equal `velocity` over the remaining
 * `distance` fixes the duration. A harder flick therefore lands SOONER — which
 * a fixed duration cannot express at all, since it makes every release take the
 * same time however hard it was thrown.
 *
 * A release carrying NO speed is the one case with nothing to continue: a
 * hold-then-lift starts from rest just as a tap does, so it takes the tap's own
 * curve and duration for that direction.
 */
function gestureSettle(to: number, from: number, velocity: number): { ease: Bezier; secs: number } {
  const distance = Math.abs(to - from)
  // Only the component heading TOWARD the target carries continuity. A release
  // committed on DISTANCE while the finger was already moving back the other way
  // has no momentum to continue, and taking its magnitude would shorten the
  // settle on the strength of a flick pointing the opposite way.
  const speed = distance > 0 ? velocity * Math.sign(to - from) : 0
  if (speed <= 0) return settleFor(to, distance)
  const matched = distance * initialSlope(SETTLE_EASE) / speed / 1000
  return {
    ease: SETTLE_EASE,
    secs: Math.min(GESTURE_SETTLE_MAX_SECS, Math.max(GESTURE_SETTLE_MIN_SECS, matched)),
  }
}

/** A settle in the spelling a `KeyframeEffect` takes — derived from the SAME
 *  resolved shape the main-thread tween uses, so the compositor and main-thread
 *  paths cannot drift apart. */
function settleTiming({ ease, secs }: { ease: Bezier; secs: number }): KeyframeAnimationOptions {
  return { duration: secs * 1000, easing: `cubic-bezier(${ease.join(', ')})`, fill: 'forwards' }
}
/** Reduced motion still needs the panel to ARRIVE — dragging is direct
 *  manipulation, and dropping the settle to 0 would teleport the panel out from
 *  under the finger. A short linear tween carries it there without the curve. */
const DRAWER_SETTLE_REDUCED = { duration: 0.12, ease: 'linear' as const }

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * The DOM the settle animates, when it can reach the compositor.
 *
 * `travel` is a function, not a number: the closed offset is the viewport width
 * and a rotation changes it between a register and a settle.
 */
export interface DrawerTargets {
  panel: () => HTMLElement | null
  scrim: () => HTMLElement | null
  travel: () => number
}

interface DrawerRuntime {
  targets: DrawerTargets
  /** Compositor animations currently carrying the panel, so a new gesture can
   *  take them over instead of writing the same channel underneath them. */
  running: Animation[]
}

/**
 * Per-MotionValue runtime, keyed weakly so a torn-down ChatPage takes its entry
 * with it. A WeakMap rather than a hook argument because `animateDrawer` is
 * called from four places that have no business knowing about DOM nodes (the
 * header toggle, the scrim tap, the session-selected close, and this hook's own
 * release), and threading the elements through all four would put the same
 * lookup in each of them.
 */
const runtimes = new WeakMap<MotionValue<number>, DrawerRuntime>()

/**
 * Point `x`'s settles at real DOM. Returns a deregister for unmount.
 *
 * PRECONDITION, and the reason the first compositor attempt shipped a visible
 * bug: nothing inside the registered panel may be a Framer layout-projection
 * node (`layout` / `layoutId`). Projection only stays correct while framer owns
 * every animated ancestor transform; under a WAAPI-driven ancestor it attributes
 * the panel's travel to the descendants themselves and compounds a corrective
 * transform per re-measure (measured: >4,000px — the sidebar rows visibly flew
 * in from the panel's right edge). ChatSidebar therefore renders its rows
 * WITHOUT projection when hosted in this drawer (`staticRows`), and that pairing
 * is what makes this registration safe — see ChatSidebar.staticRows.test.tsx.
 */
export function registerDrawerTargets(x: MotionValue<number>, targets: DrawerTargets): () => void {
  runtimes.set(x, { targets, running: [] })
  return () => runtimes.delete(x)
}

/** Scrim opacity for a panel offset: 0 fully closed, 1 at rest. Direction-
 *  agnostic — a LEFT drawer runs offset in [-travel, 0], a RIGHT drawer in
 *  [0, +travel], and |offset| is the distance from rest either way. */
function scrimOpacity(offset: number, travel: number): number {
  if (travel <= 0) return 1
  return Math.max(0, Math.min(1, 1 - Math.abs(offset) / travel))
}

/** Move `x` with NO animation and no inherited velocity, cancelling anything
 *  running on it. `set` alone does not cancel — see the note in `settle` — so
 *  `jump` is the correct verb here wherever the build provides it. */
function jumpTo(x: MotionValue<number>, to: number): void {
  if (typeof x.jump === 'function') x.jump(to)
  else x.set(to)
}

/** Current translateX of an element, read from its resolved matrix — including
 *  the value a running compositor animation is presenting this instant. */
function currentTranslateX(el: HTMLElement): number | null {
  if (typeof getComputedStyle !== 'function') return null
  const t = getComputedStyle(el).transform
  if (!t || t === 'none') return null
  const nums = t.slice(t.indexOf('(') + 1, -1).split(',').map(v => parseFloat(v))
  if (t.startsWith('matrix3d')) return Number.isFinite(nums[12]) ? nums[12] : null
  return Number.isFinite(nums[4]) ? nums[4] : null
}

/**
 * Hand `x` back the offset a compositor settle is presenting, and cancel it.
 *
 * Called wherever plain `x.stop()` used to go: a compositor animation is NOT an
 * animation ON the value, so `stop`/`jump` do not reach it — left running, it
 * would keep presenting its own offset over every write the finger makes, and
 * the panel would refuse to follow until the animation ran out.
 */
export function takeOverDrawer(x: MotionValue<number>): void {
  const rt = runtimes.get(x)
  if (rt && rt.running.length > 0) {
    const panel = rt.targets.panel()
    const at = panel ? currentTranslateX(panel) : null
    for (const a of rt.running) a.cancel()
    rt.running = []
    // Adopt the presented offset BEFORE anything repaints — into the element's
    // OWN inline style as well as the value, for the same reason publishArrival
    // does: cancel reverts to inline style, and only framer-bound panels get
    // theirs rewritten by jumpTo.
    if (at != null && panel) {
      panel.style.transform = `translate3d(${at}px, 0, 0)`
      const scrim = rt.targets.scrim()
      if (scrim) scrim.style.opacity = String(scrimOpacity(at, rt.targets.travel()))
      jumpTo(x, at)
    }
  }
  x.stop()
}

/** Animate a drawer offset to its resting place with the shared settle curve.
 *  Exported so the tap/backdrop/programmatic paths land identically to a
 *  released gesture — two curves for one panel is how the two paths visibly
 *  drift apart.
 *
 *  Runs on the COMPOSITOR when `registerDrawerTargets` has supplied elements:
 *  `transform` and `opacity` keyframes on the panel and the scrim, which the
 *  browser hands to the compositor thread. That is the point — the panel and
 *  the chat pane behind it share one main thread, and with sessions streaming
 *  that thread stalls unpredictably (chunk flushes are held, see
 *  lib/streamHold.ts, but tool events, subagent status pushes and the panel's
 *  own mount are not), so ONLY an animation that does not need the main thread
 *  holds its frame rate through the slide. Falls back to the main-thread tween
 *  when no element is registered (embed frames, tests) or under reduced motion.
 *
 *  The streaming-flush hold is kept on both paths: it starves the projection
 *  re-measures and the transcript repaints of their per-frame trigger, which
 *  is cheap insurance either way. */
export function animateDrawer(x: MotionValue<number>, to: number, onDone?: () => void, releaseVelocity?: number) {
  const reduce = prefersReducedMotion()
  const rt = runtimes.get(x)
  /** Resolved lazily, and once per path, because both branches measure from
   *  wherever the offset stands AT THAT MOMENT — the distance still to travel
   *  sets the tap duration, and on the compositor path that is only settled
   *  after `takeOverDrawer`. */
  const shape = () => {
    const from = x.get()
    return releaseVelocity == null
      ? settleFor(to, Math.abs(to - from))
      : gestureSettle(to, from, releaseVelocity)
  }

  const mainThread = () => {
    const eased = shape()
    const curve = reduce ? DRAWER_SETTLE_REDUCED : { duration: eased.secs, ease: eased.ease }
    holdStreamingFlushes(curve.duration * 1000 + 100)
    /**
     * Paint the tween onto the ELEMENTS' OWN inline styles, not just `x`.
     *
     * Framer writes a transform only where it is BOUND to `x` (the two panels
     * with a drag gesture — the sessions drawer and the right overlay — carry
     * `style={{ x }}`, because a drag writes the value directly and only a live
     * binding paints those frames). The nav drawer is a plain <nav>, so on this
     * path its MotionValue would travel while its DOM never moved — the panel
     * stays at the mounted CLOSED offset and is simply unreachable. Having no
     * gesture, it is also the one for which this fallback is the ONLY path
     * whenever the compositor one is unavailable: under
     * `prefers-reduced-motion`, and when the panel element has not appeared
     * within the mount grace frames. Same root cause as the cancel-fill bounce
     * (a non-framer-bound element needs the write spelled out), on the fallback
     * path instead of the compositor one. Redundant where framer IS bound — it
     * writes the same value from its own subscription.
     */
    const paint = rt
      ? (at: number) => {
        const panel = rt.targets.panel()
        if (panel) panel.style.transform = `translate3d(${at}px, 0, 0)`
        const scrim = rt.targets.scrim()
        if (scrim) scrim.style.opacity = String(scrimOpacity(at, rt.targets.travel()))
      }
      : null
    const unbind = paint ? x.on('change', paint) : null
    paint?.(x.get())
    const controls = animate(x, to, {
      ...curve,
      onComplete: () => { paint?.(to); unbind?.(); releaseStreamingFlushes(); onDone?.() },
    })
    return () => { unbind?.(); controls.stop() }
  }
  if (!rt || reduce) return mainThread()

  let raf: number | null = null
  let cancelled = false
  let stopMain: (() => void) | null = null
  /** Frames to wait for the panel to exist. The tap-open path calls this in the
   *  same tick as the setState that MOUNTS the panel, so on that path there is
   *  nothing to animate yet — and waiting is correct rather than merely
   *  tolerable, because the first painted frame is supposed to be the closed
   *  offset anyway. Bounded so a panel that never arrives degrades to the
   *  main-thread path instead of silently never settling. */
  let tries = 3

  const start = () => {
    if (cancelled) return
    raf = null
    /**
     * Adopt whatever the PREVIOUS settle is presenting, and cancel it, before
     * keyframing a replacement. Reversing mid-settle (close then re-open inside
     * the 420ms) otherwise breaks twice over, and nothing else cleans up: no
     * caller keeps this function's returned canceller.
     *
     * `x` does not move while the compositor owns the offset, so on a reversal
     * `from` would still read the offset the OUTGOING settle started from — a
     * close→open reversal keyframes 0→0 and snaps instead of sliding. And the
     * outgoing animation is `fill: 'forwards'`: once the replacement is
     * cancelled on arrival, that stale fill re-presents its own end state OVER
     * the published inline style, so the panel lands open and then goes back
     * offscreen while its phase still says `open` — unreachable.
     */
    takeOverDrawer(x)
    const panel = rt.targets.panel()
    if (!panel || typeof panel.animate !== 'function') {
      if (panel || tries-- <= 0) { stopMain = mainThread(); return }
      raf = requestAnimationFrame(start)
      return
    }
    // `from` first: a finger-released settle's duration is derived from the
    // distance still to travel, so the shape cannot be resolved before the
    // offset it starts from is known.
    const from = x.get()
    const timing = settleTiming(shape())
    holdStreamingFlushes(Number(timing.duration) + 100)
    const travel = rt.targets.travel()
    const anims: Animation[] = [panel.animate(
      [{ transform: `translate3d(${from}px, 0, 0)` }, { transform: `translate3d(${to}px, 0, 0)` }],
      timing,
    )]
    const scrim = rt.targets.scrim()
    // The scrim's opacity is derived from the same offset, so it has to be
    // animated in lockstep here: its own binding reads `x`, which does not move
    // while the compositor owns the panel.
    if (scrim && typeof scrim.animate === 'function') {
      anims.push(scrim.animate(
        [{ opacity: scrimOpacity(from, travel) }, { opacity: scrimOpacity(to, travel) }],
        timing,
      ))
    }
    rt.running = anims
    /**
     * Publish the arrival into the ELEMENTS' OWN inline styles FIRST, then the
     * MotionValue, then cancel. Cancelling a `fill: 'forwards'` animation
     * reverts each element to its inline style — and `jumpTo(x)` rewrites that
     * style only where framer is BOUND to `x` (the drag-driven panels'
     * `style={{ x }}`). The nav drawer is a plain <nav>, so on it the inline
     * style is whatever its last React render serialized — the CLOSED offset,
     * for a panel that mounted closed. Cancelling without this write snapped the
     * just-arrived panel offscreen until the next unrelated re-render popped it
     * back: the recorded open→vanish→flash-open bounce.
     */
    const publishArrival = () => {
      panel.style.transform = `translate3d(${to}px, 0, 0)`
      if (scrim instanceof HTMLElement) scrim.style.opacity = String(scrimOpacity(to, travel))
      jumpTo(x, to)
    }
    const settled = () => {
      if (rt.running !== anims) return // taken over by a newer gesture
      publishArrival()
      for (const a of anims) a.cancel()
      rt.running = []
      releaseStreamingFlushes()
      onDone?.()
    }
    anims[0].onfinish = settled
    // A compositor animation on a hidden/backgrounded element can be cancelled
    // by the browser rather than finished; the panel must still arrive.
    anims[0].oncancel = () => { if (rt.running === anims) { rt.running = []; publishArrival(); releaseStreamingFlushes(); onDone?.() } }
  }

  start()
  return () => {
    cancelled = true
    if (raf != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(raf)
    stopMain?.()
    for (const a of rt.running) a.cancel()
    rt.running = []
  }
}

/**
 * Nearest ancestor of `from`, up to and including `root`, that scrolls
 * horizontally. Returns null when the touch did not start inside one.
 */
function findHorizontalScroller(from: EventTarget | null, root: HTMLElement): HTMLElement | null {
  let node: Element | null = from instanceof Element ? from : null
  while (node) {
    if (node instanceof HTMLElement && node.scrollWidth - node.clientWidth > 1) {
      const overflowX = getComputedStyle(node).overflowX
      if (overflowX === 'auto' || overflowX === 'scroll') return node
    }
    if (node === root) break
    node = node.parentElement
  }
  return null
}

interface DrawerSwipeOptions {
  /** Bind the gesture at all. Mobile-only — a pointer device has the toggle. */
  enabled: boolean
  /**
   * Which edge the panel is anchored to. Decides the sign of everything: the
   * closed offset, which drag direction opens, and which closes.
   *
   * Two instances may share one element — one per side — because DIRECTION,
   * not start position, selects the panel: on a closed panel each instance
   * rejects the direction that is not its own, so a single touch can only ever
   * lock one of them. What they must NOT do is both be enabled while one panel
   * is OPEN, because then the closing drag for the open panel is also the
   * opening drag for the other one. The consumer settles that by disabling the
   * far side whenever a panel is open.
   */
  side?: 'left' | 'right'
  /**
   * The offset span between closed and open, in px. Defaults to the viewport
   * width.
   *
   * A panel NARROWER than the screen must pass its own width. Everything the
   * gesture decides is measured against this: the clamp that stops a drag at the
   * panel's own edges, the halfway point a release commits on, and the distance
   * the release-speed derivation divides by. Left at the viewport width for a
   * narrower panel, the drag can push it past its own edge and the commit point
   * sits inboard of the real halfway mark.
   */
  travel?: () => number
  /** Whether the panel is currently open. Read live, never a dependency. */
  open: boolean
  /** Panel offset, in px: `0` fully open, `∓travel` fully closed (negative for
   *  a left panel, positive for a right one). */
  x: MotionValue<number>
  /** Put the panel in the DOM at its closed offset so the drag can reveal it. */
  onGestureOpen: () => void
  /** Released. `open` is the state the panel settled into, reported only once
   *  the settle animation has finished so an unmount cannot cut it short. */
  onSettle: (open: boolean) => void
}

/** @returns whether a drag currently owns the panel (suppress transitions). */
export function useDrawerSwipe(
  ref: React.RefObject<HTMLElement | null>,
  { enabled, side = 'left', travel, open, x, onGestureOpen, onSettle }: DrawerSwipeOptions,
): boolean {
  const [dragging, setDragging] = useState(false)

  // Everything the move handler reads lives in a ref. A touchmove fires at
  // frame rate and this hook is bound inside the chat pane, so a re-render per
  // sample would drop frames on the very gesture it is meant to smooth.
  const openRef = useRef(open)
  openRef.current = open
  const onGestureOpenRef = useRef(onGestureOpen)
  onGestureOpenRef.current = onGestureOpen
  const onSettleRef = useRef(onSettle)
  onSettleRef.current = onSettle
  const travelRef = useRef(travel)
  travelRef.current = travel

  const phase = useRef<'idle' | 'pending' | 'locked'>('idle')
  const startX = useRef(0)
  const startY = useRef(0)
  const travelPx = useRef(0)
  /**
   * Offset the panel sat at when this gesture locked, and the base every later
   * sample is measured from.
   *
   * Latched, NOT re-read from `open` per move — and that is load-bearing. An
   * opening drag mounts the panel from inside the touchmove handler, React
   * flushes that synchronously, and `open` is therefore already true by the time
   * the SAME handler reaches the tracking line. Reading it there computed the
   * base for an already-open panel (0) instead of the closed one, so the offset
   * clamped to 0 and the panel appeared instantly at rest: a snap, i.e. exactly
   * the behaviour this hook replaces. The base belongs to the gesture, so it is
   * decided once, when the gesture is decided.
   */
  const gestureBase = useRef(0)
  const lastX = useRef(0)
  const lastT = useRef(0)
  /**
   * Trailing (x, t) samples of this gesture, oldest first, for the release
   * speed. A buffer rather than a running instantaneous value because the speed
   * that matters is the one over the last `VELOCITY_WINDOW_MS`, and that cannot
   * be recovered from a single pair of samples after the fact.
   */
  const samples = useRef<{ x: number; t: number }[]>([])
  const scroller = useRef<HTMLElement | null>(null)
  const scrollerLeft = useRef(0)

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    /** The gesture's full travel — the panel's own width when the consumer
     *  supplies it, the viewport otherwise. */
    const span = () => travelRef.current?.() ?? window.innerWidth
    /** Offset the panel rests at while closed. Signed by the side it is anchored
     *  to, and spanning `span()` rather than the screen. */
    const closedOffset = () => side === 'left' ? -span() : span()
    /** +1 when a RIGHTWARD drag opens this panel, -1 when a leftward one does. */
    const openDir = side === 'left' ? 1 : -1
    /** Hold an offset inside the panel's own range, so dragging past open cannot
     *  lift it off its edge and dragging past closed cannot gap it further. */
    const clampOffset = (v: number) => {
      const closed = closedOffset()
      return Math.min(Math.max(v, Math.min(0, closed)), Math.max(0, closed))
    }

    /**
     * Take `x` over, then run the shared settle to `to`, reporting `open` once
     * it arrives.
     *
     * `x.stop()` rather than a stop handle this hook kept for its OWN
     * animations. Two writers to one value is the failure mode, and the other
     * writer is not always this hook: the consumer animates the same value
     * programmatically for the header toggle, the backdrop tap and the
     * session-selected close, and discards those handles. `x.set()` does NOT
     * cancel an animation running on the value — only `stop`/`jump` do — so a
     * drag begun inside one of those ~0.32s windows had the drag and the
     * animation both writing every frame, and the panel juddered until the
     * animation ran out. Stopping the VALUE covers every writer, including ones
     * added later, which no amount of handle-tracking here can.
     *
     * Stopping also suppresses the stopped animation's `onComplete`, so a
     * close that a new gesture interrupts cannot later report `closed` over the
     * gesture's own outcome.
     */
    const settle = (to: number, open: boolean, releaseVelocity?: number) => {
      takeOverDrawer(x)
      animateDrawer(x, to, () => onSettleRef.current(open), releaseVelocity)
    }

    /** Drop a gesture that had not taken the panel over yet (still deciding its
     *  axis). Nothing was mounted and `x` was never written, so there is no
     *  visual state to put back. */
    const reset = () => {
      phase.current = 'idle'
      scroller.current = null
      setDragging(false)
    }

    /**
     * Give up a gesture that ALREADY owns the panel — a second finger, a
     * `touchcancel` from a system interruption, an incoming call.
     *
     * It is not enough to stop tracking. The panel is sitting wherever the
     * finger left it, and for an opening drag it is also MOUNTED, so merely
     * going idle strands it half-open with the scrim half-dimmed and no
     * animation coming: the release handler is the only other place that ever
     * settles it, and it never runs for a gesture that was cancelled rather
     * than released. So return the panel to the state the gesture STARTED from —
     * which is exactly `gestureBase`, 0 when it was open and the closed offset
     * when it was not — and report that state so the mount follows it.
     */
    const abandon = () => {
      if (phase.current === 'locked') settle(gestureBase.current, gestureBase.current === 0)
      reset()
    }

    /** Record one position sample, dropping anything older than the window
     *  needs (one baseline sample beyond it is kept, for the min-span widen). */
    const sample = (x: number, t: number) => {
      const buf = samples.current
      buf.push({ x, t })
      if (buf.length > VELOCITY_SAMPLES) buf.splice(0, buf.length - VELOCITY_SAMPLES)
    }

    /**
     * Finger speed at release, px/ms, signed — measured across the trailing
     * window rather than off the final pair of samples.
     *
     * A finger that stopped moving emits no `touchmove`, so it leaves no sample
     * inside the window and reads as 0 here. That replaces a hard "was the
     * release within one frame of the last move" cliff, which discarded the
     * whole speed of any flick whose lift landed a frame late — making a hard
     * throw settle in exactly the same time as a gentle drag.
     */
    const releaseSpeed = (x: number, t: number): number => {
      const buf = samples.current
      let base: { x: number; t: number } | null = null
      for (let i = 0; i < buf.length; i++) {
        if (t - buf[i].t <= VELOCITY_WINDOW_MS) {
          base = buf[i]
          // Too narrow a base reads a twitch as a throw: widen to the sample
          // just outside the window when there is one.
          if (t - base.t < VELOCITY_MIN_SPAN_MS && i > 0) base = buf[i - 1]
          break
        }
      }
      if (!base) return 0
      const span = t - base.t
      return span > 0 ? (x - base.x) / span : 0
    }

    const onTouchStart = (e: TouchEvent) => {
      // A second finger LANDING is a pinch or a two-finger scroll, not this
      // gesture — abandon on the spot. Checked before the phase guard on
      // purpose: a locked gesture would otherwise return here and only give the
      // panel up on the next touchmove, so a pinch that holds still would keep
      // it owned and stranded indefinitely.
      if (e.touches.length > 1) { abandon(); return }
      if (phase.current !== 'idle') return
      const touch = e.touches[0]
      if (!openRef.current) {
        const x0 = touch.clientX
        // Anywhere but the platform's own two edge bands. Which panel this
        // touch is for is decided by DIRECTION at lock time, not by where it
        // began, so a start position inside the pane carries no other meaning.
        if (x0 < PLATFORM_EDGE || x0 > window.innerWidth - PLATFORM_EDGE) return
      }
      travelPx.current = span()
      startX.current = touch.clientX
      startY.current = touch.clientY
      lastX.current = touch.clientX
      lastT.current = e.timeStamp
      samples.current = [{ x: touch.clientX, t: e.timeStamp }]
      phase.current = 'pending'
      scroller.current = findHorizontalScroller(e.target, el)
      scrollerLeft.current = scroller.current ? scroller.current.scrollLeft : 0
    }

    const onTouchMove = (e: TouchEvent) => {
      if (phase.current === 'idle') return
      if (e.touches.length > 1) { abandon(); return }
      const touch = e.touches[0]
      const dx = touch.clientX - startX.current
      const dy = touch.clientY - startY.current

      if (phase.current === 'pending') {
        // Vertical intent: the chat scroller owns this touch. Abandon outright
        // rather than staying armed, so a later horizontal wobble during a
        // scroll cannot retroactively claim the gesture.
        if (Math.abs(dy) > Math.abs(dx)) { reset(); return }
        if (Math.abs(dx) < AXIS_LOCK) return
        // A horizontal scroller under the finger (a wide code block, a
        // carousel) owns the gesture while it still has somewhere to go in
        // this direction. Checked at lock time, on the direction now known.
        const sc = scroller.current
        if (sc) {
          if (sc.scrollLeft !== scrollerLeft.current) { reset(); return }
          const maxScrollLeft = sc.scrollWidth - sc.clientWidth
          const canReveal = dx < 0 ? sc.scrollLeft < maxScrollLeft - 1 : sc.scrollLeft > 1
          if (canReveal) { reset(); return }
        }
        // Wrong direction for the current state: a closed panel only opens on a
        // drag AWAY from its own edge, an open one only closes on a drag back
        // toward it. This is also what lets a left and a right instance share
        // one element — each rejects the other's opening direction.
        if (openRef.current ? dx * openDir > 0 : dx * openDir < 0) { reset(); return }
        phase.current = 'locked'
        gestureBase.current = openRef.current ? 0 : closedOffset()
        // Take the value over from ANY animation still running on it — this
        // hook's own settle, or one the consumer started for the toggle, the
        // backdrop tap or a session-selected close. `x.set()` below does not
        // cancel an animation, so without this both write every frame. And a
        // compositor settle is not an animation ON the value at all, which is
        // why this goes through takeOverDrawer rather than `x.stop()`.
        takeOverDrawer(x)
        setDragging(true)
        if (!openRef.current) {
          // Seat the panel offscreen BEFORE it mounts, so the first painted
          // frame is the closed offset rather than a flash at rest position.
          x.set(closedOffset())
          onGestureOpenRef.current()
        }
      }

      if (phase.current !== 'locked') return
      // The finger owns the panel: keep the flush pipelines quiet one rolling
      // beat at a time, so the hold dies on its own if the gesture does.
      holdStreamingFlushes(250)
      sample(touch.clientX, e.timeStamp)
      lastX.current = touch.clientX
      lastT.current = e.timeStamp
      // Clamped to the panel's own range: dragging past open must not lift the
      // panel off its edge, and dragging past closed must not gap it further.
      x.set(clampOffset(gestureBase.current + dx))
    }

    const onTouchEnd = (e: TouchEvent) => {
      if (phase.current !== 'locked') { reset(); return }
      const touch = e.changedTouches[0]
      const dx = touch.clientX - startX.current
      // Speed over the trailing window, which reads ~0 for a finger that had
      // already stopped — so a hold-then-lift still cannot inherit the flick
      // that got it there, without a one-frame cliff throwing away real flicks.
      const v = releaseSpeed(touch.clientX, e.timeStamp)
      const settledAt = clampOffset(gestureBase.current + dx)
      // How far the FINGER actually took the panel, as a share of the travel.
      // Measured off the gesture's own base so it means the same thing whichever
      // state the gesture started in — see COMMIT_DRAG_SHARE.
      const dragged = Math.abs(settledAt - gestureBase.current) / travelPx.current
      // Rest is offset 0, so a gesture based there began on an OPEN panel.
      const startedOpen = gestureBase.current === 0

      let target: boolean
      // Signed against the opening direction, so a flick is judged by whether
      // it was heading toward open or back toward closed for THIS side.
      if (v * openDir > COMMIT_VELOCITY) target = true
      else if (v * openDir < -COMMIT_VELOCITY) target = false
      // Committing means arriving at the state the gesture was heading for: the
      // far one when it started open, the open one when it started closed.
      else target = startedOpen ? !(dragged > COMMIT_DRAG_SHARE) : dragged > COMMIT_DRAG_SHARE

      reset()
      // `onSettle(false)` unmounts the panel, so it is reported only once the
      // panel is offscreen — unmounting mid-slide is the snap this hook exists
      // to remove.
      //
      // `v` is handed on so the settle CONTINUES this release rather than
      // restarting from rest: it decelerates from the finger's own speed, and a
      // harder flick arrives sooner. A hold-and-release already zeroed `v`
      // above, which is exactly the case with no momentum to carry.
      settle(target ? 0 : closedOffset(), target, v)
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', abandon, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', abandon)
      // Unbinding mid-gesture (a viewport crossing out of mobile, an unmount)
      // must also END the gesture. `phase` is a ref, so it survives the
      // teardown: left at 'locked' it makes the next bind refuse every
      // touchstart, and lets the first touchmove resume from a stale startX —
      // the gesture would be dead until a remount, with one stray jump on the
      // way. The panel itself is not settled here; the consumer's own
      // leaving-mobile reset owns where it ends up.
      phase.current = 'idle'
      scroller.current = null
      setDragging(false)
    }
    // `open` is intentionally absent — see the header note. The callbacks are
    // held in refs for the same reason.
  }, [ref, enabled, side, x])

  return dragging
}
