/** Threshold (ms) below which consecutive search-nav steps are treated as
 * "rapid stepping" and snap instantly instead of smooth-scrolling. */
export const RAPID_STEP_MS = 250

/**
 * Decide how the chat should scroll when the search match changes.
 *
 * While the user steps rapidly (next/prev faster than `thresholdMs`), snap
 * instantly (`'auto'`) — a smooth animation would be interrupted and restarted
 * on every keypress, producing stutter. A lone step (or the final one after a
 * pause) glides smoothly so the landing feels settled.
 */
export function pickSearchScrollBehavior(
  now: number,
  lastStepAt: number,
  thresholdMs: number = RAPID_STEP_MS,
): ScrollBehavior {
  return now - lastStepAt < thresholdMs ? 'auto' : 'smooth'
}

/**
 * Center the active search occurrence (`mark.search-current`) in the viewport,
 * re-applying across animation frames so it CONVERGES as the target settles.
 * A far jump mounts an unmeasured virtualized row, and a match inside a
 * collapsed turn triggers a ~300ms expand animation — both keep shifting layout
 * after an initial scroll, so a single (or short) attempt lands on a stale
 * offset (often top-of-list). Re-centering for ~600ms tracks the mark through
 * measurement + expansion and ends on the correct spot on the first click. The
 * loop bails immediately if the user scrolls (wheel/touch) so it never fights
 * them, and each call is a no-op once the position is stable. Instant — no
 * animation of its own.
 *
 * Returns a `cancel()` function so callers (e.g. a useEffect that re-runs on
 * every active-occurrence change) can abort the previous loop before starting a
 * new one and on unmount — otherwise rapid navigation accumulates concurrent
 * loops, each with its own window listeners, some running against detached DOM.
 */
export function scrollCurrentMatchIntoView(root?: Element | null, maxFrames = 36): () => void {
  let frame = 0
  let cancelled = false
  const target: EventTarget | undefined =
    typeof window !== 'undefined' ? window : undefined
  const cleanup = () => {
    target?.removeEventListener('wheel', cancel)
    target?.removeEventListener('touchmove', cancel)
  }
  const cancel = () => { cancelled = true; cleanup() }
  target?.addEventListener('wheel', cancel, { passive: true })
  target?.addEventListener('touchmove', cancel, { passive: true })
  const tick = () => {
    if (cancelled) return
    const mark = (root ?? document).querySelector('mark.search-current') as HTMLElement | null
    mark?.scrollIntoView?.({ block: 'center' })
    if (++frame < maxFrames) requestAnimationFrame(tick)
    else cleanup()
  }
  requestAnimationFrame(tick)
  return cancel
}
