import { useState, useEffect, useCallback, useMemo, memo } from 'react'
import { Hourglass, ChevronUp, X, Zap } from 'lucide-react'
import { DndContext, closestCenter, DragOverlay, PointerSensor, useSensor, useSensors, type DragStartEvent, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, useSortable, verticalListSortingStrategy, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { ChatMessage } from '../types'

const CARD_H = 40

function msgId(m: ChatMessage, index?: number): string {
  return (m.meta?.queueId as string) ?? m.ts ?? (index != null ? `fallback-${index}` : '')
}

function SortableCard({ m, i, idx, onCancel, onInterrupt }: {
  m: ChatMessage; i: number; idx: number
  onCancel?: (id: string) => void; onInterrupt?: (id: string) => void
}) {
  const id = msgId(m, idx)
  const { setNodeRef, listeners, transform, transition, isDragging } = useSortable({ id })

  return (
    <div
      ref={setNodeRef}
      style={{
        height: CARD_H,
        transform: transform ? CSS.Transform.toString(transform) : undefined,
        transition: transition || undefined,
        opacity: isDragging ? 0.4 : 1,
        touchAction: 'none',
        userSelect: 'none',
      }}
      className="bg-warn border border-warn/20 px-3 py-2 text-[13px] text-warn-fg rounded-xl mb-1 cursor-grab active:cursor-grabbing"
      {...listeners}
    >
      <span className="flex items-center gap-1.5 h-full">
        <span className="shrink-0 text-[10px] font-mono opacity-50 w-4 text-center">{i}</span>
        <span className="truncate flex-1">{m.content}</span>
        {onInterrupt && id && (
          <button className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors text-white"
            title="Send now" aria-label="Send now"
            onPointerDown={e => e.stopPropagation()}
            onClick={e => { e.stopPropagation(); onInterrupt(id) }}>
            <Zap size={13} fill="currentColor" />
          </button>
        )}
        {onCancel && id && (
          <button className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors"
            title="Cancel" aria-label="Cancel queued message"
            onPointerDown={e => e.stopPropagation()}
            onClick={e => { e.stopPropagation(); onCancel(id) }}>
            <X size={13} />
          </button>
        )}
      </span>
    </div>
  )
}

function QueueStackInner({ messages, onCancel, onInterrupt, onReorder }: {
  messages: ChatMessage[]
  onCancel?: (id: string) => void
  onInterrupt?: (id: string) => void
  onReorder?: (order: string[]) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const isExpandable = messages.length > 1
  const showExpanded = expanded && isExpandable
  const [activeId, setActiveId] = useState<string | null>(null)

  useEffect(() => { if (!isExpandable) setExpanded(false) }, [isExpandable])

  const ids = useMemo(() => messages.map((m, i) => msgId(m, i)), [messages])
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

  const handleDragStart = useCallback((e: DragStartEvent) => setActiveId(e.active.id as string), [])
  const handleDragEnd = useCallback((e: DragEndEvent) => {
    setActiveId(null)
    const { active, over } = e
    if (!over || active.id === over.id || !onReorder) return
    const curr = messages.map((m, i) => msgId(m, i))
    const from = curr.indexOf(active.id as string)
    const to = curr.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    onReorder(arrayMove(curr, from, to))
  }, [messages, onReorder])

  if (!messages.length) return null

  // Collapsed: simple peek card
  if (!showExpanded) {
    return (
      <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <div
          className={`bg-warn border border-warn/20 px-3 py-2 text-[13px] text-warn-fg rounded-t-xl ${isExpandable ? 'cursor-pointer' : ''}`}
          style={{ height: CARD_H, marginBottom: -11 }}
          onClick={() => isExpandable && setExpanded(true)}
        >
          <span className="flex items-center gap-1.5 h-full">
            <span className="shrink-0 text-[10px] font-mono opacity-50 w-4 text-center">1</span>
            <span className="shrink-0 inline-flex animate-[hourglass-flip_3s_ease-in-out_infinite]"><Hourglass size={13} /></span>
            <span className="truncate flex-1">{messages[0].content}</span>
            {onInterrupt && !isExpandable && msgId(messages[0], 0) && (
              <button className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors text-white"
                title="Send now" aria-label="Send now"
                onClick={e => { e.stopPropagation(); onInterrupt(msgId(messages[0], 0)) }}>
                <Zap size={13} fill="currentColor" />
              </button>
            )}
            {onCancel && !isExpandable && msgId(messages[0], 0) && (
              <button className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors"
                title="Cancel" aria-label="Cancel queued message"
                onClick={e => { e.stopPropagation(); onCancel(msgId(messages[0], 0)) }}>
                <X size={13} />
              </button>
            )}
            {isExpandable && (
              <span className="shrink-0 flex items-center gap-1 text-[11px] opacity-70">
                {messages.length} queued <ChevronUp size={12} />
              </span>
            )}
          </span>
        </div>
      </div>
    )
  }

  // Expanded: dnd-kit sortable list
  const activeMsg = activeId ? messages.find((m, i) => msgId(m, i) === activeId) : null

  return (
    <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <DndContext sensors={sensors} collisionDetection={closestCenter}
        onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
        <SortableContext items={[...ids].reverse()} strategy={verticalListSortingStrategy}>
          {[...messages].reverse().map((m, vi) => {
            const num = messages.length - vi
            const origIdx = messages.length - 1 - vi
            return <SortableCard key={msgId(m, origIdx)} m={m} i={num} idx={origIdx} onCancel={onCancel} onInterrupt={onInterrupt} />
          })}
        </SortableContext>
        <DragOverlay>
          {activeMsg ? (
            <div className="bg-warn border border-warn/20 px-3 py-2 text-[13px] text-warn-fg rounded-xl shadow-lg"
              style={{ height: CARD_H }}>
              <span className="flex items-center gap-1.5 h-full">
                <span className="truncate flex-1">{activeMsg.content}</span>
              </span>
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
      <button className="w-full text-center text-[11px] text-warn-fg/70 hover:text-warn-fg py-1 cursor-pointer bg-transparent border-none"
        onClick={() => setExpanded(false)}>
        <ChevronUp size={12} className="inline rotate-180" /> collapse
      </button>
    </div>
  )
}

export default memo(QueueStackInner, (prev, next) =>
  prev.messages.length === next.messages.length &&
  prev.messages.every((m, i) => m === next.messages[i]) &&
  prev.onCancel === next.onCancel &&
  prev.onInterrupt === next.onInterrupt &&
  prev.onReorder === next.onReorder
)
