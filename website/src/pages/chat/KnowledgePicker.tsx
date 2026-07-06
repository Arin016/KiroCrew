import { useState, useEffect } from 'react'
import { Brain, X, Loader2, Type, Network } from 'lucide-react'
import type { KnowledgeResult } from './useKnowledgeFetch'

interface Props {
  results: KnowledgeResult[]
  query: string
  loading: boolean
  onInject: (selected: KnowledgeResult[]) => void
  onSkip: () => void
}

function MatchIcon({ type }: { type: string }) {
  if (type.includes('vector')) return <Brain size={12} className="text-accent" />
  if (type.includes('graph')) return <Network size={12} className="text-accent" />
  return <Type size={12} className="text-muted" />
}

export function KnowledgePicker({ results, query, loading, onInject, onSkip }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // Update pre-selection when results change (fixes useState initializer bug)
  useEffect(() => {
    setSelected(new Set(results.slice(0, 1).map(r => r.id)))
  }, [results])

  const toggle = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  if (loading) {
    return (
      <div className="border border-border rounded-lg p-4 mb-3 animate-pulse">
        <div className="flex items-center gap-2 text-muted text-sm">
          <Loader2 size={14} className="animate-spin" /> Searching knowledge for &ldquo;{query}&rdquo;&hellip;
        </div>
      </div>
    )
  }

  if (!results.length) {
    return (
      <div className="border border-border rounded-lg p-4 mb-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted">No knowledge found for &ldquo;{query}&rdquo;</span>
          <button onClick={onSkip} className="text-[13px] text-accent bg-transparent border-none cursor-pointer">Dismiss</button>
        </div>
      </div>
    )
  }

  const totalTokens = results.filter(r => selected.has(r.id)).reduce((sum, r) => sum + r.tokens, 0)

  return (
    <div className="border border-border rounded-lg p-4 mb-3 bg-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-sm font-medium text-text">
          <Brain size={14} className="text-accent" />
          Knowledge results for &ldquo;{query}&rdquo;
        </div>
        <button onClick={onSkip} className="text-muted hover:text-text bg-transparent border-none cursor-pointer" aria-label="Dismiss knowledge results">
          <X size={14} />
        </button>
      </div>

      <div className="space-y-2 mb-3">
        {results.map(r => (
          <label key={r.id} htmlFor={`knowledge-${r.id}`} className="flex items-start gap-2 p-2 rounded border border-border hover:border-accent/50 cursor-pointer transition-colors">
            <input
              id={`knowledge-${r.id}`}
              aria-label={r.title}
              type="checkbox"
              checked={selected.has(r.id)}
              onChange={() => toggle(r.id)}
              className="mt-1 accent-accent shrink-0"
            />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-medium text-text truncate">{r.title}</div>
              <div className="text-[11px] text-muted mt-0.5 line-clamp-2">{r.summary}</div>
              <div className="flex items-center gap-2 mt-1 text-[10px] text-muted">
                <span className="flex items-center gap-0.5"><MatchIcon type={r.match_type} /> {r.match_type}</span>
                <span>{r.tokens} tokens</span>
                {r.source && <span className="truncate max-w-[150px]">{r.source}</span>}
              </div>
            </div>
          </label>
        ))}
      </div>

      <div className="flex items-center justify-between">
        <span className="text-[11px] text-muted">{selected.size} selected · {totalTokens} tokens</span>
        <div className="flex gap-2">
          <button
            onClick={onSkip}
            className="px-3 py-1.5 text-[13px] border border-border rounded bg-transparent text-text hover:bg-bg-hover cursor-pointer"
          >
            Cancel
          </button>
          <button
            onClick={() => onInject(results.filter(r => selected.has(r.id)))}
            className="px-3 py-1.5 text-[13px] bg-accent text-white rounded hover:bg-accent/80 cursor-pointer border-none"
          >
            Inject Context
          </button>
        </div>
      </div>
    </div>
  )
}
