import { ScrollText, Monitor, Brain, Archive, Database, Network } from 'lucide-react'
import SidePanelLayout from '../components/SidePanelLayout'
import { LogViewer } from './LogsPage'
import SystemPage from './SystemPage'
import SessionArchive from './SessionArchive'
import LocalStorageDebug from './LocalStorageDebug'
import { SharedMcpGatewayToggle } from './settings/SharedMcpGatewayToggle'
import { McpPoolableServers } from './settings/McpPoolableServers'

const TABS = [
  { key: 'logs', label: 'Logs', icon: <ScrollText size={16} />, description: 'Live log viewer with level filtering and search' },
  { key: 'system', label: 'System', icon: <Monitor size={16} />, description: 'CPU, memory, network, and process metrics' },
  { key: 'storage', label: 'Storage', icon: <Database size={16} />, description: 'localStorage usage, quotas, and garbage collection' },
  { key: 'mcp-pool', label: 'MCP Pool', icon: <Network size={16} />, description: 'Shared MCP gateway and poolable server configuration' },
  { key: 'memory', label: 'Memory', icon: <Brain size={16} />, description: 'Embedding provider, vector store, and consolidation' },
  { key: 'archive', label: 'Archive', icon: <Archive size={16} />, description: 'Rotated/compacted session history (7-day retention)' },
]

export default function DeveloperPage() {
  return (
    <SidePanelLayout title="Developer" tabs={TABS}>
      {tab => <>
        {tab === 'logs' && <div className="h-[calc(100vh-160px)] min-h-[300px] flex flex-col overflow-hidden"><LogViewer compact /></div>}
        {tab === 'system' && <SystemPage embedded />}
        {tab === 'storage' && <LocalStorageDebug />}
        {tab === 'mcp-pool' && (
          <>
            <SharedMcpGatewayToggle />
            <McpPoolableServers />
          </>
        )}
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
