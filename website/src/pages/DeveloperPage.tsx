import { ScrollText, Monitor, Brain, Archive } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import { LogViewer } from './LogsPage'
import SystemPage from './SystemPage'
import SessionArchive from './SessionArchive'

const TABS = [
  { key: 'logs', label: 'Logs', icon: <ScrollText size={16} />, description: 'Live log viewer with level filtering and search' },
  { key: 'system', label: 'System', icon: <Monitor size={16} />, description: 'CPU, memory, network, and process metrics' },
  { key: 'memory', label: 'Memory', icon: <Brain size={16} />, description: 'Embedding provider, vector store, and consolidation' },
  { key: 'archive', label: 'Archive', icon: <Archive size={16} />, description: 'Rotated/compacted session history (7-day retention)' },
]

export default function DeveloperPage() {
  return (
    <SidePanelLayout title="Developer" tabs={TABS} fixedContent>
      {tab => <>
        {tab === 'logs' && <LogViewer compact />}
        {tab === 'system' && <SystemPage embedded />}
        {tab === 'memory' && (
          <div className="text-muted text-sm py-12 text-center">
            Memory internals — coming soon
          </div>
        )}
        {tab === 'archive' && <SessionArchive />}
      </>}
    </SidePanelLayout>
  )
}
