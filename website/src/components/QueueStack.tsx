import { useState, useRef, useEffect, memo } from 'react'
import { AnimatePresence, motion, useMotionValue, useSpring } from 'framer-motion'
import { Hourglass, ChevronUp, X, Zap } from 'lucide-react'
import type { ChatMessage } from '../types'

const MAX_PEEK = 2
const CARD_H = 40
const PEEK = 6
const EXPANDED_GAP = 4
const SCALE_STEP = 0.04
const HIDDEN_EXTRA_SCALE = 0.02
const OVERLAP = 6 // overlap to fuse with input area below

const DEPTH_BRIGHTNESS = [1, 0.88, 0.76]
const SPRING = { type: 'spring' as const, stiffness: 400, damping: 30 }

function QueueStackInner({ messages, onCancel, onInterrupt }: { messages: ChatMessage[]; onCancel?: (queueId: string) => void; onInterrupt?: (queueId: string) => void }) {
  const [_expanded, setExpanded] = useState(false)
  const expanded = _expanded && messages.length > 1

  // Reset expanded when queue drains to trivial size
  useEffect(() => {
    if (messages.length <= 1) setExpanded(false)
  }, [messages.length])

  const peekCount = Math.min(MAX_PEEK, Math.max(0, messages.length - 1))
  const collapsedHeight = messages.length > 0 ? CARD_H + peekCount * PEEK : 0
  const expandedHeight = messages.length > 0 ? messages.length * CARD_H + (messages.length - 1) * EXPANDED_GAP : 0

  const targetHeight = expanded ? expandedHeight : collapsedHeight
  const targetMargin = messages.length > 0 && !expanded ? -OVERLAP : 0

  // Imperatively control margin: spring on expand/collapse, snap on enter/exit
  const marginMV = useMotionValue(targetMargin)
  const marginSpring = useSpring(marginMV, SPRING)
  const prevExpanded = useRef(expanded)

  useEffect(() => {
    const expandChanged = prevExpanded.current !== expanded
    prevExpanded.current = expanded

    if (expandChanged) {
      // Expand/collapse: animate via spring
      marginMV.set(targetMargin)
    } else if (messages.length > 0) {
      // Enter (count increased) or count decreased but not to 0: snap immediately
      // When count hits 0, let onExitComplete handle the margin reset
      marginSpring.jump(targetMargin)
    }
  }, [expanded, targetMargin, messages.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Handle last-card exit: snap margin to 0 when AnimatePresence finishes
  const prevCountForExit = useRef(messages.length)
  const hasExitingRef = useRef(false)
  useEffect(() => {
    if (messages.length < prevCountForExit.current) hasExitingRef.current = true
    prevCountForExit.current = messages.length
  }, [messages.length])

  const onExitComplete = () => {
    hasExitingRef.current = false
    if (messages.length === 0) marginSpring.jump(0)
  }

  return (
    <div className="px-5 mx-auto w-full relative" style={{ maxWidth: 'var(--mc-content-width, 900px)', zIndex: 0 }}>
      <motion.div
        className="relative cursor-pointer"
        animate={{ height: targetHeight }}
        transition={SPRING}
        style={{ marginBottom: marginSpring }}
        onClick={() => messages.length > 1 && setExpanded(e => !e)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if ((e.key === 'Enter' || e.key === ' ') && messages.length > 1) {
            e.preventDefault()
            setExpanded(prev => !prev)
          }
        }}
        role={messages.length > 1 ? 'button' : undefined}
        tabIndex={messages.length > 1 ? 0 : undefined}
        aria-expanded={messages.length > 1 ? expanded : undefined}
      >
        <AnimatePresence initial={false} onExitComplete={onExitComplete}>
          {messages.map((m, i) => {
            let y: number
            let scale: number
            let opacity: number
            let zIndex: number
            let brightness: number

            if (expanded) {
              const pos = messages.length - 1 - i
              y = pos * (CARD_H + EXPANDED_GAP)
              scale = 1
              opacity = 1
              zIndex = pos + 1
              brightness = 1
            } else if (i <= MAX_PEEK) {
              const depth = i
              y = (collapsedHeight - CARD_H) - depth * PEEK
              scale = 1 - (depth + 1) * SCALE_STEP
              opacity = 1
              zIndex = (MAX_PEEK + 1) - depth
              brightness = DEPTH_BRIGHTNESS[depth] ?? DEPTH_BRIGHTNESS[MAX_PEEK]
            } else {
              y = (collapsedHeight - CARD_H) - MAX_PEEK * PEEK
              scale = 1 - (MAX_PEEK + 1) * SCALE_STEP - HIDDEN_EXTRA_SCALE
              opacity = 0
              zIndex = 0
              brightness = DEPTH_BRIGHTNESS[MAX_PEEK]
            }

            const isFrontCollapsed = !expanded && i === 0

            return (
              <motion.div
                key={m.meta?.queueId as string ?? m.ts ?? `q-${i}-${m.content}`}
                initial={false}
                animate={{
                  opacity, y, scale,
                  filter: `brightness(${brightness})`,
                  borderTopLeftRadius: 12,
                  borderTopRightRadius: 12,
                  borderBottomLeftRadius: isFrontCollapsed ? 0 : 12,
                  borderBottomRightRadius: isFrontCollapsed ? 0 : 12,
                  borderBottomWidth: isFrontCollapsed ? 0 : 1,
                }}
                exit={{ y: y + 40, zIndex: 50, borderBottomWidth: 1, borderBottomLeftRadius: 12, borderBottomRightRadius: 12, transition: { duration: 0.3, ease: 'easeIn' } }}
                transition={SPRING}
                className="absolute top-0 left-0 right-0 bg-warn border border-warn/20 px-3 py-2 text-[13px] text-warn-fg"
                style={{ transformOrigin: 'bottom center', height: CARD_H, zIndex }}
              >
                <span className="flex items-center gap-1.5 h-full">
                  <span className="shrink-0 text-[10px] font-mono opacity-50 w-4 text-center">{i + 1}</span>
                  {isFrontCollapsed && (
                    <span className="shrink-0 inline-flex animate-[hourglass-flip_3s_ease-in-out_infinite]">
                      <Hourglass size={13} />
                    </span>
                  )}
                  <span className="truncate flex-1">{m.content}</span>
                  {onInterrupt && (expanded || messages.length === 1) && !!m.meta?.queueId && (
                    <button
                      className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors text-white"
                      title="Interrupt current turn and send this now"
                      aria-label="Send now"
                      onClick={(e) => { e.stopPropagation(); onInterrupt(m.meta!.queueId as string) }}
                    >
                      <Zap size={13} fill="currentColor" />
                    </button>
                  )}
                  {onCancel && (expanded || messages.length === 1) && !!m.meta?.queueId && (
                    <button
                      className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors"
                      title="Cancel and move back to input"
                      aria-label="Cancel queued message"
                      onClick={(e) => { e.stopPropagation(); onCancel(m.meta!.queueId as string) }}
                    >
                      <X size={13} />
                    </button>
                  )}
                  {isFrontCollapsed && messages.length > 1 && (
                    <span className="shrink-0 flex items-center gap-1 text-[11px] opacity-70">
                      {messages.length} queued
                      <ChevronUp size={12} />
                    </span>
                  )}
                  {expanded && i === 0 && (
                    <ChevronUp size={13} className="shrink-0 opacity-50 rotate-180" />
                  )}
                </span>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

export default memo(QueueStackInner, (prev, next) =>
  prev.messages.length === next.messages.length &&
  prev.messages.every((m, i) => m === next.messages[i]) &&
  prev.onCancel === next.onCancel &&
  prev.onInterrupt === next.onInterrupt
)
