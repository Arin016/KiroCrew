import type { ChatMessage } from '../types'
import { parseOptions } from '../pages/chat/AssistantMessage'

export interface FollowUpDerivation {
  followUpOptions: string[]
  followUpIsPlan: boolean
}

/**
 * Derive the follow-up `[OPTIONS:]` buttons for the current chat by scanning
 * backward for the most recent real assistant turn.
 *
 * Three messages short-circuit the scan:
 *  - a `user` message ends the previous turn, so its options no longer apply →
 *    return none.
 *  - a `queued` message means the user already acted (Quick Send while the
 *    slot was busy). The optimistic user bubble was suppressed, but the intent
 *    is identical — hide options immediately so they don't linger until the
 *    queue drains.
 *  - a `compaction` notice is skipped. Auto-compaction appends a
 *    "✅ Conversation compacted" message with the `assistant` role but tagged
 *    `kind="compaction"` (see `chat_utils._broadcast_compaction_result`). It
 *    carries no `[OPTIONS:]` marker, so without this skip it would shadow the
 *    real options-bearing turn it follows and the buttons would vanish after a
 *    compaction. The marker is read from `kind` (live websocket path) or
 *    `meta.kind` (history-reload path).
 */
export function deriveFollowUpOptions(
  messages: ChatMessage[],
  isStreaming: boolean,
): FollowUpDerivation {
  if (isStreaming) return { followUpOptions: [], followUpIsPlan: false }
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'user' || m.role === 'queued') return { followUpOptions: [], followUpIsPlan: false }
    if ((m.kind ?? (m.meta?.kind as string | undefined)) === 'compaction') continue
    if (m.role === 'assistant' && m.content) {
      const { options, isPlan } = parseOptions(m.content)
      return { followUpOptions: options, followUpIsPlan: isPlan }
    }
  }
  return { followUpOptions: [], followUpIsPlan: false }
}
