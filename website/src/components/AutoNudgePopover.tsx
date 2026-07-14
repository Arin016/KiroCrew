import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Goal, X } from 'lucide-react'
import { loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

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
  // Initial field values, resolved WITHOUT touching storage (the stored-draft
  // tier is applied in a mount effect below, not here). When a loop is running
  // its config is authoritative and the stored draft is never consulted. `||`
  // (not `??`) is deliberate on the loop tier: it preserves the pre-fix
  // fallback so a loop with idle_secs/max_cycles of 0 or an empty message still
  // shows the 60 / 0 / default template rather than a bare 0 / "".
  const [message, setMessage] = useState(() => loop?.message || DEFAULT_MSG)
  const [idleSecs, setIdleSecs] = useState(() => loop?.idle_secs || 60)
  const [maxCycles, setMaxCycles] = useState(() => loop?.max_cycles || 0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // Only a genuine user edit should persist a draft. Seeding from the live loop
  // or restoring a remembered draft on open must NOT re-write the store (doing
  // so would reset the slot's TTL / LRU position on a mere view, and could
  // mirror a live loop's config into the user-draft store). `hasEdited` gates
  // the persist so it fires on real onChange edits only.
  const hasEdited = useRef(false)
  // Latest field values, kept current every render so the unmount flush below
  // (a stable cleanup that captures only mount-time values) can read them.
  const latest = useRef({ slotKey, message, idleSecs, maxCycles, loop })
  latest.current = { slotKey, message, idleSecs, maxCycles, loop }

  // Compute the draft to persist for the current field state, or null to drop
  // the slot: the blank / pristine-default case stores nothing so an emptied or
  // untouched popover never pins the template. (Only reached when no loop is
  // running — a live loop is authoritative and its config is never mirrored
  // into the user-draft store; the effects below skip persistence entirely
  // while a loop is present.)
  function draftToPersist(s: typeof latest.current): GoalDraft | null {
    const isPristineDefault = s.message === DEFAULT_MSG && s.idleSecs === 60 && s.maxCycles === 0
    return isPristineDefault ? null : { message: s.message, idleSecs: s.idleSecs, maxCycles: s.maxCycles }
  }

  // Restore the last per-slot draft on open, but ONLY when no loop is running (a
  // live loop is the authoritative source and must not be overwritten by, or
  // seeded from, a stale draft). Runs once on mount in a layout effect — after
  // render — so the render itself performs no storage read/write (React 18
  // StrictMode double-invokes render-phase code, so a read there that persists
  // its TTL prune would be a purity violation). One read seeds all three fields.
  useLayoutEffect(() => {
    if (loop) return
    const remembered = loadGoalDraft(slotKey)
    if (!remembered) return
    setMessage(remembered.message)
    setIdleSecs(remembered.idleSecs)
    setMaxCycles(remembered.maxCycles)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only seed; the popover remounts per open so slotKey/loop are fixed for this instance
  }, [])

  // Persist edits per slot, debounced with the same DRAFT_SAVE_DEBOUNCE_MS as
  // chat drafts so a long goal doesn't drive a synchronous localStorage write on
  // every keystroke. Skips until the user actually edits a field (so opening the
  // popover or the mount-restore setState above never writes).
  useEffect(() => {
    if (!hasEdited.current || loop) return
    const timer = setTimeout(() => saveGoalDraft(slotKey, draftToPersist(latest.current)), DRAFT_SAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [slotKey, message, idleSecs, maxCycles, loop])

  // Flush a pending debounced edit synchronously on unmount (popover close) so
  // edits within the last DRAFT_SAVE_DEBOUNCE_MS window aren't lost. Stable
  // cleanup (empty deps) reading the `latest` ref.
  useEffect(() => {
    return () => {
      if (!hasEdited.current || latest.current.loop) return
      saveGoalDraft(latest.current.slotKey, draftToPersist(latest.current))
    }
  }, [])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && ref.current.contains(e.target as Node)) return
      // Ignore clicks on the anchor (goal) button so its own onClick can toggle
      // the popover closed — matches the approval/model dropdown behavior.
      if (anchorRect && e.clientX >= anchorRect.left && e.clientX <= anchorRect.right && e.clientY >= anchorRect.top && e.clientY <= anchorRect.bottom) return
      onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose, anchorRect])

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
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
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
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // Position: above the toolbar button, clamped to the viewport so the popover
  // never spills off the screen. Horizontal: keep the 420px panel on screen.
  // Vertical: it grows upward from just above the button, so cap its height to
  // the space above and scroll internally to keep the top edge on screen.
  const POPOVER_WIDTH = 420
  const MARGIN = 8
  const MIN_HEIGHT = 120
  const left = Math.max(MARGIN, Math.min(anchorRect.left, window.innerWidth - POPOVER_WIDTH - MARGIN))
  const style: React.CSSProperties = {
    position: 'fixed',
    left,
    bottom: window.innerHeight - anchorRect.top + 6,
    maxHeight: Math.max(MIN_HEIGHT, anchorRect.top - 6 - MARGIN),
    overflowY: 'auto',
    zIndex: 100,
  }

  return (
    <div
      ref={ref}
      style={style}
      className="bg-bg-elevated border border-border rounded-xl shadow-xl p-4 w-[420px] text-[12px] animate-slide-up"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 font-medium text-text">
          <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
          Set a goal
          {loop?.active && <span className="text-muted text-[11px]">· cycle {loop.cycle_count}</span>}
        </div>
        <button aria-label="Close" onClick={onClose} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
          <X size={14} />
        </button>
      </div>
      <p className="text-muted text-[11px] mb-3 leading-relaxed">Give the agent a goal and it will keep working toward it autonomously — nudging itself when idle until the goal is met or the cycle limit is reached.</p>

      <div className="text-muted text-[11px] mb-1">Goal description</div>
      <textarea
        aria-label="Goal description"
        value={message}
        onChange={e => { hasEdited.current = true; setMessage(e.target.value) }}
        rows={6}
        className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text"
        placeholder="Describe what you want the agent to accomplish…"
      />

      <div className="flex gap-3 mb-3">
        <div className="flex-1">
          <div className="text-muted text-[11px] mb-1">Idle seconds before nudge</div>
          <input
            type="number"
            aria-label="Idle seconds before nudge"
            min={15}
            max={86400}
            value={idleSecs}
            onChange={e => { hasEdited.current = true; setIdleSecs(parseInt(e.target.value) || 60) }}
            className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
          />
        </div>
        <div className="flex-1">
          <div className="text-muted text-[11px] mb-1">Max cycles (0 = ∞)</div>
          <input
            type="number"
            aria-label="Max cycles (0 = infinite)"
            min={0}
            value={maxCycles}
            onChange={e => { hasEdited.current = true; setMaxCycles(parseInt(e.target.value) || 0) }}
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
          className="px-3 py-1 rounded bg-accent text-accent-fg border-none cursor-pointer disabled:opacity-50 hover:bg-accent/90"
        >
          {loop ? 'Save' : 'Start loop'}
        </button>
      </div>
    </div>
  )
}
