import { useSyncExternalStore } from 'react'

/* Exported for the top-bar rung-budget test: the <640px icon-only rung base in
   index.css needs the desktop readouts to be unreachable below the pill's
   label gate, which holds only while this form switch sits at or above it. */
export const MOBILE_BREAKPOINT = 768
const MOBILE_QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`

const mql = typeof window !== 'undefined' ? window.matchMedia(MOBILE_QUERY) : null

// A single missed or deferred `change` delivery used to strand the breakpoint;
// these only notify — getSnapshot still reads mql.matches, so a no-op wake bails.
const WINDOW_RECHECK_EVENTS = ['orientationchange', 'resize', 'pageshow'] as const
// visibilitychange fires at the document, not the window.
const DOCUMENT_RECHECK_EVENTS = ['visibilitychange'] as const

function subscribe(cb: () => void) {
  mql?.addEventListener('change', cb)
  if (typeof window !== 'undefined') {
    for (const type of WINDOW_RECHECK_EVENTS) window.addEventListener(type, cb)
    for (const type of DOCUMENT_RECHECK_EVENTS) document.addEventListener(type, cb)
  }
  return () => {
    mql?.removeEventListener('change', cb)
    if (typeof window !== 'undefined') {
      for (const type of WINDOW_RECHECK_EVENTS) window.removeEventListener(type, cb)
      for (const type of DOCUMENT_RECHECK_EVENTS) document.removeEventListener(type, cb)
    }
  }
}

function getSnapshot() {
  return mql?.matches ?? false
}

function getServerSnapshot() {
  return false
}

export function useIsMobile() {
  const match = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
  // In embed mode (IntelliJ plugin minimal view), never report as mobile
  // regardless of viewport width. The plugin panel can be narrow but should
  // always behave as desktop (Enter to send, full icon row, no collapsed UI).
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/embed/')) return false
  return match
}
