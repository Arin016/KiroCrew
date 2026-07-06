import { safeSetItem } from '../../utils/safeStorage'
import { useState } from 'react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'

const DEV_MODE_KEY = 'mc-dev-mode'
const DEV_MODE_EVENT = 'mc-dev-mode-changed'

export function GeneralPanel() {
  const [devMode, setDevMode] = useState(() => localStorage.getItem(DEV_MODE_KEY) === '1')

  const toggleDevMode = (v: boolean) => {
    safeSetItem(DEV_MODE_KEY, v ? '1' : '0')
    setDevMode(v)
    window.dispatchEvent(new CustomEvent(DEV_MODE_EVENT, { detail: v }))
    // Notify Electron main process to show/hide DevTools menu item
    ;(window as Window & { electronAPI?: { setDevMode?: (v: boolean) => void } }).electronAPI?.setDevMode?.(v)
  }

  return (
    <>
      <SettingsSection title="Developer Tools">
        <SettingsCard>
          <SettingsToggle
            label="Developer Mode"
            description="Show Developer page in sidebar with Logs, System metrics, and Memory internals"
            checked={devMode}
            onChange={toggleDevMode}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Updates">
        <SettingsCard>
          <SettingsToggle
            label="Beta Channel (Braveheart)"
            description="Receive early access updates from the braveheart branch. Coming soon."
            checked={false}
            onChange={() => {}}
            disabled
          />
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
