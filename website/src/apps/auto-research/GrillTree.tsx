import { useState } from 'react'
import { ChevronDown, ChevronRight, Check, Circle, Search, Plus, Trash2, HelpCircle, Loader2, ThumbsUp } from 'lucide-react'
import { GrillNode, GrillAction, nodeDepth } from './grillTreeModel'

const MAX_DEPTH = 4        // mirrors backend _MAX_GRILL_DEPTH
const SOFT_LIMIT = 25      // soft "tree getting large" advisory (no hard cap)

interface Props {
  tree: GrillNode[]
  dispatch: (action: GrillAction) => void
  // Calls the backend expand for a node; parent dispatches addChildren. Returns
  // {reason:'max_depth'} when the depth guard refuses.
  onExpand: (nodeId: string) => Promise<{ reason?: string } | void>
}

export default function GrillTree({ tree, dispatch, onExpand }: Props) {
  const [expandingIds, setExpandingIds] = useState<Set<string>>(new Set())
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [tried, setTried] = useState<Set<string>>(new Set())

  const live = tree.filter(n => n.status !== 'pruned')
  const childrenOf = (id: string | null) => live.filter(n => n.parent === id)

  const toggleCollapse = (id: string) =>
    setCollapsed(s => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })

  async function expand(id: string) {
    setExpandingIds(s => new Set(s).add(id))
    try {
      await onExpand(id)
    } finally {
      setExpandingIds(s => { const next = new Set(s); next.delete(id); return next })
      setTried(s => new Set(s).add(id))
    }
  }

  function whyPath(id: string): string {
    const byId = new Map(tree.map(n => [n.id, n]))
    const parts: string[] = []
    let cur = byId.get(id)
    while (cur && cur.parent) {
      const p = byId.get(cur.parent)
      if (p && p.kind === 'clarifier' && p.answer) parts.unshift(`${p.text} → ${p.answer}`)
      cur = p
    }
    return parts.length ? parts.join('  /  ') : 'top-level question'
  }

  function renderNode(node: GrillNode, depth: number) {
    const kids = childrenOf(node.id)
    const isCollapsed = collapsed.has(node.id)
    const atMaxDepth = nodeDepth(tree, node.id) >= MAX_DEPTH
    const spinning = expandingIds.has(node.id)
    const noResults = tried.has(node.id) && kids.length === 0 && !spinning

    return (
      <div key={node.id} style={{ marginLeft: depth ? 16 : 0 }}
           className={depth ? 'border-l border-dashed border-border pl-3 mt-1.5' : 'mt-1.5'}>
        <div className="flex items-start gap-1.5 text-sm">
          {kids.length > 0 ? (
            <button onClick={() => toggleCollapse(node.id)} className="text-muted mt-0.5" aria-label={isCollapsed ? 'Expand' : 'Collapse'}>
              {isCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
            </button>
          ) : <span className="w-[13px]" />}

          {node.kind === 'research' ? (
            <button onClick={() => dispatch({ type: 'togglePromote', id: node.id })}
                    className={node.status === 'promoted' ? 'text-ok mt-0.5' : 'text-muted mt-0.5'}
                    aria-label={node.status === 'promoted' ? 'Included' : 'Excluded'}>
              {node.status === 'promoted' ? <Check size={14} /> : <Circle size={14} />}
            </button>
          ) : <span className="text-warn mt-0.5">◆</span>}

          {node.kind === 'research' ? (
            <input className="flex-1 bg-transparent text-text border-b border-transparent focus:border-border outline-none"
                   value={node.text} onChange={e => dispatch({ type: 'edit', id: node.id, text: e.target.value })} />
          ) : <span className="flex-1 text-text">{node.text}</span>}

          <button onClick={() => dispatch({ type: 'prune', id: node.id })} className="text-danger mt-0.5" aria-label="Prune"><Trash2 size={12} /></button>
          <span title={whyPath(node.id)} className="text-muted mt-0.5 cursor-help"><HelpCircle size={12} /></span>
        </div>

        {node.kind === 'clarifier' && (
          <div className="ml-[34px] mt-1 text-xs text-muted flex items-center gap-2 flex-wrap">
            {node.recommended && <span>rec: <em className="text-text">{node.recommended}</em></span>}
            {node.status === 'answered' ? (
              <>
                <span className="text-text">answered: {node.answer}</span>
                <button onClick={() => expand(node.id)} disabled={atMaxDepth || spinning}
                        className="flex items-center gap-1 text-accent disabled:opacity-40">
                  {spinning ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} expand
                </button>
                {atMaxDepth && <span className="text-warn">max depth — add research manually or prune</span>}
              </>
            ) : (
              <>
                <input placeholder="answer…" defaultValue={node.answer}
                       className="bg-bg border border-border rounded px-1.5 py-0.5 text-text"
                       onKeyDown={e => { if (e.key === 'Enter') dispatch({ type: 'setAnswer', id: node.id, answer: e.currentTarget.value }) }} />
                <button onClick={() => dispatch({ type: 'accept', id: node.id })} className="flex items-center gap-1 text-accent"><ThumbsUp size={12} /> accept</button>
                <button onClick={() => dispatch({ type: 'investigateInstead', id: node.id })} className="flex items-center gap-1 text-accent"><Search size={12} /> investigate instead</button>
              </>
            )}
          </div>
        )}

        {noResults && <div className="ml-[34px] mt-1 text-xs text-muted">no suggestions — add manually or retry</div>}

        {!isCollapsed && kids.map(k => renderNode(k, depth + 1))}
      </div>
    )
  }

  const roots = childrenOf(null)
  if (roots.length === 0) return null

  return (
    <div>
      {live.length > SOFT_LIMIT && (
        <div className="text-xs text-warn mb-1">Tree is getting large — consider pruning branches you won't pursue.</div>
      )}
      {roots.map(n => renderNode(n, 0))}
    </div>
  )
}
