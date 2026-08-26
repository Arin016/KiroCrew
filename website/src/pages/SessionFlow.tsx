import { useState } from 'react'

import { House, Bot } from 'lucide-react'

import { fmtNumber, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { SubagentActivity } from '../types'
import {
  type ContextTrace,
  buildColorMap,
  groupBlocks,
  rampShade,
  USER_LABEL,
} from './ContextBreakdownPanel'

/**
 * The Session Flow header for the Context Breakdown panel.
 *
 * The per-turn rows below answer "what filled THIS session's window, turn by
 * turn". They stop at the session boundary — a spawn is one `spawn_run` tool
 * call in the transcript and the sub-agent's own turns live in a DIFFERENT
 * session the panel never sees. This header restores the missing axis: the
 * agent -> sub-agent spawn tree, each node carrying not just what it did and how
 * long it took, but the SAME context-composition bar the rows use — so the
 * reader can see which node's window is filling with what.
 *
 * Pure and data-in: the main node's composition comes from the slot's own trace
 * `totals`; each sub-agent node comes from the live `SubagentActivity` the
 * Subagents panel already subscribes to (status, tool count, elapsed, model).
 * A sub-agent's own per-source breakdown lives in a different session's shards
 * and is not fetched here — the node shows its execution shape (tools/status/
 * time), and the composition bar is the MAIN session's, which is the window this
 * panel is scoped to.
 */

/** A sub-agent's coarse lifecycle, mapped onto the three states a reader acts on. */
type NodeStatus = 'running' | 'done' | 'error'

function nodeStatus(s: SubagentActivity['status']): NodeStatus {
  if (s === 'done') return 'done'
  if (s === 'error' || s === 'stopped') return 'error'
  return 'running' // pending | running | tool
}

const STATUS_STYLE: Record<NodeStatus, { bg: string; fg: string; key: string }> = {
  running: { bg: 'var(--warn-subtle)', fg: 'var(--warn)', key: 'pages.sessionFlow.status_running' },
  done: { bg: 'var(--ok-subtle)', fg: 'var(--ok)', key: 'pages.sessionFlow.status_done' },
  error: { bg: 'var(--danger-subtle)', fg: 'var(--danger)', key: 'pages.sessionFlow.status_error' },
}

const fmtN = (n: number): string => fmtNumber(Math.round(n))

/** Elapsed in a compact human unit. `elapsed` is milliseconds (the store folds
 *  it from the live counter); a running node's figure keeps ticking upstream so
 *  this only renders whatever it is handed. */
function fmtElapsed(ms: number): string {
  if (ms <= 0) return ''
  const secs = ms / 1000
  if (secs < 90) return fmtUnit(secs, 'second', { maximumFractionDigits: secs < 10 ? 1 : 0 })
  return fmtUnit(secs / 60, 'minute', { maximumFractionDigits: 1 })
}

interface MiniSeg {
  key: string
  label: string
  pct: number
  fill: string
  isUser?: boolean
}

/** Compose the main node's context bar from the slot's whole-session totals,
 *  ranked and coloured EXACTLY as the per-turn rows below (same `buildColorMap`
 *  and `groupBlocks`), so a block is one colour across the whole panel. */
export function mainNodeSegments(
  trace: ContextTrace,
  colorOf: (label: string) => { fill: string },
): MiniSeg[] {
  const grouped = groupBlocks(trace.totals)
  const total = Object.values(grouped).reduce((a, b) => a + b, 0)
  if (total <= 0) return []
  const nonUser = Object.entries(grouped)
    .filter(([label]) => label !== USER_LABEL)
    .sort((a, b) => b[1] - a[1])
  const segs: MiniSeg[] = nonUser.map(([label, chars]) => ({
    key: label,
    label,
    pct: (chars / total) * 100,
    fill: colorOf(label).fill,
  }))
  const userChars = grouped[USER_LABEL] ?? 0
  if (userChars > 0) {
    segs.push({
      key: USER_LABEL,
      label: USER_LABEL,
      pct: (userChars / total) * 100,
      fill: 'var(--accent)',
      isUser: true,
    })
  }
  return segs
}

function MiniBar({ segs }: { segs: MiniSeg[] }) {
  if (segs.length === 0) return null
  return (
    <div
      className="h-1.5 rounded-[3px] overflow-hidden flex mt-2"
      style={{ background: 'var(--bg-hover)' }}
    >
      {segs.map(seg => (
        <div
          key={seg.key}
          className="h-full"
          style={{ width: `${seg.pct}%`, background: seg.fill, minWidth: seg.isUser ? '2px' : undefined }}
        />
      ))}
    </div>
  )
}

/** One tool the node last ran / a running node's tool count — the execution
 *  shape a sub-agent node carries in place of a per-source breakdown it cannot
 *  supply from this session's shards. */
function NodeMeta({ node }: { node: SubagentActivity }) {
  const parts: string[] = []
  if (node.model) parts.push(node.model)
  if (typeof node.toolCount === 'number' && node.toolCount > 0) {
    parts.push(i18nT('pages.sessionFlow.tool_count', { count: fmtN(node.toolCount) }))
  }
  const el = fmtElapsed(node.elapsed)
  if (el) parts.push(el)
  if (parts.length === 0) return null
  return <div className="font-mono text-[10.5px] text-muted mt-1 truncate">{parts.join(' · ')}</div>
}

function StatusPill({ status }: { status: NodeStatus }) {
  const st = STATUS_STYLE[status]
  return (
    <span
      className="ml-auto shrink-0 text-[9.5px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full"
      style={{ background: st.bg, color: st.fg }}
    >
      {i18nT(st.key)}
    </span>
  )
}

/** The main (this-session) node: the agent that owns the panel, with its
 *  whole-window composition bar. */
function MainNode({ trace, segs }: { trace: ContextTrace; segs: MiniSeg[] }) {
  const model = trace.turns.find(t => t.model)?.model ?? ''
  return (
    <div
      className="rounded-xl px-3 py-2.5 border"
      style={{ borderColor: 'var(--accent)', background: 'var(--card)', boxShadow: '0 0 0 1px var(--accent-subtle)' }}
    >
      <div className="flex items-center gap-2">
        <span
          className="w-5 h-5 rounded-md grid place-items-center shrink-0"
          style={{ background: 'var(--accent)', color: 'var(--ctx-user-fg)' }}
        >
          <House size={12} aria-hidden="true" />
        </span>
        <span className="text-[12px] font-semibold text-text truncate">
          {i18nT('pages.sessionFlow.main_label')}
        </span>
        {model ? <span className="font-mono text-[10.5px] text-muted truncate">{model}</span> : null}
        <span className="ml-auto font-mono text-[10.5px] text-muted tabular-nums">
          {i18nT('pages.sessionFlow.turns_count', { count: fmtN(trace.turns.length) })}
        </span>
      </div>
      <MiniBar segs={segs} />
    </div>
  )
}

/** One sub-agent node, hung off the main node by a connector drawn in the
 *  parent. Carries execution shape (status / tools / elapsed) rather than a
 *  composition bar — see the file header. */
function SubNode({ node }: { node: SubagentActivity }) {
  const status = nodeStatus(node.status)
  return (
    <div
      className="rounded-xl px-3 py-2.5 border"
      style={{ borderColor: 'var(--border)', background: 'var(--card)' }}
    >
      <div className="flex items-center gap-2">
        <span
          className="w-5 h-5 rounded-md grid place-items-center shrink-0"
          style={{ background: 'var(--bg-hover)', color: 'var(--accent)', border: '1px solid var(--border)' }}
        >
          <Bot size={12} aria-hidden="true" />
        </span>
        <span className="text-[12px] font-semibold text-text truncate">{node.agent || i18nT('pages.sessionFlow.unknown_agent')}</span>
        {node.stalled ? (
          <span className="font-mono text-[10px]" style={{ color: 'var(--warn)' }}>
            {i18nT('pages.sessionFlow.stalled')}
          </span>
        ) : null}
        <StatusPill status={status} />
      </div>
      {node.task ? <div className="text-[11px] text-muted mt-1 line-clamp-2">{node.task}</div> : null}
      <NodeMeta node={node} />
    </div>
  )
}

/**
 * The flow graph. `subagents` is the live map the Subagents panel subscribes to
 * (order is not meaningful, so nodes are sorted by start time — the order they
 * were spawned, which is the order a reader scans a fan-out in). Renders nothing
 * when the session spawned nothing: a session with no sub-agents has no tree to
 * draw and the per-turn rows already tell its whole story.
 */
export function SessionFlow({
  trace,
  subagents,
}: {
  trace: ContextTrace | null | undefined
  subagents: Record<string, SubagentActivity>
}) {
  const [open, setOpen] = useState(true)
  const nodes = Object.values(subagents).sort((a, b) => a.startedAt - b.startedAt)
  if (!trace || nodes.length === 0) return null

  const colorMap = buildColorMap(trace.totals)
  const darkest = rampShade(1, 1)
  const colorOf = (label: string) => colorMap.get(label) ?? darkest
  const segs = mainNodeSegments(trace, colorOf)

  const running = nodes.filter(n => nodeStatus(n.status) === 'running').length

  return (
    <div className="border border-border bg-card rounded-xl overflow-hidden mb-3">
      <button
        className="w-full flex items-center justify-between gap-3 px-3.5 py-3 border-b border-border bg-[var(--bg-accent)] cursor-pointer bg-transparent"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <span className="text-[11.5px] font-semibold uppercase tracking-wide text-text">
          {i18nT('pages.sessionFlow.title')}
        </span>
        <span className="font-mono text-[11px] text-muted">
          {i18nT('pages.sessionFlow.header_meta', {
            subs: fmtN(nodes.length),
            running: fmtN(running),
          })}
        </span>
      </button>
      {open ? (
        // Vertical stack, not a fixed main+connector+column row: the panel is
        // resizable down to 320px (SIDE_PANEL_MIN_W) and a 190px main node plus a
        // 24px connector plus a sub-node column overflows there. The main node
        // takes the full width; sub-nodes sit beneath it, indented past a left
        // rail that reads as "spawned by the node above", and each is free to use
        // the whole remaining width at any panel size.
        <div className="p-3.5 flex flex-col gap-2">
          <MainNode trace={trace} segs={segs} />
          <div className="flex">
            {/* The spawn rail: a vertical line the sub-nodes hang off. */}
            <div className="w-3.5 shrink-0 relative" aria-hidden="true">
              <div
                className="absolute left-1.5 top-0 bottom-3 w-px"
                style={{ background: 'var(--border-strong)' }}
              />
            </div>
            <div className="flex-1 min-w-0 flex flex-col gap-2">
              {nodes.map(node => (
                <div key={node.id} className="relative">
                  {/* Horizontal stub from the rail into this node. */}
                  <div
                    className="absolute top-1/2 -left-3.5 w-3 h-px"
                    style={{ background: 'var(--border-strong)' }}
                    aria-hidden="true"
                  />
                  <SubNode node={node} />
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
