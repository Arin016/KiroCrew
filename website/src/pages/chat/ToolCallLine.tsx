import { memo, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { shallowEqual } from 'react-redux'
import { useAppSelector, useAppDispatch } from '../../store'
import { clearFocusToolCallId } from '../../store/chatSlice'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { LoaderCircle, CircleSlash, CircleDot, Lock } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import type { ChatMessage } from '../../types'
import { ToolDetails } from './ToolDetails'
import { registerToolPill } from '../../store/toolPillRegistry'

/** Inline tool call pill. Click toggles an expanded panel below the pill that
 *  shows purpose / input / output (the same details that previously lived in
 *  the Activity sidebar's deprecated "Tools" tab). */
export default memo(function ToolCallLine({ message, running: _running }: { message: ChatMessage; running: boolean }) {
  const dispatch = useAppDispatch()
  const label = message.content.replace(/^🔧\s*/, '')
  const toolCallId = message.meta?.tool_call_id as string | undefined
  const simplified = useSimplifiedToolNames()

  // Pull the matching toolLog entry. Returns purpose/input/output for the inline
  // expansion as well as completion status for the icon.
  const { effectiveId, isDone, isRejected, purpose, input, output, auto, ts, hasEntry } = useAppSelector(s => {
    const log = s.chat.toolLog
    const slotRunning = s.chat.slotRunning
    const msgs = s.chat.messages

    // Helper: check if this tool's permission was resolved as rejected
    const wasRejectedByPerm = () => {
      if (!toolCallId) return false
      for (let j = msgs.length - 1; j >= 0; j--) {
        const m = msgs[j]
        if (m.role !== 'permission' || !m.meta?.tool_call_id) continue
        if (m.meta.tool_call_id === toolCallId) {
          return m.meta?.resolved === 'rejected'
        }
      }
      return false
    }

    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i]
      if (e.type !== 'tool') continue
      if ((toolCallId && e.tool_call_id === toolCallId) || (!toolCallId && e.tool_call_id && label.includes(e.text))) {
        const rejected = !!e.rejected || wasRejectedByPerm()
        const isDone = e.output != null || rejected || !slotRunning
        return {
          effectiveId: e.tool_call_id || null,
          isDone, isRejected: rejected,
          purpose: e.purpose || '',
          input: e.input || '',
          output: e.output || '',
          auto: !!e.auto,
          ts: e.ts || 0,
          hasEntry: true,
        }
      }
    }
    // No toolLog entry — historical message. Check permission state for rejection.
    const rejected = wasRejectedByPerm()
    // Backend persists `input` (when the call was issued) and `output` (when
    // the result arrived) directly on the tool message's meta — see
    // _tool_meta() and the EVENT_TOOL_RESULT handler in chat_runner.py.
    // Pre-persistence messages won't have these fields and fall through to
    // the empty-state hint inside ToolDetails.
    const metaInput = (message.meta?.input as string | undefined) || ''
    const metaOutput = (message.meta?.output as string | undefined) || ''
    return {
      effectiveId: toolCallId || null,
      isDone: true, isRejected: rejected,
      purpose: (message.meta?.purpose as string) || '',
      input: metaInput, output: metaOutput, auto: false,
      // ChatMessage.ts is a string (ISO timestamp) when restored from history;
      // parse it for the meta-row time renderer. Falls to 0 if unparseable —
      // fmtTime hides the row when ts is 0.
      ts: typeof message.ts === 'number' ? message.ts : (message.ts ? Date.parse(String(message.ts)) || 0 : 0),
      // Treat the message as having an entry when persisted I/O is available,
      // so the empty-state copy only shows for truly bare historical messages.
      hasEntry: !!(metaInput || metaOutput),
    }
  }, shallowEqual)

  // Check if this specific tool has a pending (unresolved) permission matching its tool_call_id.
  // Only match when tool_call_id is present on both sides — prevents batched approvals from
  // incorrectly lighting up all pills as pending approval.
  const hasPendingPerm = useAppSelector(s => {
    if (isDone || !toolCallId) return false
    const msgs = s.chat.messages
    for (let j = msgs.length - 1; j >= 0; j--) {
      const m = msgs[j]
      if (m.role !== 'permission' || m.meta?.resolved || !m.meta?.tool_call_id) continue
      if (m.meta.tool_call_id === toolCallId) return true
    }
    return false
  })

  // Inline expansion state.
  //
  // Default `expanded` mirrors `hasPendingPerm` so a tool that lands awaiting
  // approval (or one that's still pending after a page reload) opens with its
  // details visible — the inline panel is the only place the user can read
  // what the agent is about to run.
  //
  // `pendingAutoExpand` tracks whether the current expanded state was *driven*
  // by the pending-approval transition. We clear it on any user interaction
  // (manual toggle / focus signal) so the panel stays open if the user took
  // explicit control, and only auto-collapse when the approval resolves
  // *and* we were the ones who opened it.
  const [expanded, setExpanded] = useState(() => hasPendingPerm)
  const [pendingAutoExpand, setPendingAutoExpand] = useState(() => hasPendingPerm)
  const prevPendingRef = useRef(hasPendingPerm)
  useEffect(() => {
    const wasPending = prevPendingRef.current
    prevPendingRef.current = hasPendingPerm
    if (hasPendingPerm && !wasPending) {
      // Approval just became pending → auto-expand
      setExpanded(true)
      setPendingAutoExpand(true)
    } else if (!hasPendingPerm && wasPending && pendingAutoExpand) {
      // Approval just resolved (approved/rejected/cancelled) and the user
      // didn't take over → auto-collapse. Defer to the next animation frame
      // so any concurrent state changes (inner Input/Output section auto-
      // promote when output arrives, output content rendering, etc.) commit
      // and settle layout first. Without this, AnimatePresence captures a
      // mid-flux height for the exit animation and the panel snaps shut
      // instead of animating cleanly.
      const raf = requestAnimationFrame(() => {
        setExpanded(false)
        setPendingAutoExpand(false)
      })
      return () => cancelAnimationFrame(raf)
    }
  }, [hasPendingPerm, pendingAutoExpand])

  const containerRef = useRef<HTMLDivElement>(null)

  // External focus signal (e.g. from ChatInput's "Jump to tool" link). When the
  // redux focusToolCallId matches this pill, auto-expand and scroll into view,
  // then clear the focus so subsequent re-renders don't keep firing. Treat the
  // jump as user intent — clear pendingAutoExpand so the panel stays open
  // through approval resolution.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (focusToolCallId && effectiveId && focusToolCallId === effectiveId) {
      setExpanded(true)
      setPendingAutoExpand(false)
      requestAnimationFrame(() => containerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
      dispatch(clearFocusToolCallId())
    }
  }, [focusToolCallId, effectiveId, dispatch])

  const Icon = isDone
    ? (isRejected ? CircleSlash : CircleDot)
    : hasPendingPerm ? Lock
    : LoaderCircle
  const iconClass = isDone
    ? (isRejected ? 'text-danger' : 'text-ok')
    : hasPendingPerm ? 'text-warn'
    : 'text-accent animate-spin'
  // Match the panel's left rail to the pill's status — keeps the visual chain
  // (icon → bar → content) coherent across rejected (red), done (green),
  // pending-approval (yellow), and running (accent) states. Inline style with
  // color-mix() rather than Tailwind opacity classes — the project's Tailwind
  // config doesn't compile `border-{color}/N` opacity variants for theme colors.
  const barColor = isDone
    ? (isRejected ? 'var(--danger)' : 'var(--ok)')
    : hasPendingPerm ? 'var(--warn)' : 'var(--accent)'
  const barStyle = `color-mix(in srgb, ${barColor} 70%, transparent)`
  const toolLabel = (simplified && (purpose || message.meta?.purpose)) ? (purpose || message.meta?.purpose as string) : label
  // Both running and pending-approval pills shimmer — the highlight color
  // tracks the status so pending shimmers warn-yellow (matching the approval
  // bar) and running shimmers accent.
  const isShimmering = !isDone
  const shimmerHighlight = hasPendingPerm ? 'var(--warn)' : 'var(--accent)'
  const shimmerBase = 'var(--muted)'

  // Click-to-toggle handler — kept stable so memo() short-circuits work.
  // User click is explicit intent — clear pendingAutoExpand so the panel
  // doesn't auto-collapse out from under them when the approval resolves.
  // Pending-approval pills are locked open: clicking them is a no-op so the
  // user can't hide the input they're being asked to approve.
  const onToggle = useCallback(() => {
    if (hasPendingPerm) return
    setExpanded(e => !e)
    setPendingAutoExpand(false)
  }, [hasPendingPerm])

  const fmtTime = (t: number) => t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''

  // Pending pills are always expanded — `expanded` state still tracks the
  // user's intent for after the approval resolves, but the rendered panel
  // ignores it while pending.
  const effectivelyExpanded = expanded || hasPendingPerm

  // Stable per-instance fallback id for framer-motion's `LayoutGroup`. When a
  // pre-persistence historical message has neither `effectiveId` nor
  // `toolCallId`, multiple pills would otherwise share `tool-detail-` and the
  // segmented-control highlight would fly between unrelated panels.
  const fallbackId = useId()

  // While this pill is awaiting approval, register its DOM node with the
  // tool pill visibility registry. The approval bar in ChatInput subscribes
  // and grows a "ghost pill" mirror when this node scrolls out of view, so
  // the user never loses sight of what the tool is about to do. We use
  // useLayoutEffect so registration commits before paint — eliminates the
  // brief flicker that would otherwise show a ghost on the same frame the
  // pill mounts already-visible.
  const pillButtonRef = useRef<HTMLButtonElement>(null)
  useLayoutEffect(() => {
    if (!hasPendingPerm || !toolCallId) return
    const el = pillButtonRef.current
    if (!el) return
    return registerToolPill(toolCallId, el)
  }, [hasPendingPerm, toolCallId])

  return (
    <div ref={containerRef} className="ft-block-reveal">
      <button
        ref={pillButtonRef}
        className={`inline-flex items-start gap-1 text-[13px] font-mono px-2 py-0.5 rounded-md transition-all text-left focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none ${hasPendingPerm ? 'cursor-default' : 'cursor-pointer hover:brightness-110'}`}
        aria-expanded={effectivelyExpanded}
        aria-label={hasPendingPerm ? `Awaiting approval for tool: ${label}` : `${effectivelyExpanded ? 'Hide' : 'Show'} details for tool: ${label}`}
        onClick={onToggle}
      >
        <Icon size={12} className={`shrink-0 ${iconClass}`} style={{ marginTop: '3px' }} />
        {isShimmering ? (
          <motion.span
            className="break-words min-w-0 bg-clip-text"
            style={{
              backgroundImage: `linear-gradient(90deg, ${shimmerBase} 0%, ${shimmerBase} 40%, ${shimmerHighlight} 50%, ${shimmerBase} 60%, ${shimmerBase} 100%)`,
              backgroundSize: '300% 100%',
              WebkitTextFillColor: 'transparent',
              color: 'transparent',
            }}
            animate={{ backgroundPosition: ['100% 0%', '-50% 0%'] }}
            transition={{ duration: 2.4, repeat: Infinity, ease: 'linear' }}
          >{toolLabel}</motion.span>
        ) : (
          <span className="break-words min-w-0 text-muted hover:text-text transition-colors">{toolLabel}</span>
        )}
      </button>

      <AnimatePresence initial={false}>
        {effectivelyExpanded && (
          <motion.div
            key="tool-details"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.35, ease: [0.4, 0.0, 0.2, 1] /* Material standard */ }}
            style={{ overflow: 'hidden' }}
          >
            <ToolDetails purpose={purpose} pillLabel={toolLabel} toolName={label} input={input} output={output} auto={auto} pending={hasPendingPerm} ts={ts} hasEntry={hasEntry} fmtTime={fmtTime} barColor={barStyle} layoutId={`tool-detail-${effectiveId || toolCallId || fallbackId}`} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
})
