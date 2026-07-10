import { Fragment, useEffect, useRef, type ReactNode } from 'react'
import type { GridNode, GridLeaf, GridSplit } from '../hooks/useSessionGrid'

const DIVIDER = 6 // px — draggable separator thickness between sibling panes

/**
 * SessionGridLayout — recursive "terminal split" renderer.
 *
 * Walks the split tree from useSessionGrid: a leaf renders via `renderLeaf`; a
 * split tiles its children along one axis (dir 'col' → left→right with vertical
 * dividers, dir 'row' → top→bottom with horizontal dividers) using flex-grow
 * ratios from the node's `sizes`. Each split owns its dividers, so dragging one
 * resizes only that split's two adjacent children (per-node resize, tmux style).
 * Layout-only: it never knows what a leaf contains.
 */
export default function SessionGridLayout({
  node,
  renderLeaf,
  onResize,
}: {
  node: GridNode
  renderLeaf: (leaf: GridLeaf) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
}) {
  if (node.type === 'leaf') {
    return <div className="h-full w-full min-w-0 min-h-0 overflow-hidden">{renderLeaf(node)}</div>
  }
  return <SplitContainer node={node} renderLeaf={renderLeaf} onResize={onResize} />
}

function SplitContainer({
  node,
  renderLeaf,
  onResize,
}: {
  node: GridSplit
  renderLeaf: (leaf: GridLeaf) => ReactNode
  onResize: (splitId: string, index: number, deltaFrac: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  // Teardown for an in-progress divider drag, invoked on unmount so closing a
  // pane mid-drag still removes the overlay + window listeners (no leak).
  const dragCleanupRef = useRef<(() => void) | null>(null)
  useEffect(() => () => dragCleanupRef.current?.(), [])

  const horizontal = node.dir === 'col' // children flow left→right

  const startDrag = (index: number, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const extent = horizontal ? rect.width : rect.height
    if (extent <= 0) return
    let last = horizontal ? e.clientX : e.clientY
    const cursor = horizontal ? 'col-resize' : 'row-resize'
    const overlay = document.createElement('div')
    overlay.style.cssText = `position:fixed;inset:0;z-index:9999;cursor:${cursor};`
    document.body.appendChild(overlay)
    const onMove = (ev: MouseEvent) => {
      const pos = horizontal ? ev.clientX : ev.clientY
      const d = (pos - last) / extent
      if (d !== 0) {
        onResize(node.id, index, d)
        last = pos
      }
    }
    const onUp = () => {
      overlay.remove()
      document.body.style.cursor = ''
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      dragCleanupRef.current = null
    }
    document.body.style.cursor = cursor
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    dragCleanupRef.current = onUp
  }

  return (
    <div
      ref={ref}
      className="flex h-full w-full min-w-0 min-h-0"
      style={{ flexDirection: horizontal ? 'row' : 'column' }}
    >
      {node.children.map((child, i) => (
        <Fragment key={child.id}>
          <div
            className="min-w-0 min-h-0 overflow-hidden"
            style={{ flexGrow: node.sizes[i] ?? 1, flexBasis: 0, flexShrink: 1 }}
          >
            <SessionGridLayout node={child} renderLeaf={renderLeaf} onResize={onResize} />
          </div>
          {i < node.children.length - 1 && (
            <div
              onMouseDown={(e) => startDrag(i, e)}
              className={`shrink-0 flex items-center justify-center group/div ${horizontal ? 'cursor-col-resize' : 'cursor-row-resize'}`}
              style={horizontal ? { width: DIVIDER } : { height: DIVIDER }}
              role="separator"
              aria-orientation={horizontal ? 'vertical' : 'horizontal'}
            >
              <div
                className={`bg-border group-hover/div:bg-accent transition-colors rounded-full ${horizontal ? 'w-[2px] h-full' : 'h-[2px] w-full'}`}
              />
            </div>
          )}
        </Fragment>
      ))}
    </div>
  )
}
