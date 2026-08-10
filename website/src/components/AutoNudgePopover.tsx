import { useEffect, useRef, useState } from 'react'
import { Goal, ShieldCheck, X } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { loadGoalDraft, saveGoalDraft, type GoalDraft } from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
export interface AutoNudgeLoop {
  id: string
  slot_key: string
  message: string
  idle_secs: number
  max_cycles: number
  cycle_count: number
  active: boolean
  last_fire_ts: number
  /** Seconds left on this run's auto-approve window; 0 when it holds none. */
  auto_approve_remaining_secs?: number
  /** The windows an operator may choose from, in seconds. */
  auto_approve_windows?: number[]
}

interface Props {
  slotKey: string
  loop: AutoNudgeLoop | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (loop: AutoNudgeLoop | null) => void
}

// Mirrors the server's offer so the control still renders if the field is absent
// (an older gateway, or a loop serialized before this shipped).
const DEFAULT_WINDOWS = [2 * 3600, 8 * 3600, 12 * 3600]

const ERROR_BY_CODE: Record<string, string> = {
  not_owner: 'components.autoNudgePopover.error_owner_only',
  window_not_offered: 'components.autoNudgePopover.error_window_not_offered',
  bad_window: 'components.autoNudgePopover.error_window_not_offered',
  loop_inactive: 'components.autoNudgePopover.error_run_not_running',
  not_found: 'components.autoNudgePopover.error_run_not_found',
  autonudge_disabled: 'components.autoNudgePopover.error_autonudge_disabled',
}

/** A catalog string for a server error code.
 *
 * `fallbackKey` names the ACTION that failed: telling someone their authorization
 * failed when they asked to revoke one is the wrong sentence on a security
 * control, so each caller supplies its own generic case.
 */
function errorForCode(
  code: unknown,
  fallbackKey = 'components.autoNudgePopover.authorize_failed',
): string {
  const key = typeof code === 'string' ? ERROR_BY_CODE[code] : undefined
  return i18nT(key || fallbackKey)
}

/** Whole hours when the window divides evenly, else hours + minutes. */
function fmtWindow(secs: number): string {
  if (secs < 0) return i18nT('components.autoNudgePopover.no_expiry')
  const totalMinutes = Math.max(0, Math.round(secs / 60))
  const h = Math.floor(totalMinutes / 60)
  const m = totalMinutes % 60
  if (h && m) return i18nT('components.autoNudgePopover.duration_hours_minutes', { hours: h, minutes: m })
  if (h) return i18nT('components.autoNudgePopover.duration_hours', { hours: h })
  return i18nT('components.autoNudgePopover.duration_minutes', { minutes: m })
}

const DEFAULT_MSG = `Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create {{STOP_FILE}}`

export default function AutoNudgePopover({ slotKey, loop, open, onOpenChange, onChange }: Props) {
  // `||` (not `??`) is deliberate on the loop tier: it preserves the fallback
  // so a loop with idle_secs/max_cycles of 0 or an empty message still shows
  // the 60 / 0 / default template rather than a bare 0 / "".
  const [message, setMessage] = useState(() => loop?.message || DEFAULT_MSG)
  // Idle-seconds and max-cycles are held as RAW STRINGS while the popover is
  // open so every edit (including a fully-cleared field or a transient "") is
  // allowed as-typed. Coercing to a number on each keystroke would snap a
  // backspaced-to-empty field straight back to its default and prevent removing
  // the leading digit. The string is parsed
  // into a number only when the field commits (blur / save); an empty or
  // unparseable value falls back to the field default — 60 idle, 0 cycles.
  const [idleInput, setIdleInput] = useState(() => String(loop?.idle_secs || 60))
  const [maxCyclesInput, setMaxCyclesInput] = useState(() => String(loop?.max_cycles || 0))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // '' | 'saved' | 'released' -- what the last save did to this run's window.
  const [saveNotice, setSaveNotice] = useState<'' | 'saved' | 'released'>('')

  const parseIdle = (s: string) => parseInt(s, 10) || 60
  const parseCycles = (s: string) => parseInt(s, 10) || 0

  // Only a genuine user edit should persist a draft. Seeding from the live loop
  // or restoring a remembered draft on open must NOT re-write the store (doing
  // so would reset the slot's TTL / LRU position on a mere view, and could
  // mirror a live loop's config into the user-draft store). `hasEdited` gates
  // the persist so it fires on real onChange edits only.
  const hasEdited = useRef(false)
  // Latest field values, kept current every render so the close-flush below
  // (which runs from a stable handler) can read them.
  const latest = useRef({ slotKey, message, idleInput, maxCyclesInput, loop })
  latest.current = { slotKey, message, idleInput, maxCyclesInput, loop }

  // Compute the draft to persist for the current field state, or null to drop
  // the slot: the blank / pristine-default case stores nothing so an emptied or
  // untouched popover never pins the template. (Only reached when no loop is
  // running — a live loop is authoritative and its config is never mirrored
  // into the user-draft store; persistence is skipped entirely while a loop is
  // present.)
  function draftToPersist(s: typeof latest.current): GoalDraft | null {
    const idleSecs = parseIdle(s.idleInput)
    const maxCycles = parseCycles(s.maxCyclesInput)
    const isPristineDefault = s.message === DEFAULT_MSG && idleSecs === 60 && maxCycles === 0
    return isPristineDefault ? null : { message: s.message, idleSecs, maxCycles }
  }

  // Seed/restore fields on each open (rising edge). A live loop is the
  // authoritative source; otherwise the last per-slot draft is restored.
  // One read seeds all three fields. Runs in an effect (not render) so the
  // render itself performs no storage read/write.
  useEffect(() => {
    if (!open) return
    hasEdited.current = false
    setError('')
    if (loop) {
      // `||` (not `??`) is deliberate: a loop with idle_secs/max_cycles of 0
      // or an empty message shows the 60 / 0 / default template.
      setMessage(loop.message || DEFAULT_MSG)
      setIdleInput(String(loop.idle_secs || 60))
      setMaxCyclesInput(String(loop.max_cycles || 0))
    } else {
      const remembered = loadGoalDraft(slotKey)
      setMessage(remembered ? remembered.message : DEFAULT_MSG)
      setIdleInput(String(remembered ? remembered.idleSecs : 60))
      setMaxCyclesInput(String(remembered ? remembered.maxCycles : 0))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open-edge seed only; loop/slotKey are read fresh each open
  }, [open])

  // Flush a pending debounced edit synchronously when the popover closes OR
  // unmounts while open, so edits within the last DRAFT_SAVE_DEBOUNCE_MS
  // window aren't lost. Effect cleanup covers both paths.
  useEffect(() => {
    if (!open) return
    return () => {
      if (!hasEdited.current || latest.current.loop) return
      saveGoalDraft(latest.current.slotKey, draftToPersist(latest.current))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stable cleanup reading the latest ref
  }, [open])

  // Persist edits per slot, debounced with the same DRAFT_SAVE_DEBOUNCE_MS as
  // chat drafts so a long goal doesn't drive a synchronous localStorage write on
  // every keystroke. Skips until the user actually edits a field (so opening the
  // popover or the open-restore setState above never writes).
  useEffect(() => {
    if (!open || !hasEdited.current || loop) return
    const timer = setTimeout(() => saveGoalDraft(slotKey, draftToPersist(latest.current)), DRAFT_SAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [open, slotKey, message, idleInput, maxCyclesInput, loop])

  async function save() {
    setSaving(true)
    setError('')
    try {
      // Parse from the raw strings here (not a committed number state) so a value
      // typed and then Save-clicked without an intervening blur is still captured.
      const idle_secs = parseIdle(idleInput)
      const max_cycles = parseCycles(maxCyclesInput)
      const body = JSON.stringify({ slot_key: slotKey, message, idle_secs, max_cycles })
      const resp = loop
        ? await fetch(`/api/autonudge/${loop.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, idle_secs, max_cycles, active: true }) })
        : await fetch('/api/autonudge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      const data = await resp.json()
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
      // Save can change AUTHORIZATION state as a side effect (the backend releases
      // the window when the goal is rewritten), so it owes the operator one
      // statement about what happened to it. Deciding that here -- once, from the
      // before/after pair -- rather than at each render site is what keeps this
      // seam from growing a third silent case.
      const hadWindow = Boolean(loop?.auto_approve_remaining_secs)
      const nowLive = Boolean(data.loop?.active)
      const hasWindow = Boolean(data.loop?.auto_approve_remaining_secs)
      const needsAuthorization = nowLive && !hasWindow
      setSaveNotice(hadWindow && !hasWindow ? 'released' : needsAuthorization ? 'saved' : '')
      onChange(data.loop)
      // Closing here is what made the offer undiscoverable at the one moment it
      // matters -- an operator arms an overnight run, the panel closes, and they
      // walk away into the stall this feature exists to prevent. Closing stays the
      // default for every other save, so the flow is unchanged once authorized.
      if (!needsAuthorization) onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // The window is not sent as a free number: the offer comes from the server and
  // the button posts one of its values back, so the UI cannot invent a duration
  // the backend never offered.
  async function authorize(windowSecs: number) {
    setSaveNotice('')
    if (!loop) return
    setSaving(true)
    setError('')
    try {
      const resp = await fetch(`/api/autonudge/${loop.id}/authorize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ window_secs: windowSecs }),
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        // Map the machine-readable code to a catalog string. `body.error` is
        // advisory English from the server, so showing it verbatim puts an
        // untranslated fragment ("owner only") in a localized UI.
        setError(errorForCode(body.code))
        return
      }
      // Reflect the granted window immediately rather than waiting for the next
      // poll: the operator just clicked it, and showing the offer again for a
      // second reads as the click not having landed.
      onChange({ ...loop, auto_approve_remaining_secs: windowSecs })
    } catch {
      setError(i18nT('components.autoNudgePopover.authorize_failed'))
    } finally {
      setSaving(false)
    }
  }

  // Revoking only moves the deadline earlier, so it needs no confirmation: the
  // risky direction is granting, and that already costs a deliberate click.
  async function revoke() {
    setSaveNotice('')
    if (!loop) return
    setSaving(true)
    setError('')
    try {
      const resp = await fetch(`/api/autonudge/${loop.id}/authorize`, { method: 'DELETE' })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        setError(errorForCode(body.code, 'components.autoNudgePopover.revoke_failed'))
        return
      }
      onChange({ ...loop, auto_approve_remaining_secs: 0 })
    } catch {
      setError(i18nT('components.autoNudgePopover.revoke_failed'))
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
      onOpenChange(false)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const remaining = loop?.auto_approve_remaining_secs || 0
  // A permanent grant reports -1; treat anything non-zero as "already covered"
  // so the offer is never shown next to a live window.
  const granted = remaining !== 0
  const windows = loop?.auto_approve_windows?.length ? loop.auto_approve_windows : DEFAULT_WINDOWS

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          className={`h-8 px-2 rounded-lg text-[12px] font-mono flex items-center gap-1 cursor-pointer transition-all bg-transparent border-none shrink-0 whitespace-nowrap ${
            loop?.active
              ? 'text-accent hover:text-accent hover:bg-accent/10 animate-pulse'
              : 'text-muted hover:text-text hover:bg-bg-hover'
          }`}
          title={loop?.active ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: loop.cycle_count }) : i18nT('components.autoNudgePopover.set_a_goal')}
          aria-label={loop?.active ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: loop.cycle_count }) : i18nT('components.autoNudgePopover.set_a_goal')}
        >
          <Goal size={16} className="shrink-0" />
          {loop?.active && loop.cycle_count > 0 ? loop.cycle_count : null}
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" align="start" className="w-[420px] p-4 text-[12px]">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 font-medium text-text">
            <Goal size={14} className={loop?.active ? 'text-accent' : 'text-muted'} />
            {i18nT('components.autoNudgePopover.set_a_goal')}
            {loop?.active && <span className="text-muted text-[11px]">{i18nT('components.autoNudgePopover.cycle')} {loop.cycle_count}</span>}
          </div>
          <button aria-label={i18nT('components.autoNudgePopover.close')} onClick={() => onOpenChange(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} />
          </button>
        </div>
        <p className="text-muted text-[11px] mb-3 leading-relaxed">{i18nT('components.autoNudgePopover.give_the_agent_a_goal_and_it_will_keep_working_t')}</p>

        <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.goal_description')}</div>
        <textarea
          aria-label={i18nT('components.autoNudgePopover.goal_description')}
          value={message}
          onChange={e => { hasEdited.current = true; setMessage(e.target.value) }}
          rows={6}
          className="w-full bg-bg border border-border rounded p-2 text-[12px] font-mono resize-y mb-3 text-text"
          placeholder={i18nT('components.autoNudgePopover.describe_what_you_want_the_agent_to_accomplish')}
        />

        <div className="flex gap-3 mb-3">
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.idle_seconds_before_nudge')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.idle_seconds_before_nudge')}
              min={15}
              max={86400}
              value={idleInput}
              onChange={e => { hasEdited.current = true; setIdleInput(e.target.value) }}
              onBlur={() => setIdleInput(String(parseIdle(idleInput)))}
              className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
            />
          </div>
          <div className="flex-1">
            <div className="text-muted text-[11px] mb-1">{i18nT('components.autoNudgePopover.max_cycles_0')}</div>
            <input
              type="number"
              aria-label={i18nT('components.autoNudgePopover.max_cycles_0_infinite')}
              min={0}
              value={maxCyclesInput}
              onChange={e => { hasEdited.current = true; setMaxCyclesInput(e.target.value) }}
              onBlur={() => setMaxCyclesInput(String(parseCycles(maxCyclesInput)))}
              className="w-full bg-bg border border-border rounded px-2 py-1 text-[12px] text-text"
            />
          </div>
        </div>

        {saveNotice && (
          <div
            className={
              saveNotice === 'released'
                ? 'text-danger text-[11px] mb-2 leading-relaxed'
                : 'text-muted text-[11px] mb-2'
            }
          >
            {saveNotice === 'released'
              ? i18nT('components.autoNudgePopover.auto_approve_released_by_goal_change')
              : i18nT('components.autoNudgePopover.goal_saved')}
          </div>
        )}
        {loop?.active && (
          <div className="border border-border rounded p-2 mb-3">
            <div className="flex items-center gap-1.5 text-text text-[11px] mb-1">
              <ShieldCheck size={12} className={granted ? 'text-accent' : 'text-muted'} />
              {i18nT('components.autoNudgePopover.auto_approve_for_this_run')}
            </div>
            {granted ? (
              <div className="text-muted text-[11px] leading-relaxed">
                {remaining < 0
                  ? i18nT('components.autoNudgePopover.auto_approve_has_no_expiry')
                  : i18nT('components.autoNudgePopover.auto_approve_ends_in', {
                      duration: fmtWindow(remaining),
                    })}
                <div className="mt-0.5">
                  {i18nT('components.autoNudgePopover.auto_approve_released_when_the_run_stops')}
                </div>
                <div className="mt-0.5">
                  {i18nT('components.autoNudgePopover.auto_approve_cleared_if_you_restart_the_goal')}
                </div>
                <button
                  onClick={revoke}
                  disabled={saving}
                  className="mt-1.5 px-2 py-1 rounded border border-border text-muted hover:text-danger hover:border-danger bg-transparent cursor-pointer disabled:opacity-50 text-[11px]"
                >
                  {i18nT('components.autoNudgePopover.revoke_auto_approve')}
                </button>
              </div>
            ) : (
              <>
                <p className="text-muted text-[11px] mb-2 leading-relaxed">
                  {i18nT('components.autoNudgePopover.authorize_this_run_for')}
                </p>
                <div className="flex gap-2">
                  {windows.map(w => (
                    <button
                      key={w}
                      onClick={() => authorize(w)}
                      disabled={saving}
                      aria-label={i18nT('components.autoNudgePopover.authorize_for_duration', {
                        duration: fmtWindow(w),
                      })}
                      className="px-2 py-1 rounded border border-border text-text hover:border-accent hover:text-accent bg-transparent cursor-pointer disabled:opacity-50 text-[11px]"
                    >
                      {fmtWindow(w)}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {loop && (
          <div className="text-muted text-[11px] mb-3">
            {i18nT('components.autoNudgePopover.last_fire')} {loop.last_fire_ts ? fmtTimeNumeric(loop.last_fire_ts) : i18nT('components.autoNudgePopover.never')}
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
              {i18nT('components.autoNudgePopover.stop_loop')}
            </button>
          )}
          <button
            onClick={save}
            disabled={saving || !message.trim()}
            className="px-3 py-1 rounded bg-accent text-accent-fg border-none cursor-pointer disabled:opacity-50 hover:bg-accent/90"
          >
            {loop ? i18nT('components.autoNudgePopover.save') : i18nT('components.autoNudgePopover.start_loop')}
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
