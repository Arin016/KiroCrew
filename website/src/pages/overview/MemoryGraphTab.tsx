import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network as NetworkIcon, RefreshCw } from 'lucide-react'
import { Network } from 'vis-network'
import { DataSet } from 'vis-data'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { getContrastingTextColor } from '../../utils/sessionColors'

const GROUP_COLORS: Record<string, { bg: string; border: string; font: string }> = {
  preference: { bg: '#3b82f6', border: '#2563eb', font: getContrastingTextColor('#3b82f6') },
  project:    { bg: '#22c55e', border: '#16a34a', font: getContrastingTextColor('#22c55e') },
  semantic:   { bg: '#a855f7', border: '#9333ea', font: getContrastingTextColor('#a855f7') },
  lesson:     { bg: '#f97316', border: '#ea580c', font: getContrastingTextColor('#f97316') },
  history:    { bg: '#6b7280', border: '#4b5563', font: getContrastingTextColor('#6b7280') },
}

const StatusDot = ({ color }: { color: string }) => <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
const GROUP_LABELS: Record<string, ReactNode> = {
  preference: <><StatusDot color="#3b82f6" /> Preferences</>,
  project: <><StatusDot color="#22c55e" /> Projects</>,
  semantic: <><StatusDot color="#a855f7" /> Semantic</>,
  lesson: <><StatusDot color="#f97316" /> Lessons</>,
  history: <><StatusDot color="#9ca3af" /> History</>,
}

interface GraphNode { id: string; label: string; group: string; title: string }
interface GraphEdge { from: string; to: string }

export default function MemoryGraphTab() {
  const containerRef = useRef<HTMLDivElement>(null)
  const networkRef = useRef<Network | null>(null)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const [filter, setFilter] = useState<string | null>(null)
  const [searchImmediate, setSearchImmediate] = useState('')
  const [search, setSearch] = useState('')

  const { data, isLoading: loading, refetch: load } = useQuery({
    queryKey: ['memory-graph'],
    queryFn: async () => {
      const r = await api.memoryGraph().catch(() => ({ nodes: [], edges: [] }))
      return r as { nodes: GraphNode[]; edges: GraphEdge[] }
    },
  })
  const nodes = data?.nodes ?? []
  const edges = data?.edges ?? []

  useEffect(() => {
    const t = setTimeout(() => setSearch(searchImmediate), 300)
    return () => clearTimeout(t)
  }, [searchImmediate])

  const nodesDS = useRef(new DataSet<any>())
  const edgesDS = useRef(new DataSet<any>())

  useEffect(() => {
    if (!containerRef.current || nodes.length === 0) return
    // Destroy previous instance before creating a new one
    if (networkRef.current) {
      networkRef.current.destroy()
      networkRef.current = null
    }

    let net: Network | undefined
    let destroyed = false
    try {
      nodesDS.current.clear()
      nodesDS.current.add(nodes.map(n => {
        const c = GROUP_COLORS[n.group] || GROUP_COLORS.history
        return {
          id: n.id,
          label: n.label.length > 40 ? n.label.slice(0, 37) + '…' : n.label,
          title: (() => { const el = document.createElement('span'); el.textContent = n.title; return el })(),
          group: n.group,
          color: { background: c.bg, border: c.border, highlight: { background: c.border, border: c.font } },
          font: { color: c.font, size: 12 },
          shape: 'box', borderWidth: 1, borderWidthSelected: 3,
          margin: { top: 8, right: 8, bottom: 8, left: 8 },
        }
      }))
      edgesDS.current.clear()
      edgesDS.current.add(edges.map((e, i) => ({ id: `e${i}`, from: e.from, to: e.to, color: { color: '#555', opacity: 0.6 }, arrows: '' })))

      net = new Network(containerRef.current, { nodes: nodesDS.current, edges: edgesDS.current }, {
        physics: { solver: 'forceAtlas2Based', forceAtlas2Based: { gravitationalConstant: -40, centralGravity: 0.005, springLength: 120 }, stabilization: { iterations: 150 } },
        interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true },
        layout: { improvedLayout: nodes.length < 200 },
      })
      net.on('click', (params) => {
        if (params.nodes.length > 0) { const n = nodes.find(x => x.id === params.nodes[0]); setSelected(n || null) }
        else { setSelected(null) }
      })
      networkRef.current = net
    } catch (err) {
      console.warn('MemoryGraph: vis-network init failed', err)
      if (net && !destroyed) { net.destroy(); destroyed = true }
      net = undefined
    }
    return () => {
      if (net && !destroyed) { net.destroy(); destroyed = true }
      networkRef.current = null
    }
  }, [nodes, edges])

  // Toggle visibility without recreating the network
  useEffect(() => {
    if (!networkRef.current) return
    const searchLower = search.toLowerCase()
    nodesDS.current.forEach((n: any) => {
      const hidden = (!!filter && n.group !== filter) ||
        (!!search && !n.label.toLowerCase().includes(searchLower))
      nodesDS.current.update({ id: n.id, hidden })
    })
  }, [filter, search, nodes])

  const counts = nodes.reduce<Record<string, number>>((acc, n) => {
    acc[n.group] = (acc[n.group] || 0) + 1
    return acc
  }, {})

  if (loading) return <Card><CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph</CardTitle><p className="text-muted text-sm">Loading graph data…</p></Card>
  if (nodes.length === 0) return <Card><CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph</CardTitle><p className="text-muted text-sm">No memory data to visualize. Add preferences, projects, or lessons first.</p></Card>

  return (<>
    <Card>
      <CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph <InfoTip text="Interactive visualization of all KiroClaw memory. Nodes are color-coded by type. Connected nodes share references. Click a node to inspect, use filters to focus." />
        <Btn onClick={() => load()} className="ml-2"><RefreshCw className="lucide-inline" /> Refresh</Btn>
      </CardTitle>
      <div className="flex gap-2 flex-wrap mb-3 items-center">
        <input
          className="bg-bg-elevated border border-border rounded-md px-3 py-1.5 text-text text-sm font-body outline-none transition-colors focus-ring flex-1 min-w-[200px]"
          placeholder="Search nodes…" value={searchImmediate} onChange={e => setSearchImmediate(e.target.value)}
        />
        <Btn onClick={() => setFilter(null)} className={!filter ? '!border-accent !text-accent' : ''}>All ({nodes.length})</Btn>
        {Object.entries(GROUP_LABELS).map(([key, label]) => counts[key] ? (
          <Btn key={key} onClick={() => setFilter(filter === key ? null : key)} className={filter === key ? '!border-accent !text-accent' : ''}>{label} ({counts[key]})</Btn>
        ) : null)}
      </div>
      <div ref={containerRef} className="w-full border border-border rounded-md bg-bg-elevated" style={{ height: '500px' }} />
      {selected && (
        <div className="mt-3 p-3 bg-bg-elevated border border-border rounded-md">
          <div className="flex items-center gap-2 mb-1">
            <Badge variant={selected.group === 'lesson' ? 'warn' : selected.group === 'semantic' ? 'aim' : 'ok'}>{selected.group}</Badge>
            <span className="text-sm font-medium text-text-strong">{selected.label}</span>
          </div>
          <p className="text-sm text-muted break-words whitespace-pre-wrap">{selected.title}</p>
        </div>
      )}
    </Card>
  </>)
}
