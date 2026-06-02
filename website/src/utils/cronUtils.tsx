/** Shared cron formatting utilities used by CronTab and SchedulePage */
import { Save, Plus } from 'lucide-react'
import type { CronJob } from '../types'

export const PY_TO_CRON = [1, 2, 3, 4, 5, 6, 0]
export const CRON_SEL = 'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring'
export const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
export const TH_CLS = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
export const TD_CLS = 'px-2.5 py-2 border-b border-border text-sm'

/** Render table header cells from column definitions */
export function renderThCells(cols: { h: string; w: string }[]) {
  return cols.map(c => <th key={c.h} className={`${TH_CLS} ${c.w}`}>{c.h}</th>)
}

export function fmtSchedule(j: CronJob): string {
  if (j.cron_expr) return j.cron_expr
  if (j.every) {
    const s = j.every
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.floor(s / 60)}m`
    if (s < 86400) return `${Math.floor(s / 3600)}h`
    return `${Math.floor(s / 86400)}d`
  }
  if (j.at) return new Date(j.at * 1000).toLocaleString()
  return '—'
}

const DOW_NAMES: Record<string, number> = { SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6 }

/** Resolve a single token (numeric or named) to a cron DOW number, or -1 if invalid */
function parseDowToken(t: string): number {
  if (t === '') return -1
  const named = DOW_NAMES[t.toUpperCase()]
  if (named !== undefined) return named
  if (isNaN(+t)) return -1
  return +t  // preserve raw value (0-7); caller normalizes with %7
}

/** Expand a cron dow field (e.g. "1-5", "MON-FRI", "0,6", "MON,WED,FRI") into an array of individual numbers */
export function expandDow(dow: string): number[] {
  return [...new Set(dow.split(',').flatMap(part => {
    const m = part.match(/^([A-Za-z0-9]+)-([A-Za-z0-9]+)$/)
    if (!m) { const v = parseDowToken(part); return v < 0 ? [] : [v % 7] }
    const start = parseDowToken(m[1]), end = parseDowToken(m[2])
    if (start < 0 || end < 0) return []
    const nums: number[] = []
    if (start > end) {
      for (let i = start; i <= 6; i++) nums.push(i % 7)
      for (let i = 0; i <= end; i++) nums.push(i % 7)
    } else {
      for (let i = start; i <= end; i++) nums.push(i % 7)
    }
    return nums
  }))]
}

export function fmtCron(expr: string): string {
  try {
    const p = expr.trim().split(/\s+/)
    if (p.length !== 5) return expr
    const [min, hr, dom, , dow] = p
    const NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
    const expanded = expandDow(dow)
    const days = dow === '*' ? 'daily' : expanded.length > 0 ? expanded.map(d => NAMES[d] || String(d)).join(',') : dow
    const domPart = dom !== '*' ? ` (days ${dom})` : ''
    return `${days} ${hr.padStart(2,'0')}:${min.padStart(2,'0')}${domPart}`
  } catch { return expr }
}

/** Save/Create button label with icon — shared by JobForm and SchedulePage */
export function SaveCreateLabel({ isEdit, saving }: { isEdit: boolean; saving: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      {isEdit ? <Save size={14} /> : <Plus size={14} />}
      {saving ? 'Saving...' : (isEdit ? 'Save' : 'Create')}
    </span>
  )
}
