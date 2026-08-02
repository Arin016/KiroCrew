/**
 * useBubble — Manages bubble notification state with auto-dismiss timer
 * and fade-out animation.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { BUBBLE_FADE_MS } from '../../shared/constants'

import { api } from '../../mochiApi'

const BUBBLE_AUTO_DISMISS_MS = 6000

export interface UseBubbleReturn {
  bubble: string | null
  bubbleFading: boolean
  dismissBubble: () => void
}

export function useBubble(): UseBubbleReturn {
  const [bubble, setBubble] = useState<string | null>(null)
  const [bubbleFading, setBubbleFading] = useState(false)
  const fadingRef = useRef(false)
  const bubbleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Stable callback — no dependency on bubbleFading state, uses ref instead.
  // This avoids stale-closure issues when drainNextBubble sends a new bubble
  // right after a dismiss (the old useCallback captured stale bubbleFading=true).
  const dismissBubble = useCallback(() => {
    if (fadingRef.current) return
    fadingRef.current = true
    setBubbleFading(true)
    if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
    setTimeout(() => { setBubble(null); setBubbleFading(false); fadingRef.current = false }, BUBBLE_FADE_MS)
    api?.dismissBubble?.()
  }, [])

  useEffect(() => {
    const off = api?.onBubble?.((text: string, sticky: boolean) => {
      fadingRef.current = false
      setBubbleFading(false)
      setBubble(text)
      if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
      if (!sticky) {
        bubbleTimer.current = setTimeout(() => dismissBubble(), BUBBLE_AUTO_DISMISS_MS)
      }
    })
    return () => { off?.() }
  }, [dismissBubble])

  return { bubble, bubbleFading, dismissBubble }
}
