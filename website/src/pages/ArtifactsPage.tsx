import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bookmark, ExternalLink, X } from 'lucide-react'
import { api } from '../api/client'
import { Card, CardTitle, PageHeader, StatCard, Btn, Badge, SearchInput, EmptyState } from '../components/ui'
import InfoTip from '../components/InfoTip'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import type { Artifact } from '../types'

const sel =
  'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring'

const KIND_OPTIONS = ['', 'widget', 'html', 'markdown', 'svg', 'json', 'text'] as const

const KIND_BADGE: Record<Artifact['kind'], 'ok' | 'err' | 'warn' | 'aim'> = {
  widget: 'aim',
  html: 'ok',
  markdown: 'ok',
  svg: 'warn',
  json: 'ok',
  text: 'ok',
}

function isoToTs(iso: string): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0
}

export default function ArtifactsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [kindFilter, setKindFilter] = useState<string>('')

  const { data, isLoading, error } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', { tag: tagFilter, kind: kindFilter }],
    queryFn: () =>
      api.artifacts({
        tag: tagFilter || undefined,
        kind: kindFilter || undefined,
      }),
  })

  // Separate unfiltered query that drives the tag dropdown options so users
  // can switch between tags without first resetting to "all tags". Without
  // this, allTags would be derived only from currently-filtered results and
  // co-occurring tags would disappear when one is selected.
  const { data: allTagsData } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', 'all-tags'],
    queryFn: () => api.artifacts({}),
  })

  const artifacts = data?.artifacts || []
  const allTags = useMemo(() => {
    const s = new Set<string>()
    for (const a of allTagsData?.artifacts || []) for (const t of a.tags || []) s.add(t)
    return Array.from(s).sort()
  }, [allTagsData])

  const visible = useMemo(() => {
    if (!filter) return artifacts
    const q = filter.toLowerCase()
    return artifacts.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        a.slug.toLowerCase().includes(q) ||
        (a.description || '').toLowerCase().includes(q),
    )
  }, [artifacts, filter])

  const totalVersions = artifacts.reduce((sum, a) => sum + (a.version || 1), 0)
  const widgetCount = artifacts.filter((a) => a.kind === 'widget').length

  const deleteMut = useMutation({
    mutationFn: (slug: string) => api.deleteArtifact(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['artifacts'] }),
  })

  const handleDelete = (slug: string) => {
    if (window.confirm(
      `Remove artifact "${slug}" from your library?\n\n` +
      `This deletes the artifact entry and its version history. ` +
      `If this artifact came from a file on disk or a chat widget, ` +
      `the original is NOT touched — you can re-add it later.`
    )) {
      deleteMut.mutate(slug)
    }
  }

  const errMessage = error ? (error instanceof Error ? error.message : String(error)) : null
  const mutErr = deleteMut.error
    ? deleteMut.error instanceof Error
      ? deleteMut.error.message
      : String(deleteMut.error)
    : null

  if (isLoading) return <div className="p-6 text-muted">Loading…</div>

  return (
    <>
      <PageHeader title="Artifacts" subtitle="Widgets, files, and snippets — live-tracked with version history" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Total" value={artifacts.length} accent />
          <StatCard label="Widgets" value={widgetCount} delay={60} />
          <StatCard label="Tags" value={allTags.length} delay={120} />
          <StatCard label="Total Versions" value={totalVersions} delay={180} />
        </div>

        {(errMessage || mutErr) && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <span className="text-danger text-lg shrink-0"><AlertTriangle className="lucide-inline" /></span>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-danger font-medium">Error</div>
              <div className="text-[13px] text-danger/90 mt-0.5">{errMessage || mutErr}</div>
            </div>
            <Btn onClick={() => deleteMut.reset()} className="text-danger/60 hover:text-danger shrink-0">×</Btn>
          </div>
        )}

        <Card>
          <CardTitle>
            Library{' '}
            <InfoTip text="Artifacts are persistent, versioned widgets. Save one from any rendered <mcwidget> in chat (Bookmark icon), or have the agent call artifact_save. Iterate later via 'iterate on artifact <slug>'." />
          </CardTitle>
          <div className="flex flex-wrap gap-2 items-center mb-3">
            <SearchInput
              placeholder="Filter by name, slug, description…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <select className={sel} value={kindFilter} aria-label="Filter by kind" onChange={(e) => setKindFilter(e.target.value)}>
              {KIND_OPTIONS.map((k) => (
                <option key={k} value={k}>
                  {k ? `kind: ${k}` : 'all kinds'}
                </option>
              ))}
            </select>
            <select className={sel} value={tagFilter} aria-label="Filter by tag" onChange={(e) => setTagFilter(e.target.value)}>
              <option value="">all tags</option>
              {allTags.map((t) => (
                <option key={t} value={t}>
                  tag: {t}
                </option>
              ))}
            </select>
          </div>

          {artifacts.length === 0 ? (
            <EmptyState
              icon={<Bookmark className="lucide-inline" />}
              title="No artifacts yet"
              subtitle="Click the bookmark icon on any rendered widget in chat to save it here."
            />
          ) : visible.length === 0 ? (
            <div className="text-muted italic px-2.5 py-3.5 text-sm">No artifacts match your filters.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse table-striped">
                <thead>
                  <tr>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[160px]">Name</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[180px]">Slug</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[100px]">Kind</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[60px]">Ver</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[160px]">Tags</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[110px]">Updated</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[120px]">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((a) => (
                    <tr
                      key={a.slug}
                      className="hover:bg-bg-hover transition-colors cursor-pointer"
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey) {
                          window.open(`/artifacts/${a.slug}`, '_blank')
                        } else {
                          navigate(`/artifacts/${a.slug}`)
                        }
                      }}
                    >
                      <td className="px-2.5 py-2 border-b border-border">
                        <div className="text-sm text-text-strong font-medium">{a.name}</div>
                        {a.description && (
                          <div className="text-[12px] text-muted truncate max-w-[400px]">{a.description}</div>
                        )}
                      </td>
                      <td className="px-2.5 py-2 border-b border-border">
                        <code className="text-[12px] text-muted">{a.slug}</code>
                      </td>
                      <td className="px-2.5 py-2 border-b border-border">
                        <Badge variant={KIND_BADGE[a.kind]}>{a.kind}</Badge>
                      </td>
                      <td className="px-2.5 py-2 border-b border-border text-sm text-muted">v{a.version}</td>
                      <td className="px-2.5 py-2 border-b border-border">
                        <div className="flex flex-wrap gap-1">
                          {(a.tags || []).map((t) => (
                            <span
                              key={t}
                              className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted"
                            >
                              {t}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">
                        {_timeAgo(isoToTs(a.updated_at))}
                      </td>
                      <td className="px-2.5 py-2 border-b border-border">
                        <div className="flex items-center gap-1">
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              navigate(`/artifacts/${a.slug}`)
                            }}
                            className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                            title="Open standalone"
                            aria-label="Open standalone"
                          >
                            <ExternalLink size={13} />
                          </button>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              handleDelete(a.slug)
                            }}
                            className="p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer bg-transparent border-none"
                            title="Remove from artifacts library (does not delete the source file or widget)"
                            aria-label="Remove from artifacts library"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}
