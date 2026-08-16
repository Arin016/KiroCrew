// Scroll-driven header collapse for the detail panes.
//
// Both detail panes keep their header OUTSIDE the scroller, so it is standing
// furniture: whatever it costs vertically, it costs for the whole session. On a
// phone that bill is most of the screen — measured against the panes' own
// markup, the header is 211px of a 390x844 viewport (a normal title wraps onto
// three lines at 27px) and 272px of a 320x568 one, leaving the description and
// timeline 52% of the shortest phone. A desktop pays the same 211px out of ~900,
// which is why the size is right there and wrong here.
//
// So while narrow the header earns its height only until the reader starts
// reading: past the first swipe the title shrinks to a single compact line and
// the standing metadata that the opening comment card repeats a few pixels
// below (author, when, identity badge) drops out. Collapsing reclaims 106px at
// 390px and 167px at 320px.
//
// The scroll signal is deliberately the WRAPPER's, not a viewport query: below
// `sm:` the wrapper is the pane's single scroller and above it the wrapper is
// `overflow-visible` and the two body columns scroll themselves. So this hook
// only ever hears from the layout that has a collapsible header, with no second
// breakpoint to keep in step (`useIsMobile` flips at 768px, `sm:` at 640px —
// they disagree across an 128px band). The RENDER side still gates every
// compact class behind `sm:`, so a stale `collapsed` cannot follow a resize onto
// a desktop.
import { useCallback, useState } from 'react'

/** Collapse once the reader has swiped past roughly one line of content. */
const COLLAPSE_AT = 56
/** Re-expand only near the very top, so the two thresholds cannot chatter. */
const EXPAND_AT = 16

/**
 * The hysteresis, as a pure function so the band is falsifiable on its own.
 *
 * Asymmetric on purpose. Collapsing grows the scroll area by the reclaimed
 * height, which lowers the maximum scroll offset and can let the browser clamp
 * `scrollTop` back down; with a single threshold that clamp would re-expand,
 * restoring the offset, and the header would flicker for as long as the finger
 * stayed still. With the band, a clamp can only ever land the pane back at the
 * top and leave it there — expanding never raises `scrollTop`, so nothing
 * re-triggers a collapse until the reader scrolls again.
 */
export function nextCollapsed(collapsed: boolean, scrollTop: number): boolean {
  return collapsed ? scrollTop > EXPAND_AT : scrollTop > COLLAPSE_AT
}

/** `collapsed` plus the `onScroll` handler to hang on the pane's scroller. */
export function useCollapsingHeader() {
  const [collapsed, setCollapsed] = useState(false)
  const onScroll = useCallback((e: React.UIEvent<HTMLElement>) => {
    const { scrollTop } = e.currentTarget
    // Functional update: React bails out of the re-render when the next value
    // equals the current one, so the common case (a scroll event that does not
    // cross either threshold) costs nothing and needs no rAF throttle.
    setCollapsed((prev) => nextCollapsed(prev, scrollTop))
  }, [])
  return { collapsed, onScroll }
}
