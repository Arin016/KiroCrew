import { useEffect, useRef, useState } from 'react'
import { Repeat, X } from 'lucide-react'

export interface AutoNudgeLoop {
  id: string
  slot_key: string
  message: string
  idle_secs: number
  max_cycles: number
  cycle_count: number
  active: boolean
  last_fire_ts: number
}

interface Props {
  slotKey: string
  anchorRect: DOMRect
  loop: AutoNudgeLoop | null
  onClose: () => void
  onChange: (loop: AutoNudgeLoop | null) => void
}

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

export default function AutoNudgePopover({ slotKey, anchorRect, loop, onClose, onChange }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [message, setMessage] = useState(loop?.message || DEFAULT_MSG)
  const [idleSecs, setIdleSecs] = useState(loop?.idle_secs || 60)
  const [maxCycles, setMaxCycles] = useState(loop?.max_cycles || 0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  async function save() {
    setSaving(true)
    setError('')
    try {
      const body = JSON.stringify({ slot_key: slotKey, message, idle_secs: idleSecs, max_cycles: maxCycles })
      const resp = loop
        ? await fetch(`/api/autonudge/${loop.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, idle_secs: idleSecs, max_cycles: maxCycles, active: true }) })
        : await fetch('/api/autonudge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      const data = await resp.json()
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      onChange(data.loop)
      onClose()
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setSaving(false)
    }
  }

  async function stop() {
    if (!loop) return
    setSaving(true)
    try {
      const resp = await fetch(`/api/autonudge/${loop.id}`, { method: 'DELETE' })
      if (!resp.ok) {
        // Parse JSON body for server-supplied error (e.g. 503 when feature disabled).
        // Only on error path: a successful DELETE may return 204 No Content.
        const data = await resp.json().catch(() => ({}))
        throw new Error(data.error || `HTTP ${resp.status}`)
      }
      onChange(null)
      onClose()
    } catch (e: any) {
      setError(e.message || String(e))
    } finally {
      setSaving(false)
    }
  }

  // Position: above the toolbar button, aligned left.
  const style: React.CSSProperties = {
    position: 'fixed',
    left: anchorRect.left,
    bottom: window.innerHeight - anchorRect.top + 6,
    zIndex: 100,
  }

  return (
    <div
      ref={ref}
      style={style}
      className="bg-chrome border border-border rounded-lg shadow-xl p-4 w-[420px] text-[12px]"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 font-medium text-text">
          <Repeat size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
          Auto-Nudge Loop
          {loop?.active && <span className="text-muted text-[11px]">· cycle {loop.cycle_count}</span>}
        </div>
        <button aria-label="Close" onClick={onClose} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
          <X size={14} />
        </button>
      </div>

      <div className="text-muted text-[11px] mb-1">Nudge message ({"{{…}}"} variables auto-filled)</div>
      <textarea
        value={message}
        onChange={e => setMessage(e.target.value)}
        rows={6}
        className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text"
        placeholder="What should the agent do when idle?"
      />

      <div className="flex gap-3 mb-3">
        <div className="flex-1">
          <div className="text-muted text-[11px] mb-1">Idle seconds before nudge</div>
          <input
            type="number"
            min={15}
            max={86400}
            value={idleSecs}
            onChange={e => setIdleSecs(parseInt(e.target.value) || 60)}
            className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
          />
        </div>
        <div className="flex-1">
          <div className="text-muted text-[11px] mb-1">Max cycles (0 = ∞)</div>
          <input
            type="number"
            min={0}
            value={maxCycles}
            onChange={e => setMaxCycles(parseInt(e.target.value) || 0)}
            className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
          />
        </div>
      </div>

      {loop && (
        <div className="text-muted text-[11px] mb-3">
          Last fire: {loop.last_fire_ts ? new Date(loop.last_fire_ts * 1000).toLocaleTimeString() : 'never'}
        </div>
      )}

      {error && <div className="text-danger text-[11px] mb-2">{error}</div>}

      <div className="flex gap-2 justify-end">
        {loop && (
          <button
            onClick={stop}
            disabled={saving}
            className="px-3 py-1 rounded border border-border text-muted hover:text-danger hover:border-danger bg-transparent cursor-pointer disabled:opacity-50"
          >
            Stop loop
          </button>
        )}
        <button
          onClick={save}
          disabled={saving || !message.trim()}
          className="px-3 py-1 rounded bg-accent text-white border-none cursor-pointer disabled:opacity-50 hover:bg-accent/90"
        >
          {loop ? 'Save' : 'Start loop'}
        </button>
      </div>
    </div>
  )
}
