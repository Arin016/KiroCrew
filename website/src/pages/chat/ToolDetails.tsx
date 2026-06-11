/**
 * Shared inline detail panel for tool calls. Used in two places:
 *
 *   1. Inline pill ({@link ../chat/ToolCallLine}) — expanded below the pill
 *      button when the user clicks it or when the tool is awaiting approval.
 *   2. Approval bar ghost ({@link ../../components/ChatInput}) — rendered
 *      above the always-visible button row when the inline pill has scrolled
 *      out of viewport, so the user never loses sight of what the tool is
 *      about to do.
 *
 * Keeping the renderer in one place guarantees both surfaces show identical
 * details — meta row chips, segmented Input/Output toggle, preformatted
 * payload blocks — with no risk of drift.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence, LayoutGroup } from 'framer-motion'
import { Zap, Wrench } from 'lucide-react'
import { ToolInputText } from '../../components/ToolInputText'

export function ToolDetails({ purpose, pillLabel, toolName, input, output, auto, pending, ts, hasEntry, fmtTime, barColor, layoutId, compact }: {
  purpose: string
  /** What the pill itself displays. The meta row hides the `→ purpose` line
   *  when it would just duplicate the pill text — happens when
   *  `simplifiedToolNames` is on and the pill is already showing the purpose. */
  pillLabel: string
  /** Raw tool name (e.g. the underlying tool identifier). Rendered as a chip in
   *  the meta row whenever the pill is showing something else (the purpose),
   *  so an expanded tool call always reveals which tool actually ran. Hidden
   *  when it would duplicate the pill label. */
  toolName?: string
  input: string; output: string; auto: boolean; pending: boolean; ts: number; hasEntry: boolean
  fmtTime: (t: number) => string
  /** Full CSS color value for the left rail (typically a `color-mix(...)` of
   *  a theme variable). Mirrors the pill icon's status colour so the panel
   *  visually chains off the pill. */
  barColor: string
  /** Stable per-pill id so framer-motion's segmented-control pill animates within
   *  this instance only — sharing the layoutId across pills would cause the
   *  active highlight to fly between unrelated tool calls. */
  layoutId: string
  /** When true, render in a tighter layout: single-line purpose clamp and
   *  smaller pre max-height. Used by the approval bar's ghost mirror so an
   *  unusually large input doesn't make the ghost surface dominate the
   *  screen — users can hit "Show in chat" to see the inline pill's full
   *  view if details get cut off. */
  compact?: boolean
}) {
  const hasInput = !!input
  const hasOutput = !!output
  // Default: prefer Output if present, else Input. Tracks user intent so we
  // don't yank focus away from a section the user explicitly opened.
  const [section, setSection] = useState<'input' | 'output'>(hasOutput ? 'output' : 'input')
  const userPickedRef = useRef(false)
  const onSectionChange = useCallback((s: 'input' | 'output') => {
    userPickedRef.current = true
    setSection(s)
  }, [])
  // Live tools: output arrives after input. Auto-promote to Output when it
  // first becomes available (unless the user has explicitly stayed on Input).
  useEffect(() => {
    if (hasOutput && !userPickedRef.current && section !== 'output') setSection('output')
  }, [hasOutput, section])

  const empty = !purpose && !hasInput && !hasOutput
  // Active section guarded against picking a disabled segment (e.g. user clicked
  // Output earlier, then re-opened a tool that hadn't received output yet).
  const active: 'input' | 'output' =
    section === 'output' && !hasOutput ? 'input' :
    section === 'input' && !hasInput ? 'output' : section
  // Only show the purpose line when it adds info the pill isn't already showing.
  const showPurpose = !!purpose && purpose.trim() !== pillLabel.trim()
  // Show the raw tool name when the pill is displaying something else (the
  // purpose, under simplifiedToolNames) — otherwise the expanded panel would
  // never reveal which tool actually ran. Suppressed when it duplicates the pill.
  const showToolName = !!toolName && toolName.trim() !== pillLabel.trim()
  // Recompute `empty` against the actual render predicates: the empty hint
  // should appear whenever the meta row, I/O blocks, and purpose line are all
  // suppressed. Without this, a historical tool whose only persisted meta is
  // a `purpose` that dedups against the pill label would render an empty
  // colored bar with no content (purpose is truthy → `empty` was false, but
  // `showPurpose` is false → meta row hidden, no I/O → blocks hidden).
  const reallyEmpty = (empty && !showToolName) || (!showPurpose && !showToolName && !hasInput && !hasOutput && !auto && !pending && ts === 0)

  return (
    <div className="ml-3 mt-1 mb-2 border-l-2 pl-3 flex flex-col gap-2" style={{ borderLeftColor: barColor }}>
      {(auto || pending || ts > 0 || showToolName || showPurpose || hasInput || hasOutput) && (
        <div className="flex items-end gap-2 flex-wrap">
          {showToolName && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-text text-[11px] font-mono">
              <Wrench size={10} className="text-muted shrink-0" /> {toolName}
            </span>
          )}
          {ts > 0 && <span className="inline-flex items-center px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-muted text-[11px] font-mono">{fmtTime(ts)}</span>}
          {pending && (
            <span
              className="inline-flex items-center px-2 py-0.5 rounded-md text-[11px] font-mono"
              style={{
                color: 'var(--warn)',
                backgroundColor: 'color-mix(in srgb, var(--warn) 8%, transparent)',
                border: '1px solid color-mix(in srgb, var(--warn) 30%, transparent)',
              }}
            >
              Waiting for approval
            </span>
          )}
          {auto && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-muted text-[11px] font-mono"><Zap size={10} /> Auto</span>}
          {showPurpose && <span className={`text-[12px] text-muted/50 break-words min-w-0 ${compact ? 'line-clamp-1' : ''}`}>→ {purpose}</span>}
          {(compact ? (hasInput && hasOutput) : (hasInput || hasOutput)) && (
            <div className="ml-auto shrink-0">
              <ToolSegmented
                active={active}
                hasInput={hasInput}
                hasOutput={hasOutput}
                onChange={onSectionChange}
                layoutId={layoutId}
              />
            </div>
          )}
        </div>
      )}
      {(hasInput || hasOutput) && (
        <AnimatePresence mode="wait" initial={false}>
          {active === 'input' && hasInput && (
            <motion.div
              key="input"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18, ease: [0.4, 0, 0.2, 1] }}
              className="overflow-hidden"
            >
              <pre className={`px-2.5 py-2 bg-bg-elevated rounded-md text-[12px] font-mono whitespace-pre-wrap break-all ${compact ? 'max-h-[160px]' : 'max-h-[400px]'} overflow-y-auto leading-relaxed border border-border`}><ToolInputText text={input} /></pre>
            </motion.div>
          )}
          {active === 'output' && hasOutput && (
            <motion.div
              key="output"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18, ease: [0.4, 0, 0.2, 1] }}
              className="overflow-hidden"
            >
              <pre className={`px-2.5 py-2 bg-bg-elevated rounded-md text-[12px] font-mono whitespace-pre-wrap break-all ${compact ? 'max-h-[160px]' : 'max-h-[500px]'} overflow-y-auto leading-relaxed border border-border`}><ToolInputText text={output} /></pre>
            </motion.div>
          )}
        </AnimatePresence>
      )}
      {reallyEmpty && (
        <div className="text-[12px] text-muted/60 italic">
          {hasEntry ? 'No input or output captured for this tool call.' : 'Details unavailable for historical tool calls.'}
        </div>
      )}
    </div>
  )
}

/** Two-segment toggle styled to match the Activity sidebar's `SegmentedControl`
 *  full mode (border + bg-elevated capsule, accent text + bg-card pill on the
 *  active segment via a shared `layoutId` framer animation). Unavailable
 *  segments render in a disabled state (dimmed, no hover, no click). We render
 *  this inline rather than reusing `SegmentedControl` because its adaptive
 *  collapse measures parent width — and our `shrink-0` wrapper kept forcing
 *  it into dropdown mode. We always want the full pill here. */
function ToolSegmented({ active, hasInput, hasOutput, onChange, layoutId }: {
  active: 'input' | 'output'
  hasInput: boolean
  hasOutput: boolean
  onChange: (s: 'input' | 'output') => void
  layoutId: string
}) {
  const segments: { key: 'input' | 'output'; label: string; enabled: boolean }[] = [
    { key: 'input', label: 'Input', enabled: hasInput },
    { key: 'output', label: 'Output', enabled: hasOutput },
  ]
  return (
    <LayoutGroup id={layoutId}>
      <div className="glass-surface glass-static inline-flex rounded-lg p-0.5 gap-0.5">
        {segments.map(s => {
          const isActive = s.key === active
          const disabled = !s.enabled
          return (
            <motion.button
              key={s.key}
              layout
              type="button"
              disabled={disabled}
              title={disabled ? `${s.label} not yet available` : s.label}
              aria-disabled={disabled || undefined}
              onClick={() => { if (!disabled) onChange(s.key) }}
              whileTap={isActive && !disabled ? { scale: 0.95 } : undefined}
              transition={{ duration: 0.15 }}
              className={`relative flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] font-medium border-none transition-colors z-[1] ${
                disabled
                  ? 'text-muted/30 cursor-not-allowed bg-transparent'
                  : isActive
                    ? 'text-accent cursor-pointer'
                    : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'
              }`}
            >
              {isActive && !disabled && (
                <motion.div
                  layoutId={`${layoutId}-indicator`}
                  className="glass-surface glass-static absolute inset-0 rounded-md"
                  transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                />
              )}
              <span className="relative z-[1]">{s.label}</span>
            </motion.button>
          )
        })}
      </div>
    </LayoutGroup>
  )
}
