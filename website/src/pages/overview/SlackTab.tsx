import { useState, useEffect, useCallback, useRef } from 'react'
import { AlertTriangle, X, Mic, Hourglass, Package } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Badge, Btn, Toggle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'

interface SttStatus {
  enabled: boolean
  model: string
  available: boolean
  docker_mode: boolean
  models: Record<string, string>
  provider?: string
  streaming?: boolean
  install_step: string
  install_detail: string
  install_error: string
  prereqs: string[]
}

const STEP_LABELS: Record<string, string> = {
  starting: 'Starting…',
  checking: 'Finding Python…',
  installing_xcode: 'Installing Xcode CLI Tools…',
  installing_brew: 'Installing Homebrew…',
  installing_python: 'Installing Python…',
  installing_ffmpeg: 'Installing ffmpeg…',
  installing_whisper: 'Installing whisper (~1.6 GB)…',
  pulling: 'Pulling Docker image…',
  done: 'Done!',
  error: 'Failed',
}

export default function SlackTab() {
  const [stt, setStt] = useState<SttStatus | null>(null)
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(() => {
    api.sttConfig().then((s: SttStatus) => {
      setStt(s)
      if (s.install_error) setErr(s.install_error)
    }).catch(e => setErr(e.message))
  }, [])
  useEffect(() => { load() }, [load])

  // Poll during install
  const installing = stt?.install_step != null && !['idle', 'done', 'error'].includes(stt.install_step)
  useEffect(() => {
    if (installing && !pollRef.current) {
      pollRef.current = setInterval(() => {
        api.sttConfig().then((s: SttStatus) => {
          setStt(s)
          if (['done', 'error', 'idle'].includes(s.install_step)) {
            if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
            if (s.install_error) setErr(s.install_error)
          }
        }).catch(() => {})
      }, 2000)
    }
    return () => { if (pollRef.current && !installing) { clearInterval(pollRef.current); pollRef.current = null } }
  }, [installing])

  const toggle = async () => {
    if (!stt) return
    setSaving(true)
    try { setStt(await api.saveSttConfig({ enabled: !stt.enabled })) }
    catch (e: any) { setErr(e.message) }
    setSaving(false)
  }

  const setModel = async (model: string) => {
    setSaving(true)
    try { setStt(await api.saveSttConfig({ model })) }
    catch (e: any) { setErr(e.message) }
    setSaving(false)
  }

  const toggleStreaming = async () => {
    if (!stt) return
    setSaving(true)
    try { setStt(await api.saveSttConfig({ streaming: !stt.streaming })) }
    catch (e: any) { setErr(e.message) }
    setSaving(false)
  }

  const install = async () => {
    setErr('')
    // Fire and forget — polling picks up progress
    api.sttInstall().then(res => {
      if (res.ok && res.ffmpeg === false) {
        setErr('Whisper installed but ffmpeg is missing — voice transcription needs ffmpeg.')
      }
      load()
    }).catch(e => setErr(e.message))
    // Start polling immediately
    setStt(prev => prev ? { ...prev, install_step: 'starting' } : prev)
  }

  const ErrorBanner = err ? (
    <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
      <span><AlertTriangle className="lucide-inline" /></span><span className="text-sm text-danger">{err}</span>
      <button className="ml-auto text-muted hover:text-text text-sm cursor-pointer bg-transparent border-none" onClick={() => { setErr(''); load() }}><X className="lucide-inline" /></button>
    </div>
  ) : null

  if (!stt) return (
    <Card>
      {ErrorBanner}
      <div className="skeleton h-40 rounded" />
    </Card>
  )

  const stepLabel = STEP_LABELS[stt.install_step] || ''

  return (
    <Card>
      {ErrorBanner}
      <CardTitle><Mic className="lucide-inline" /> Speech-to-Text <InfoTip text="Transcribe voice memos sent in Slack using OpenAI Whisper. Runs locally — no data leaves your machine." /></CardTitle>

      <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
        <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
          <span className="text-muted">Enabled</span>
          <Toggle checked={stt.enabled} onChange={toggle} disabled={saving} label="Enabled" />
        </div>

        <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
          <span className="text-muted">Status</span>
          {stt.available ? <Badge variant="ok">ready</Badge> : <Badge variant="warn">not installed</Badge>}
        </div>

        <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
          <span className="text-muted">Model</span>
          <select value={stt.model} onChange={e => setModel(e.target.value)} disabled={saving}
            className="bg-bg-elevated border border-border rounded-md px-3 py-1.5 text-text text-[13px] font-body outline-none cursor-pointer transition-colors focus-ring">
            {Object.entries(stt.models).map(([name, size]) => (
              <option key={name} value={name}>{name} ({size})</option>
            ))}
          </select>
        </div>

        <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
          <span className="text-muted">Runtime</span>
          <span className="text-text font-mono text-[13px]">{stt.docker_mode ? 'Docker' : 'Native'}</span>
        </div>

        {stt.provider === 'transcribe' && (
          <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
            <span className="text-muted">
              Streaming <InfoTip text="Stream live partial transcripts into the dashboard input as you speak. Transcribe provider only." />
            </span>
            <Toggle checked={!!stt.streaming} onChange={toggleStreaming} disabled={saving} label="Streaming" />
          </div>
        )}
      </div>

      {/* Install section */}
      {!stt.available && (
        <div className="mt-4">
          {stt.prereqs?.length > 0 && !installing && (
            <div className="mb-3 bg-accent/10 border border-accent/20 rounded-lg p-3 animate-rise">
              <p className="text-sm text-text font-medium mb-2">Run these commands in your terminal first:</p>
              {stt.prereqs.map((cmd, i) => (
                <code key={i} className="block bg-bg-elevated rounded px-3 py-1.5 text-[13px] font-mono text-accent mb-1 select-all">{cmd}</code>
              ))}
              <p className="text-muted text-[13px] mt-2">Then click Install below.</p>
            </div>
          )}
          {installing ? (
            <div className="animate-rise">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[13px] animate-pulse"><Hourglass className="lucide-inline" /></span>
                <span className="text-sm text-text font-medium">{stepLabel}</span>
              </div>
              {stt.install_detail && (
                <p className="text-muted text-[13px] font-mono truncate">{stt.install_detail}</p>
              )}
              <div className="mt-2 h-1.5 bg-border rounded-full overflow-hidden">
                <div className="h-full bg-accent rounded-full transition-all duration-500 animate-pulse"
                  style={{ width: stt.install_step === 'checking' ? '10%' : stt.install_step === 'installing_xcode' ? '15%' : stt.install_step === 'installing_brew' ? '25%' : stt.install_step === 'installing_python' ? '35%' : stt.install_step === 'installing_ffmpeg' ? '50%' : stt.install_step === 'installing_whisper' || stt.install_step === 'pulling' ? '70%' : '5%' }} />
              </div>
            </div>
          ) : (
            <>
              <Btn onClick={install}>
                {stt.docker_mode ? <><Package className="lucide-inline" /> Pull Docker Image</> : <><Package className="lucide-inline" /> Install Whisper</>}
              </Btn>
              <p className="text-muted text-[13px] mt-2">
                {stt.docker_mode
                  ? 'Pulls python:3.11-slim for Docker-based transcription (AL2).'
                  : 'Installs openai-whisper + ffmpeg. Uses system python3 (≥ 3.10).'}
              </p>
            </>
          )}
        </div>
      )}
    </Card>
  )
}
