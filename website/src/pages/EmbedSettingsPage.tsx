import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Palette, MessageSquare, Cpu, Bell } from 'lucide-react'
import { useAppSelector } from '../store'
import { DisplayPanel } from './settings/DisplayPanel'
import { ChatPanel } from './settings/ChatPanel'
import { ProviderPanel } from './settings/ProviderPanel'
import { NotificationsPanel } from './settings/NotificationsPanel'

const TABS = [
  { key: 'display', label: 'Display', icon: <Palette size={14} /> },
  { key: 'chat', label: 'Chat', icon: <MessageSquare size={14} /> },
  { key: 'provider', label: 'Provider', icon: <Cpu size={14} /> },
  { key: 'notifications', label: 'Notifications', icon: <Bell size={14} /> },
]

export default function EmbedSettingsPage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState('display')
  const activeSlot = useAppSelector(s => s.chat.activeSlot)

  const goBack = () => {
    if (activeSlot) {
      navigate(`/embed/chat/${activeSlot}?sid=${activeSlot}`)
    } else {
      navigate('/embed/sessions')
    }
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header with back button */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border shrink-0">
        <button
          onClick={goBack}
          className="p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
          aria-label="Back"
        >
          <ArrowLeft size={16} />
        </button>
        <span className="text-sm font-medium">Settings</span>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 px-4 py-2 border-b border-border shrink-0 overflow-x-auto">
        {TABS.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
              tab === t.key
                ? 'bg-accent/10 text-accent'
                : 'text-muted hover:text-text hover:bg-bg-hover'
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* Panel content */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {tab === 'display' && <DisplayPanel />}
        {tab === 'chat' && <ChatPanel />}
        {tab === 'provider' && <ProviderPanel />}
        {tab === 'notifications' && <NotificationsPanel />}
      </div>
    </div>
  )
}
