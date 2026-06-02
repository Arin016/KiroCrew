import { memo } from 'react'
import { motion } from 'framer-motion'
import { Square, XOctagon } from 'lucide-react'
import type { ChatMessage } from '../../types'

/** Inline card for stop_event messages. Three visual states driven by meta.state. */
export default memo(function StopEventCard({ message }: { message: ChatMessage }) {
  const state = (message.meta?.state as string) ?? 'stopping'

  if (state === 'stopping') {
    return (
      <motion.div
        role="status"
        aria-label="Stopping in progress"
        aria-live="polite"
        className="text-danger text-[13px] font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-1.5"
        animate={{ opacity: [0.6, 1, 0.6] }}
        transition={{ duration: 1.2, repeat: Infinity }}
        data-testid="stop-event-card"
        data-state={state}
      >
        <Square size={13} fill="currentColor" className="lucide-inline" aria-hidden="true" />
        Stopping…
      </motion.div>
    )
  }

  if (state === 'stop_failed_reset') {
    return (
      <div
        role="alert"
        aria-label="Stop failed, session reset"
        className="text-danger text-[13px] font-mono px-3 py-2 rounded-md border border-danger/15 bg-danger-subtle inline-flex items-center gap-1.5"
        data-testid="stop-event-card"
        data-state={state}
      >
        <XOctagon size={13} className="lucide-inline" aria-hidden="true" />
        [Stop Failed, Session Reset]
      </div>
    )
  }

  // Default: 'stopped'
  return (
    <div
      role="status"
      aria-label="Stopped"
      className="text-danger text-[13px] font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-1.5"
      data-testid="stop-event-card"
      data-state={state}
    >
      <Square size={13} fill="currentColor" className="lucide-inline" aria-hidden="true" />
      [Stopped]
    </div>
  )
})
