import { useState } from 'react'
import { Gamepad2, X } from 'lucide-react'
import PixelCanvas, { type SlotData } from './PixelCanvas'
import type { ProjectRun } from '../types'

function slotsFromRun(run: ProjectRun): SlotData[] {
  const mapped: SlotData[] = []
  for (const t of run.task_details || []) {
    if (mapped.length >= 7) break
    if (t.status === 'pending') mapped.push({ state: 'empty', label: (t.title || '').slice(0, 20) })
    else if (t.status === 'in_progress') mapped.push({ state: 'typing', label: (t.title || '').slice(0, 20) })
    else if (t.status === 'reviewing') mapped.push({ state: 'typing', label: (t.title || '').slice(0, 20) })
  }
  while (mapped.length < 7) mapped.push({ state: 'empty' })
  return mapped
}

export default function PixelCanvasWidget({ run }: { run: ProjectRun }) {
  const [open, setOpen] = useState(false)
  const slots = slotsFromRun(run)
  const active = slots.filter(s => s.state !== 'empty').length
  const name = run.name || run.spec_name || 'Project'

  return (
    <>
      <button className="relative px-3 py-1.5 rounded-lg text-[13px] font-bold cursor-pointer border-none transition-all hover:scale-105" style={{ background: 'var(--card)', border: '2px solid var(--border)' }} onClick={() => setOpen(true)} title="Open workspace animation">
        <span className="text-lg"><Gamepad2 className="lucide-inline" /></span>
        {active > 0 && <span className="absolute -top-1.5 -right-1.5 flex h-4 min-w-4 items-center justify-center rounded-full text-[10px] font-bold text-accent-fg" style={{ background: 'var(--accent)' }}>{active}</span>}
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: 'rgba(0,0,0,.4)' }} onClick={() => setOpen(false)}>
          <div className="w-full max-w-2xl mx-4 rounded-xl shadow-lg overflow-hidden" style={{ background: 'var(--card)', border: '1px solid var(--border)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
              <span className="text-sm font-bold" style={{ color: 'var(--text-strong)' }}>{name} — Workspace</span>
              <div className="flex items-center gap-2">
                <span className="text-[12px]" style={{ color: 'var(--muted)' }}>{active} agent{active !== 1 ? 's' : ''}</span>
                <button className="text-lg cursor-pointer bg-transparent border-none font-body" style={{ color: 'var(--muted)' }} onClick={() => setOpen(false)}><X className="lucide-inline" /></button>
              </div>
            </div>
            <div className="p-4">
              <PixelCanvas slots={slots} />
            </div>
          </div>
        </div>
      )}
    </>
  )
}
