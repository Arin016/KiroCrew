import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kiroclawConfig: () => Promise.resolve({}),
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ChatPanel settings – navPanelOpen toggle', () => {
  beforeEach(() => { localStorage.removeItem('mc-chat-config') })

  it('renders and toggles Navigation Panel setting', () => {
    wrap(<ChatPanel />)
    const label = screen.getByText('Navigation Panel')
    expect(label).toBeInTheDocument()
    // Click the toggle to exercise the onChange callback
    fireEvent.click(label)
    // After click, navPanelOpen should be saved as true
    const stored = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    expect(stored.navPanelOpen).toBe(true)
  })
})
