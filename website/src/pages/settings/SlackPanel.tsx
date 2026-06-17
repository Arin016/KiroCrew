import { MessageCircle } from 'lucide-react'
import { EmptyState } from '../../components/ui'

/** Slack channel-integration settings. Speech-to-text moved to the Voice tab. */
export function SlackPanel() {
  return (
    <EmptyState
      icon={<MessageCircle size={40} />}
      title="Slack channel integration"
      subtitle="Channel integration settings will live here."
    />
  )
}
