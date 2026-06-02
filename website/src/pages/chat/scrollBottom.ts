// Jump to the absolute scroll bottom of a Virtuoso list.
//
// `instant=true` skips the smooth-scroll animation -- use for the user-clicked
// "Bottom" pill, where animating page-by-page from far up feels slow.
// `instant=false` (default) keeps the smooth animation for organic
// auto-scrolls (streaming, tab switch, slot start).
//
// Why a multi-frame settle: `scrollToIndex(LAST, 'end')` aligns the last
// item's bottom with the viewport bottom, but two effects keep scrollHeight
// growing for several frames after the call returns:
//   1. Virtuoso lazy-expands items as the scroll traverses them; each
//      estimated-height placeholder swap to its real height bumps
//      scrollHeight upward.
//   2. The Footer (ChatFooter + 2vh spacer) sits below the last item, so
//      the scroller's true max is scrollHeight - clientHeight, not the
//      last item's edge.
//
// `scrollBy({ top: huge })` clamps to the *current* scrollHeight, so a
// single rAF nudge can land short of the true bottom. Iterating the nudge
// across ~30 frames (~500 ms at 60fps) re-targets each frame as scrollHeight
// settles. It is a no-op once we are at the bottom.
import type { VirtuosoHandle } from 'react-virtuoso'

// Large enough to exceed any reasonable scroller height. The scroller
// clamps the delta to (scrollHeight - clientHeight - currentTop), so this
// always targets the exact bottom regardless of content length.
const PAST_FOOTER_NUDGE = 1e9

// Number of animation frames over which to keep pushing toward the bottom.
// At 60fps this is ~500 ms, which empirically covers Virtuoso's lazy-render
// settle window for the deepest histories observed in practice.
const SETTLE_FRAMES = 30

export function scrollToTrueBottom(virtuoso: VirtuosoHandle | null, instant: boolean = false): void {
  if (!virtuoso) return
  const behavior: ScrollBehavior = instant ? 'auto' : 'smooth'
  virtuoso.scrollToIndex({ index: 'LAST', behavior, align: 'end' })
  let remaining = SETTLE_FRAMES
  const settle = () => {
    virtuoso.scrollBy({ top: PAST_FOOTER_NUDGE, behavior })
    if (--remaining > 0) requestAnimationFrame(settle)
  }
  requestAnimationFrame(settle)
}
