// Code Review Sage — dashboard page (/code-review-sage).
//
// GitHub-PR-only: paste one or more GitHub PR URLs and the app backend runs a
// deterministic two-stage review (POST /api/apps/code-review-sage/review). The
// driver runs in-process, so the Phase 1 -> Phase 2 switch and finalize always
// run. Findings post as a PENDING (draft) review the human submits on GitHub.
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ScanSearch, GitPullRequest, ExternalLink, Circle, Settings } from 'lucide-react'

import { SendBtn } from '../../components/ui'

const API = '/api/apps/code-review-sage'

interface RunProgressEntry { phase: string; counts?: { red?: number; yellow?: number }; error?: string }
interface Run {
  run_id: string
  status: string
  changes: string[]
  progress?: Record<string, RunProgressEntry>
  report_slug?: string | null
  error?: string
  started_at?: string
  finished_at?: string
}
interface Settings { model: string | null; effort: string; active_namespaces: string[] }
interface SettingsResp { settings: Settings; models: string[]; efforts: string[]; namespaces: string[] }

// Human label for a GH-<owner>-<repo>-<n> change id (mirrors the backend id).
function changeLabel(id: string): string {
  const m = id.match(/^GH-(.+)-(.+)-(\d+)$/)
  return m ? `${m[1]}/${m[2]} #${m[3]}` : id
}

const PHASE_LABEL: Record<string, string> = {
  queued: 'queued', gating: 'design gate…', deep: 'deep review…',
  done: 'reviewed', failed: 'failed',
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}

async function sendJSON(url: string, body: unknown, method = 'POST'): Promise<Record<string, unknown>> {
  const r = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok || (data as { error?: string })?.error) {
    throw new Error((data as { error?: string })?.error || `HTTP ${r.status}`)
  }
  return data as Record<string, unknown>
}

export default function CodeReviewSagePage() {
  const qc = useQueryClient()
  const [input, setInput] = useState('')

  // React Query owns the fetch/loading/error/poll lifecycle. refetchInterval is
  // evaluated from the cached data, so a run that is already running on mount
  // resumes polling automatically (the old manual-interval version did not).
  const { data: runsData } = useQuery({
    queryKey: ['code-review-sage-runs'],
    queryFn: () => getJSON<{ runs: Run[] }>(`${API}/runs`),
    refetchInterval: (q) => (q.state.data?.runs?.[0]?.status === 'running' ? 3000 : false),
  })
  const run = runsData?.runs?.[0] ?? null

  const { data: settings } = useQuery({
    queryKey: ['code-review-sage-settings'],
    queryFn: () => getJSON<SettingsResp>(`${API}/settings`),
  })

  const reviewMut = useMutation({
    mutationFn: (links: string) => sendJSON(`${API}/review`, { links }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-review-sage-runs'] }),
  })

  const saveMut = useMutation({
    mutationFn: (patch: Partial<Settings>) => sendJSON(`${API}/settings`, patch, 'PUT'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-review-sage-settings'] }),
  })

  const s = settings?.settings
  const changes = run?.changes ?? []
  const running = reviewMut.isPending || run?.status === 'running'
  const reviewErr = reviewMut.error instanceof Error ? reviewMut.error.message : ''

  const startReview = () => {
    const links = input.trim()
    if (!links || running) return
    reviewMut.mutate(links)
  }

  return (
    <div className="p-6 max-w-[860px] mx-auto text-text">
      <h1 className="flex items-center gap-2.5 text-xl"><ScanSearch size={22} /> Code Review Sage</h1>
      <p className="text-muted text-[13px] mt-1">
        Self-evolving deep reviewer for GitHub PRs. Findings post as a PENDING (draft)
        review you submit yourself on GitHub.
      </p>

      {/* Always-visible GitHub one-time setup hint */}
      <div className="text-xs text-text bg-bg border border-border rounded-md px-3 py-2.5 my-3.5 leading-relaxed">
        <GitPullRequest size={14} className="inline align-middle mr-1.5" />
        <strong>GitHub PR — one-time setup.</strong> The review runs on the gateway host
        and needs the <code>gh</code> CLI authenticated there: run{' '}
        <code>gh auth login --hostname github.com</code> once (never paste a token into
        this page). Findings post as a single PENDING review you submit in the GitHub UI.
        A PR shown as <em>“unavailable — could not be fetched”</em> usually means{' '}
        <code>gh</code> is not authenticated on the gateway host.
      </div>

      <textarea
        value={input}
        onChange={e => setInput(e.target.value)}
        rows={3}
        placeholder="Paste one or more GitHub PR links (one per line or comma-separated)"
        className="w-full box-border text-[13px] px-3 py-2.5 rounded-md bg-bg text-text border border-border resize-y font-body"
      />
      <div className="flex items-center gap-3 mt-3">
        <SendBtn onClick={startReview} disabled={running || !input.trim()}>
          {running ? 'Running…' : 'Review'}
        </SendBtn>
        {s && (
          <span className="ml-auto text-[11px] text-muted">
            Model: {s.model || 'default'} · effort: {s.effort || 'default'}
          </span>
        )}
      </div>
      <div className="text-[11px] text-muted mt-2.5">
        Each PR is reviewed in its own clean pooled worker; comments are posted as drafts only.
      </div>
      {reviewErr && <div className="text-danger text-xs mt-2.5">{reviewErr}</div>}

      {/* Current / last run */}
      {run && (
        <div className="mt-[22px] border-t border-border pt-4">
          <div className="flex items-center gap-2.5 text-[13px]">
            <strong>Run {run.status === 'running' ? '(in progress)' : run.status}</strong>
            {run.report_slug && (
              <a href={`/artifacts/${run.report_slug}`}
                className="ml-auto flex items-center gap-1 text-accent text-xs">
                Open focus report <ExternalLink size={12} />
              </a>
            )}
          </div>
          {run.error && <div className="text-danger text-xs mt-1.5">{run.error}</div>}
          <ul className="list-none p-0 mt-2.5">
            {changes.map(id => {
              const p = run.progress?.[id]
              const phase = p?.phase ?? 'queued'
              const counts = p?.counts
              return (
                <li key={id}
                  className="flex items-center gap-2.5 text-xs py-1.5 border-b border-border">
                  <span className="font-mono">{changeLabel(id)}</span>
                  <span className={`ml-auto flex items-center gap-1 ${phase === 'failed' ? 'text-danger' : 'text-muted'}`}>
                    {PHASE_LABEL[phase] ?? phase}
                    {counts && phase === 'done' && (
                      <>
                        <Circle size={9} className="text-danger ml-1.5" fill="currentColor" />
                        {counts.red ?? 0}
                        <Circle size={9} className="text-warn ml-1" fill="currentColor" />
                        {counts.yellow ?? 0}
                      </>
                    )}
                    {p?.error ? ` — ${p.error}` : ''}
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* Settings — model + effort inherit the app default unless overridden */}
      {settings && (
        <details className="mt-[22px]">
          <summary className="cursor-pointer text-[13px] flex items-center gap-1.5">
            <Settings size={13} /> Configuration
          </summary>
          <div className="flex gap-[18px] mt-3 flex-wrap">
            <label className="text-xs text-muted">
              Model{' '}
              <select value={s?.model ?? ''} onChange={e => saveMut.mutate({ model: e.target.value || null })}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border">
                <option value="">Default (agent config)</option>
                {settings.models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="text-xs text-muted">
              Effort{' '}
              <select value={s?.effort ?? ''} onChange={e => saveMut.mutate({ effort: e.target.value })}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border">
                <option value="">Default (model/provider)</option>
                {settings.efforts.map(ef => <option key={ef} value={ef}>{ef}</option>)}
              </select>
            </label>
          </div>
        </details>
      )}
    </div>
  )
}
