/**
 * OrchestratedChatPage — orchestrator mode for multi-agent planning.
 *
 * Wraps ChatPage with mode="orchestrator" so new slots created from this
 * page use the orchestrator prompt and plan lessons. The conductor skill
 * is independent — it controls agent routing in both pages.
 */
import ChatPage from './ChatPage'

export default function OrchestratedChatPage() {
  return <ChatPage mode="orchestrator" />
}
