import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { loadChatConfig, saveChatConfig, type ChatConfig, type ContentWidth, type DashboardConfig, type SendMode } from '../chat/ChatSettings'
import { api } from '../../api/client'
import { isMac } from '../../utils/platform'

type VoiceConfig = {
  enabled: boolean; voice: string; engine: string; rate: string
  autoSpeak: boolean; aws_profile: string; region: string
}

type SttConfig = {
  enabled: boolean
  provider: string
  model: string
  available: boolean
  streaming: boolean
  transcribe_region: string
  transcribe_profile: string
  language_code: string
  models: Record<string, string>
  language_codes: string[]
}

const VOICE_OPTIONS_FALLBACK = [
  { value: 'Ruth', label: 'Ruth (US F)' },
  { value: 'Matthew', label: 'Matthew (US M)' },
  { value: 'Joanna', label: 'Joanna (US F)' },
  { value: 'Amy', label: 'Amy (UK F)' },
]

const ENGINE_OPTIONS = ['generative', 'neural', 'long-form', 'standard']
const SPEED_OPTIONS = ['80%', '90%', '95%', '100%', '110%', '120%', '130%', '150%']
const STT_PROVIDER_OPTIONS = ['whisper', 'transcribe']
const STT_PROVIDER_LABELS = ['Whisper (local)', 'Transcribe (AWS)']
// Fallback when the server hasn't responded yet. The authoritative list comes
// from the `/api/config/stt` GET response's `language_codes` field.
const STT_LANGUAGE_FALLBACK = ['en-US']
const NOTIF_OPTIONS = ['25', '50', '100', '200']
const RESTORE_OPTIONS = ['15', '30', '60', '120', '360', '720', '1440', '0']
const RESTORE_LABELS = ['15m', '30m', '1h', '2h', '6h', '12h', '24h', 'No limit']
const COMPACT_OPTIONS = ['20', '40', '60', '80', '90']
const COMPACT_LABELS = ['20% (aggressive)', '40%', '60%', '80%', '90% (default)']

const SOFT_STOP_MIN = 0.5
const SOFT_STOP_MAX = 60
const SOFT_STOP_DEFAULT = 10.0

type CompletionKeepMode = 'head' | 'tail' | 'both'
const COMPLETION_KEEP_OPTIONS: CompletionKeepMode[] = ['head', 'tail', 'both']
const COMPLETION_KEEP_LABELS = [
  'Head (preserve start of stream)',
  'Tail (preserve end / final summary)',
  'Both (head + tail with truncation marker)',
]
const COMPLETION_KEEP_CHARS_MIN = 0
// Mirrors RESULT_FILE_MAX_BYTES on the backend (handlers/core.py _EDITABLE_CONFIG).
const COMPLETION_KEEP_CHARS_MAX = 512000
const COMPLETION_KEEP_CHARS_DEFAULT = 3000

export function ChatPanel() {
  const qc = useQueryClient()
  const [chatCfg, setChatCfg] = useState<ChatConfig>(loadChatConfig)
  const [localProfile, setLocalProfile] = useState('')
  const [localRegion, setLocalRegion] = useState('')
  const [localSttProfile, setLocalSttProfile] = useState('')
  const [localSttRegion, setLocalSttRegion] = useState('')
  const [saveError, setSaveError] = useState('')

  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashCfg = dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, quick_send: false }

  const dashMut = useMutation({
    mutationFn: (next: DashboardConfig) => api.updateDashboardConfig(next),
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: ['dashboardConfig'] })
      const prev = qc.getQueryData<DashboardConfig>(['dashboardConfig'])
      qc.setQueryData(['dashboardConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['dashboardConfig'], ctx.prev)
      setSaveError('Failed to save dashboard config')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['dashboardConfig'] }),
  })

  // ── KiroClaw config (server-side) ──
  const mcQ = useQuery<{
    session?: { autocompact_pct?: number }
    agent?: {
      soft_stop_budget_secs?: number
      completion_keep?: CompletionKeepMode
      completion_keep_chars?: number
    }
  }>({
    queryKey: ['kiroclawConfig'],
    queryFn: () => api.kiroclawConfig(),
  })
  const mcCfg = mcQ.data

  const [localBudget, setLocalBudget] = useState('')
  const budgetInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !budgetInitRef.current) {
      budgetInitRef.current = true
      setLocalBudget(String(mcQ.data.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    }
  }, [mcQ.data])

  const budgetMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.soft_stop_budget_secs', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kiroclawConfig'] }),
    onError: () => {
      setSaveError('Failed to save soft-stop budget')
      // Revert the input to the last-known server value so the user isn't
      // left looking at an unpersisted number. budgetInitRef stays true,
      // so the init effect will not clobber this on future query updates.
      setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
    },
  })

  const [localKeepChars, setLocalKeepChars] = useState('')
  const keepCharsInitRef = useRef(false)
  useEffect(() => {
    if (mcQ.data && !keepCharsInitRef.current) {
      keepCharsInitRef.current = true
      setLocalKeepChars(String(mcQ.data.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT))
    }
  }, [mcQ.data])

  const keepCharsMut = useMutation({
    mutationFn: (n: number) => api.patchConfig('agent.completion_keep_chars', n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kiroclawConfig'] }),
    onError: () => {
      setSaveError('Failed to save completion-keep characters')
      setLocalKeepChars(
        String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
      )
    },
  })

  const keepModeMut = useMutation({
    mutationFn: (v: CompletionKeepMode) => api.patchConfig('agent.completion_keep', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kiroclawConfig'] }),
    onError: () => setSaveError('Failed to save completion-keep mode'),
  })

  // ── Voice config (server-side) ──
  const voiceQ = useQuery<VoiceConfig>({
    queryKey: ['voiceConfig'],
    queryFn: () => api.voiceConfig(),
  })

  type PollyVoice = { id: string; name: string; language: string; languageCode: string; gender: string; engines: string[] }
  const voicesQ = useQuery<{ voices: PollyVoice[] }>({
    queryKey: ['voiceVoices'],
    queryFn: () => api.voiceVoices(),
    staleTime: 3600_000,
  })

  const initializedRef = useRef(false)
  useEffect(() => {
    if (voiceQ.data && !initializedRef.current) {
      initializedRef.current = true
      setLocalProfile(voiceQ.data.aws_profile || '')
      setLocalRegion(voiceQ.data.region || '')
    }
  }, [voiceQ.data])
  const voiceCfg = voiceQ.data ?? { enabled: false, voice: 'Ruth', engine: 'generative', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }
  const voiceOptions = voicesQ.data?.voices
    ? voicesQ.data.voices.map(v => ({ value: v.id, label: `${v.name} (${v.languageCode} ${v.gender[0]})`, engines: v.engines }))
    : VOICE_OPTIONS_FALLBACK.map(v => ({ ...v, engines: ENGINE_OPTIONS }))
  const selectedVoiceEngines = voiceOptions.find(v => v.value === voiceCfg.voice)?.engines ?? ENGINE_OPTIONS

  const voiceMut = useMutation({
    mutationFn: (patch: Partial<VoiceConfig>) => api.updateVoiceConfig(patch),
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: ['voiceConfig'] })
      const prev = qc.getQueryData<VoiceConfig>(['voiceConfig'])
      if (prev) {
        const next = { ...prev, ...patch }
        qc.setQueryData(['voiceConfig'], next)
        window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: next }))
      }
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(['voiceConfig'], ctx.prev)
        setLocalProfile(ctx.prev.aws_profile || '')
        setLocalRegion(ctx.prev.region || '')
        window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: ctx.prev }))
      }
      setSaveError('Failed to save voice config')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['voiceConfig'] }),
  })

  // ── STT config (server-side) ──
  const sttQ = useQuery<SttConfig>({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig(),
  })

  const sttInitializedRef = useRef(false)
  useEffect(() => {
    if (sttQ.data && !sttInitializedRef.current) {
      sttInitializedRef.current = true
      setLocalSttProfile(sttQ.data.transcribe_profile || '')
      setLocalSttRegion(sttQ.data.transcribe_region || '')
    }
  }, [sttQ.data])

  const sttCfg: SttConfig = sttQ.data ?? {
    enabled: false, provider: 'whisper', model: 'turbo', available: false,
    streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US',
    models: {}, language_codes: STT_LANGUAGE_FALLBACK,
  }

  const sttMut = useMutation({
    mutationFn: (patch: Partial<SttConfig>) => api.saveSttConfig(patch),
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: ['sttConfig'] })
      const prev = qc.getQueryData<SttConfig>(['sttConfig'])
      if (prev) qc.setQueryData(['sttConfig'], { ...prev, ...patch })
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(['sttConfig'], ctx.prev)
        setLocalSttProfile(ctx.prev.transcribe_profile || '')
        setLocalSttRegion(ctx.prev.transcribe_region || '')
      }
      setSaveError('Failed to save STT config')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['sttConfig'] }),
  })

  // ── Local chat config (localStorage) ──
  const setChat = useCallback(<K extends keyof ChatConfig>(k: K, v: ChatConfig[K]) => {
    setChatCfg(prev => {
      const next = { ...prev, [k]: v }
      saveChatConfig(next)
      return next
    })
  }, [])

  const setDash = (patch: Partial<DashboardConfig>) => {
    dashMut.mutate({ ...dashCfg, ...patch })
  }

  const setVoice = (patch: Partial<VoiceConfig>) => {
    voiceMut.mutate(patch)
  }

  const setStt = (patch: Partial<SttConfig>) => {
    sttMut.mutate(patch)
  }

  const dashDisabled = !dashQ.isSuccess
  const voiceDisabled = !voiceQ.isSuccess
  const sttDisabled = !sttQ.isSuccess
  const languageOptions = sttCfg.language_codes?.length ? sttCfg.language_codes : STT_LANGUAGE_FALLBACK
  const isTranscribe = sttCfg.provider === 'transcribe'

  // Switching to transcribe enables streaming by default (one-click off if not wanted).
  const handleProviderChange = (v: string) => {
    if (v === 'transcribe' && !sttCfg.streaming) {
      setStt({ provider: v, streaming: true })
    } else {
      setStt({ provider: v })
    }
  }

  return (
    <>
      {saveError && (
        <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center justify-between animate-rise">
          <span className="text-[13px] text-danger">{saveError}</span>
          <button className="text-[13px] text-danger hover:text-text cursor-pointer bg-transparent border-none" onClick={() => setSaveError('')}>Dismiss</button>
        </div>
      )}

      <SettingsSection title="Sidebar">
        <SettingsCard>
          <SettingsToggle label="Navigation Panel" description="Show the link navigation panel when opening a session" checked={chatCfg.navPanelOpen} onChange={v => setChat('navPanelOpen', v)} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Behavior">
        <SettingsCard>
          <SettingsSelect
            label="Send shortcut"
            description={chatCfg.sendOnEnter === 'enter' ? 'Shift+Enter for newline' : chatCfg.sendOnEnter === 'ctrl-enter' ? 'Enter for newline' : `${isMac ? '⌘' : 'Ctrl'}+Enter for newline`}
            value={chatCfg.sendOnEnter}
            options={['enter', 'ctrl-enter', 'enter-ctrl-newline']}
            optionLabels={['Enter sends', `${isMac ? '⌘' : 'Ctrl'}+Enter sends`, `Enter sends, ${isMac ? '⌘' : 'Ctrl'}+Enter newline`]}
            onChange={v => setChat('sendOnEnter', v as SendMode)}
          />
          <SettingsToggle label="Quick Send" description={`Click a suggested reply to send it instantly. ${isMac ? '⇧' : 'Shift'}+Click to select multiple.`} checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} disabled={dashDisabled} />
          <SettingsToggle label="Show Timestamps" description="Display time on each message" checked={chatCfg.showTimestamps} onChange={v => setChat('showTimestamps', v)} />
          <SettingsToggle label="History Expanded" description="Expand history sidebar by default" checked={chatCfg.historyExpanded} onChange={v => setChat('historyExpanded', v)} />
          <SettingsSelect label="Notification Limit" description="Maximum notifications to display" value={String(chatCfg.notifLimit)} options={NOTIF_OPTIONS} onChange={v => setChat('notifLimit', Number(v))} />
          <SettingsButtonGroup label="Content Width" description="Compact is the original view. Comfortable and Full use more screen space." value={chatCfg.contentWidth} options={[{ value: "compact", label: "Compact" }, { value: "comfortable", label: "Comfortable" }, { value: "full", label: "Full" }]} onChange={v => setChat('contentWidth', v as ContentWidth)} />
          <SettingsToggle label="Show Thinking Inline" description="Show intermediate reasoning text between tool calls instead of collapsing everything" checked={!chatCfg.collapseAllSteps} onChange={v => setChat('collapseAllSteps', !v)} />
          {dashQ.isError && <div className="text-[13px] text-danger mb-2">Failed to load config. <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => dashQ.refetch()}>Retry</button></div>}
          <SettingsToggle label="Merge Queued Messages" description="Combine follow-up messages into a single labeled prompt while the agent is busy" checked={dashCfg.merge_queued_messages} onChange={v => setDash({ merge_queued_messages: v })} disabled={dashDisabled} />
          <SettingsSelect label="Widget Density" description="How aggressively the agent uses inline widgets for visual content" value={dashCfg.widget_density ?? 'more'} options={['more', 'less']} optionLabels={['More (encourage widgets)', 'Less (only when needed)']} onChange={v => setDash({ widget_density: v as 'more' | 'less' })} disabled={dashDisabled} />
          <SettingsToggle label="Confirm Before Closing Session" description="Show a confirmation dialog when closing a session from the sidebar" checked={chatCfg.confirmCloseSession} onChange={v => setChat('confirmCloseSession', v)} />
          <SettingsToggle label="Simplified Tool Call Names" description="When enabled, inline tool pills show simplified tool use purpose instead of the exact command being run" checked={chatCfg.simplifiedToolNames} onChange={v => setChat('simplifiedToolNames', v)} />
          <SettingsSelect label="File Change Chips" description="How file diff chips appear below assistant messages" value={chatCfg.fileChipStyle} options={['expanded', 'minimal']} optionLabels={['Expanded (icon + name + stats)', 'Minimal (stats only, name on hover)']} onChange={v => setChat('fileChipStyle', v as ChatConfig['fileChipStyle'])} />
          <SettingsButtonGroup label="Follow-Up Bar Layout" description="Multiline wraps suggestions onto multiple rows. Single line keeps them on one horizontally-scrollable row." value={chatCfg.followUpLayout} options={[{ value: "multiline", label: "Multiline" }, { value: "scroll", label: "Single line" }]} onChange={v => setChat('followUpLayout', v as ChatConfig['followUpLayout'])} />
          {mcQ.isError && (
            <div className="text-[13px] text-danger mb-2">
              Failed to load config.{' '}
              <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>Retry</button>
            </div>
          )}
          <SettingsInput
            label="Soft-stop budget (seconds)"
            aria-label="Soft-stop budget (seconds)"
            hint="How long to wait for the agent to honor a Stop press before forcefully killing the session. Longer budgets preserve session state more often but make stops feel laggy when agents are stuck in long tool calls."
            type="number"
            value={localBudget}
            min={SOFT_STOP_MIN}
            max={SOFT_STOP_MAX}
            step={0.5}
            onChange={setLocalBudget}
            onBlur={() => {
              const n = parseFloat(localBudget)
              if (isNaN(n) || n < SOFT_STOP_MIN || n > SOFT_STOP_MAX) {
                setLocalBudget(String(mcCfg?.agent?.soft_stop_budget_secs ?? SOFT_STOP_DEFAULT))
                return
              }
              budgetMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Startup">
        <SettingsCard>
          {dashQ.isError && <div className="text-[13px] text-danger mb-2">Failed to load config. <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => dashQ.refetch()}>Retry</button></div>}
          <SettingsToggle label="Restore Sessions" description="Re-open recently active sessions on startup" checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} disabled={dashDisabled} />
          {dashCfg.restore_sessions && (
            <SettingsSelect label="Restore Window" description="Time window for session restoration" value={String(dashCfg.restore_window_minutes)} options={RESTORE_OPTIONS} optionLabels={RESTORE_LABELS} onChange={v => setDash({ restore_window_minutes: Number(v) })} disabled={dashDisabled} />
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Context">
        <SettingsCard>
          {mcQ.isError && (
            <div className="text-[13px] text-danger mb-2">
              Failed to load config.{' '}
              <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>Retry</button>
            </div>
          )}
          <SettingsSelect
            label="Auto-Compact Threshold"
            description="Context usage % at which auto-compaction triggers. Lower = more frequent compaction, longer sessions"
            value={String(mcCfg?.session?.autocompact_pct ?? 90)}
            options={COMPACT_OPTIONS}
            optionLabels={COMPACT_LABELS}
            onChange={v =>
              api.patchConfig('session.autocompact_pct', Number(v))
                .then(() => qc.invalidateQueries({ queryKey: ['kiroclawConfig'] }))
                .catch(() => setSaveError('Failed to save auto-compact threshold'))
            }
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Subagents">
        <SettingsCard>
          {mcQ.isError && (
            <div className="text-[13px] text-danger mb-2">
              Failed to load config.{' '}
              <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => mcQ.refetch()}>Retry</button>
            </div>
          )}
          <SettingsSelect
            label="Completion Event Truncation"
            description="Which part of a subagent's stream to keep when injecting its completion event into the parent session. Head preserves the start (default, matches legacy behavior). Tail preserves the final summary. Both keeps a slice from each end with a marker between them."
            value={mcCfg?.agent?.completion_keep ?? 'head'}
            options={COMPLETION_KEEP_OPTIONS}
            optionLabels={COMPLETION_KEEP_LABELS}
            onChange={v => keepModeMut.mutate(v as CompletionKeepMode)}
            disabled={!mcQ.isSuccess}
          />
          <SettingsInput
            label="Completion Event Characters"
            aria-label="Completion event characters"
            hint={`Maximum characters retained in the completion event after applying the truncation mode. 0 disables truncation entirely. Default ${COMPLETION_KEEP_CHARS_DEFAULT}.`}
            type="number"
            value={localKeepChars}
            min={COMPLETION_KEEP_CHARS_MIN}
            max={COMPLETION_KEEP_CHARS_MAX}
            step={500}
            onChange={setLocalKeepChars}
            onBlur={() => {
              const n = parseInt(localKeepChars, 10)
              if (
                isNaN(n) ||
                n < COMPLETION_KEEP_CHARS_MIN ||
                n > COMPLETION_KEEP_CHARS_MAX
              ) {
                setLocalKeepChars(
                  String(mcCfg?.agent?.completion_keep_chars ?? COMPLETION_KEEP_CHARS_DEFAULT)
                )
                return
              }
              keepCharsMut.mutate(n)
            }}
            disabled={!mcQ.isSuccess}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Voice (TTS)">
        <SettingsCard>
          {voiceQ.isError && <div className="text-[13px] text-danger mb-2">Failed to load voice config. <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => voiceQ.refetch()}>Retry</button></div>}
          <SettingsToggle label="Auto-speak Responses" description="Speak every assistant reply automatically" checked={voiceCfg.autoSpeak} onChange={v => setVoice({ autoSpeak: v, ...(v ? { enabled: true } : {}) })} disabled={voiceDisabled} />
          <SettingsSelect label="Voice" description="AWS Polly voice for TTS" value={voiceCfg.voice} options={voiceOptions.map(o => o.value)} optionLabels={voiceOptions.map(o => o.label)} onChange={v => { const engines = voiceOptions.find(o => o.value === v)?.engines ?? ENGINE_OPTIONS; const patch: Partial<VoiceConfig> = { voice: v }; if (!engines.includes(voiceCfg.engine)) patch.engine = engines[0]; setVoice(patch) }} disabled={voiceDisabled} />
          <SettingsSelect label="Engine" description="Polly engine type" value={voiceCfg.engine} options={selectedVoiceEngines} onChange={v => setVoice({ engine: v })} disabled={voiceDisabled} />
          <SettingsSelect label="Speed" description="Speech rate" value={voiceCfg.rate} options={SPEED_OPTIONS} onChange={v => setVoice({ rate: v })} disabled={voiceDisabled} />
          <SettingsInput label="AWS Profile" description="AWS credentials profile for Polly" value={localProfile} onChange={setLocalProfile} onBlur={() => setVoice({ aws_profile: localProfile.trim() })} placeholder="default" disabled={voiceDisabled} />
          <SettingsInput label="AWS Region" description="AWS region for Polly API" value={localRegion} onChange={setLocalRegion} onBlur={() => setVoice({ region: localRegion.trim() })} placeholder="us-east-1" disabled={voiceDisabled} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Voice (STT)">
        <SettingsCard>
          {sttQ.isError && <div className="text-[13px] text-danger mb-2">Failed to load STT config. <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => sttQ.refetch()}>Retry</button></div>}
          <SettingsToggle label="Enabled" description="Transcribe voice memos into the input box when you click the mic" checked={sttCfg.enabled} onChange={v => setStt({ enabled: v })} disabled={sttDisabled} />
          <SettingsSelect label="Provider" description="Whisper runs locally; Transcribe calls AWS" value={sttCfg.provider} options={STT_PROVIDER_OPTIONS} optionLabels={STT_PROVIDER_LABELS} onChange={handleProviderChange} disabled={sttDisabled} />
          <SettingsToggle label="Streaming" description="Stream live partial transcripts into the input box as you speak (Transcribe only)" checked={sttCfg.streaming} onChange={v => setStt({ streaming: v })} disabled={sttDisabled || !isTranscribe} />
          <SettingsSelect label="Language" description="BCP-47 language code for speech recognition" value={sttCfg.language_code} options={languageOptions} onChange={v => setStt({ language_code: v })} disabled={sttDisabled} />
          {isTranscribe && (
            <>
              <SettingsInput label="AWS Profile" description="AWS credentials profile for Transcribe (blank = default chain)" value={localSttProfile} onChange={setLocalSttProfile} onBlur={() => setStt({ transcribe_profile: localSttProfile.trim() })} placeholder="default" disabled={sttDisabled} />
              <SettingsInput label="AWS Region" description="AWS region for Transcribe" value={localSttRegion} onChange={setLocalSttRegion} onBlur={() => setStt({ transcribe_region: localSttRegion.trim() })} placeholder="us-east-1" disabled={sttDisabled} />
            </>
          )}
          <div className="text-muted text-[12px] mt-1">Whisper install and model selection live under Overview → Slack.</div>
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
