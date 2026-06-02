import { useState } from 'react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect } from '../../components/settings'
import {
  SOUND_PRESETS, type SoundPreset, type SoundCategory,
  loadSoundSettings, saveSoundSettings, playPreset,
} from '../../hooks/useNotificationSound'

const PRESET_OPTIONS: SoundPreset[] = ['none', ...SOUND_PRESETS]
const PRESET_LABELS: Record<SoundPreset, string> = {
  none: 'Silent', chime: 'Chime (C6-E6-G6)', ding: 'Ding', blip: 'Blip', pop: 'Pop',
}
const DEFAULT_SENTINEL = 'default'
const OVERRIDE_OPTIONS: string[] = [DEFAULT_SENTINEL, ...PRESET_OPTIONS]
const OVERRIDE_LABELS: string[] = ['Use default', ...PRESET_OPTIONS.map(p => PRESET_LABELS[p])]

const CATEGORY_ROWS: { key: SoundCategory; label: string; description: string }[] = [
  { key: 'all',        label: 'Default (all categories)', description: 'Fallback sound when no category-specific override is set' },
  { key: 'cron',       label: 'Cron',       description: 'Scheduled job completions' },
  { key: 'approval',   label: 'Approval',   description: 'Tool approval requests' },
  { key: 'hook',       label: 'Webhook',    description: 'External hook triggers' },
  { key: 'heartbeat',  label: 'Heartbeat',  description: 'Heartbeat task results' },
  { key: 'subagent',   label: 'Subagent',   description: 'Background subagent completions' },
  { key: 'taskrunner', label: 'Tasks',      description: 'Task runner completions' },
]

export function NotificationsPanel() {
  const [settings, setSettings] = useState(() => loadSoundSettings())

  const update = (partial: Partial<typeof settings>) => {
    const next = { ...settings, ...partial }
    setSettings(next)
    saveSoundSettings(next)
  }

  const setCategoryPreset = (cat: SoundCategory, preset: SoundPreset) => {
    update({ perCategory: { ...settings.perCategory, [cat]: preset } })
  }

  const clearCategoryOverride = (cat: SoundCategory) => {
    const { [cat]: _drop, ...rest } = settings.perCategory
    void _drop
    update({ perCategory: rest })
  }

  const fallback = settings.perCategory.all ?? 'chime'

  return (
    <>
      <SettingsSection title="Sound">
        <SettingsCard>
          <SettingsToggle
            label="Play sound on new notifications"
            checked={settings.enabled}
            onChange={v => update({ enabled: v })}
          />
          <div className="flex flex-col gap-1.5 py-1.5">
            <label htmlFor="mc-volume-slider" className="text-[13px] font-semibold text-text">Volume</label>
            <div className="text-[12px] text-muted">{Math.round(settings.volume * 100)}%</div>
            <input
              id="mc-volume-slider"
              type="range" min={0} max={100} step={5}
              value={Math.round(settings.volume * 100)}
              onChange={e => update({ volume: Number(e.target.value) / 100 })}
              disabled={!settings.enabled}
              className="w-full accent-[var(--accent)]"
            />
          </div>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Per-category sounds">
        <SettingsCard>
          {CATEGORY_ROWS.map(row => {
            const hasOverride = row.key !== 'all' && settings.perCategory[row.key] !== undefined
            const effective: SoundPreset = row.key === 'all'
              ? fallback
              : (settings.perCategory[row.key] ?? fallback)
            const selectValue: string = row.key === 'all'
              ? fallback
              : (hasOverride ? (settings.perCategory[row.key] as SoundPreset) : DEFAULT_SENTINEL)
            const opts = row.key === 'all' ? PRESET_OPTIONS : OVERRIDE_OPTIONS
            const optLabels = row.key === 'all'
              ? PRESET_OPTIONS.map(p => PRESET_LABELS[p])
              : OVERRIDE_LABELS
            return (
              <div key={row.key} className="flex items-end gap-2">
                <div className="flex-1 min-w-0">
                  <SettingsSelect
                    label={row.label}
                    description={row.description}
                    value={selectValue}
                    options={opts}
                    optionLabels={optLabels}
                    onChange={v => {
                      if (v === DEFAULT_SENTINEL) clearCategoryOverride(row.key)
                      else setCategoryPreset(row.key, v as SoundPreset)
                    }}
                    disabled={!settings.enabled}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => playPreset(effective, settings.volume)}
                  disabled={!settings.enabled || effective === 'none' || settings.volume === 0}
                  className="mb-2 px-3 py-1.5 rounded-md border border-border text-[12px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong disabled:opacity-40 disabled:cursor-not-allowed transition-all font-body"
                >
                  Test
                </button>
              </div>
            )
          })}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
