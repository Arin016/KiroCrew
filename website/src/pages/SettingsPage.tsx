import { Bell, Code, Cpu, Globe, LayoutGrid, MessageCircle, MessageSquare, Palette, ShieldCheck } from 'lucide-react'
import { useAppSelector } from '../store'
import SidePanelLayout from '../components/SidePanelLayout'
import { BrowserPanel } from './settings/BrowserPanel'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { GeneralPanel } from './settings/GeneralPanel'
import { SecurityPanel } from './settings/SecurityPanel'
import { SlackPanel } from './settings/SlackPanel'
import { OverviewPanel } from './settings/OverviewPanel'
import { ProviderPanel } from './settings/ProviderPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'

const TABS = [
  { key: 'overview', label: 'Overview', icon: <LayoutGrid size={16} />, description: 'System status, memory, agent config, and usage metrics' },
  { key: 'provider', label: 'Provider', icon: <Cpu size={16} />, description: 'LLM provider backend, model selection, and session limits' },
  { key: 'chat', label: 'Chat', icon: <MessageSquare size={16} />, description: 'Message behavior, history, timestamps, and voice settings' },
  { key: 'display', label: 'Display', icon: <Palette size={16} />, description: 'Zoom, font, and color theme preferences' },
  { key: 'browser', label: 'Browser', icon: <Globe size={16} />, description: 'Playwright browser mode, extension token, and auth configuration' },
  { key: 'security', label: 'Security', icon: <ShieldCheck size={16} />, description: 'Security posture, defense layers, certifications, and data classification' },
  { key: 'notifications', label: 'Notifications', icon: <Bell size={16} />, description: 'Sound effects and per-category alert preferences' },
  { key: 'slack', label: 'Slack', icon: <MessageCircle size={16} />, description: 'Slack integration, allowed users, channels, and STT settings' },
  { key: 'developer', label: 'Developer', icon: <Code size={16} />, description: 'Developer mode, logs, system metrics, and diagnostics' },
]

export default function SettingsPage() {
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'

  return (
    <SidePanelLayout
      title="Settings"
      tabs={TABS}
      footer={<span className="text-[12px] text-muted">KiroClaw v{version}</span>}
    >
      {tab => <>
        {tab === 'overview' && <OverviewPanel />}
        {tab === 'provider' && <ProviderPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'display' && <DisplayPanel />}
        {tab === 'browser' && <BrowserPanel />}
        {tab === 'security' && <SecurityPanel />}
        {tab === 'notifications' && <NotificationsPanel />}
        {tab === 'slack' && <SlackPanel />}
        {tab === 'developer' && <GeneralPanel />}
        {tab !== 'overview' && tab !== 'provider' && tab !== 'chat' && tab !== 'display' && tab !== 'browser' && tab !== 'security' && tab !== 'notifications' && tab !== 'slack' && tab !== 'developer' && (
          <div className="text-muted text-sm py-12 text-center">
            {TABS.find(t => t.key === tab)?.label} settings — coming soon
          </div>
        )}
      </>}
    </SidePanelLayout>
  )
}
