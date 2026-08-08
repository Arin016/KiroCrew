import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Bot, FolderOpen, Brain, Settings, Lock, Flame, X } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Badge, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import SimpleSelect from '../../components/SimpleSelect'
import { useProvider } from '../../providers'

import type { KiroCrewAgent } from '../../components/AgentSelector'

import { i18nT } from '../../i18n/t'
type KiroCrewAgentCfg = Omit<KiroCrewAgent, 'name'>
interface WorkspaceCfg { dir: string }
interface MemoryStoreCfg { description: string; embedding_provider: string }
interface KiroCrewCfg {
  agents: Record<string, KiroCrewAgentCfg>
  default_agent: string
  workspaces: Record<string, WorkspaceCfg>
  default_workspace: string
  memory_stores: Record<string, MemoryStoreCfg>
  default_memory_store: string
  agent: { default_agent: string; provider: string; model: string; approval_mode: string; sandbox: string; subagent_max_turns?: number; max_subagents?: number; subagent_auto_max?: number; conductor_skill?: boolean; tool_search?: boolean; max_channels: number; max_channel_agents: number; enforce_denied_commands: string }
  session: { timeout_secs: number; pool_size: number; pool_agent: string; pool_agents?: Record<string, number> | null; pool_ttl_secs: number }
  memory: { embedding_provider: string }
  auto_update: boolean
}

function Tag({ children, active }: { children: React.ReactNode; active?: boolean }) {
  return <span className={`px-1.5 py-[1px] rounded text-[12px] font-mono ${active ? 'bg-accent/15 text-accent border border-accent/30' : 'bg-bg-elevated text-muted border border-border'}`}>{children}</span>
}

function UsedByTags({ names }: { names: string[] }) {
  return <div className="flex gap-1 flex-wrap">{names.length > 0 ? names.map(n => <Tag key={n} active>{n}</Tag>) : <span className="text-muted text-[13px]">—</span>}</div>
}

const rowCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm"
const inputCls = "h-7 min-w-[120px] bg-bg-elevated border border-border rounded-md px-2 py-0.5 text-[13px] font-mono text-text focus:border-accent focus:outline-none"
const readonlyCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm bg-bg-elevated/30 rounded px-1 -mx-1"

function useDirtyTrack<T>(value: T) {
  const [ok, setOk] = useState(false)
  const dirty = useRef(false)
  useEffect(() => { if (dirty.current) { setOk(true); dirty.current = false; const t = setTimeout(() => setOk(false), 2000); return () => clearTimeout(t) } }, [value])
  const markDirty = () => { dirty.current = true }
  return { ok, markDirty }
}

function CfgRow({ label, hint, ok, children }: { label: string; hint?: string; ok: boolean; children: React.ReactNode }) {
  return (
    <div className={rowCls}>
      <span className="text-muted inline-flex items-center gap-1">{label} {hint && <InfoTip text={hint} />}</span>
      <div className="flex items-center gap-1.5">
        {children}
        {ok && <span className="text-ok text-[11px]"><Check className="lucide-inline" /></span>}
      </div>
    </div>
  )
}

function CfgSelect({ label, path, value, options, hint, labels, onSave }: { label: string; path: string; value: string; options: string[]; hint?: string; labels?: Record<string, string>; onSave: (p: string, v: string) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      {/* The trigger is a <button>, so the row's visible label is threaded in
          as the accessible name (matches CfgNumber's aria-label={label}).
          `className` restores this table's compact geometry: the shared trigger
          defaults to `px-3 py-2 text-sm`, which is ~10px taller than the `h-7
          text-[13px]` control it replaced and would grow every row. */}
      <SimpleSelect
        aria-label={label}
        className="h-7 px-2 py-0.5 text-[13px] font-mono"
        style={{ minWidth: 120 }}
        options={options}
        optionLabels={options.map(o => labels?.[o] ?? o)}
        value={local}
        onChange={v => { markDirty(); setLocal(v); onSave(path, v) }}
      />
    </CfgRow>
  )
}

function CfgNumber({ label, path, value, suffix, min, max, hint, onSave }: { label: string; path: string; value: number; suffix?: string; min?: number; max?: number; hint?: string; onSave: (p: string, v: number) => void }) {
  const [local, setLocal] = useState(String(value))
  const { ok, markDirty } = useDirtyTrack(value)
  const [err, setErr] = useState('')
  useEffect(() => { setLocal(String(value)); setErr('') }, [value])
  const commit = () => {
    const n = parseInt(local)
    if (isNaN(n)) { setErr('invalid'); return }
    if (min !== undefined && n < min) { setErr(`min ${min}`); return }
    if (max !== undefined && n > max) { setErr(`max ${max}`); return }
    if (n !== value) { markDirty(); setErr(''); onSave(path, n) }
  }
  return (
    <CfgRow label={label} hint={hint} ok={ok && !err}>
      <input type="number" aria-label={label} min={min} max={max} placeholder={min !== undefined && max !== undefined ? `${min}–${max}` : undefined}
        className={`${inputCls} text-right ${err ? 'border-danger' : ''}`}
        value={local}
        onChange={e => { setLocal(e.target.value); setErr('') }}
        onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit() }}
      />
      {suffix && <span className="text-muted text-[12px]">{suffix}</span>}
      {err && <span className="text-danger text-[11px]">{err}</span>}
    </CfgRow>
  )
}

function CfgToggle({ label, path, value, hint, onSave }: { label: string; path: string; value: boolean; hint?: string; onSave: (p: string, v: boolean) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      <button className={`h-7 min-w-[120px] px-2 py-0.5 rounded text-[13px] font-mono ${local ? 'bg-ok/15 text-ok border border-ok/30' : 'bg-bg-elevated text-muted border border-border'}`} onClick={() => { markDirty(); const v = !local; setLocal(v); onSave(path, v) }}>
        {local ? 'on' : 'off'}
      </button>
    </CfgRow>
  )
}

function PoolAgentCount({ label, agentKey, counts, max, busy, isPending, onSave, onRemove }: { label: string; agentKey: string; counts: Record<string, number>; max: number; busy: boolean; isPending?: boolean; onSave: (p: string, v: unknown) => void; onRemove: () => void }) {
  const value = counts[agentKey] ?? 0
  const [local, setLocal] = useState(String(value))
  const { ok, markDirty } = useDirtyTrack(value)
  const [err, setErr] = useState('')
  useEffect(() => { setLocal(String(value)); setErr('') }, [value])
  const commit = () => {
    // A commit rebuilds the WHOLE map from the counts prop, which only
    // refreshes when the previous PATCH round-trips — committing a second
    // row mid-flight would resend the first row's stale value. The input is
    // disabled while a save is pending, so commit can trust the prop.
    if (busy) return
    const n = parseInt(local)
    if (isNaN(n) || n < 0) { setErr('invalid'); return }
    const rest = Object.entries(counts).reduce((s, [k, v]) => k === agentKey ? s : s + v, 0)
    if (rest + n > max) { setErr(i18nT('pages.overview.kiroCrewCfgTab.max_n_of_total_cap', { n: max - rest, total: max })); return }
    if (n !== value) {
      markDirty(); setErr('')
      const next: Record<string, number> = { ...counts }
      if (n > 0) next[agentKey] = n
      else delete next[agentKey]
      onSave('session.pool_agents', next)
    }
  }
  return (
    <CfgRow label={label} ok={ok && !err}>
      <input type="number" aria-label={label} min={0} max={max} disabled={busy} autoFocus={isPending}
        className={`${inputCls} text-right ${err ? 'border-danger' : ''} ${busy ? 'opacity-50' : ''}`}
        value={local}
        onChange={e => { setLocal(e.target.value); setErr('') }}
        onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit() }}
      />
      <span className="text-muted text-[12px]">{i18nT('pages.overview.kiroCrewCfgTab.warm_suffix')}</span>
      {err && <span className="text-danger text-[11px]">{err}</span>}
      {!err && isPending && <span className="text-warning text-[11px]">{i18nT('pages.overview.kiroCrewCfgTab.enter_a_count')}</span>}
      <button aria-label={i18nT('pages.overview.kiroCrewCfgTab.remove_from_pool', { name: label })} disabled={busy}
        className={`text-muted hover:text-danger transition-colors ${busy ? 'opacity-50' : ''}`}
        onClick={onRemove}>
        <X className="lucide-inline" />
      </button>
    </CfgRow>
  )
}

export default function KiroCrewCfgTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const { data: cfg = null, error: queryErr } = useQuery<KiroCrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const err = queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''
  const [saveErr, setSaveErr] = useState('')
  const [rev, setRev] = useState(0)
  // Agents added to the Warm Pool card but not yet holding a saved count.
  // Local-only: a pending row becomes config-backed on its first commit >0.
  const [pendingPool, setPendingPool] = useState<string[]>([])

  const reqId = useRef(0)

  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) => api.patchConfig(path, value),
    onSuccess: (updated) => { queryClient.setQueryData(['kirocrewConfig'], updated) },
    onError: (e: Error) => {
      setSaveErr(e.message)
      setTimeout(() => setSaveErr(''), 4000)
      queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      setRev(r => r + 1)
    },
  })

  const save = (path: string, value: unknown) => {
    ++reqId.current
    patchMut.mutate({ path, value })
  }

  if (err) return <Card><p className="text-danger text-sm">{err}</p></Card>
  if (!cfg) return <Card><div className="skeleton h-40 rounded" /></Card>

  const agents = Object.entries(cfg.agents)
  const workspaces = Object.entries(cfg.workspaces)
  const stores = Object.entries(cfg.memory_stores)

  // Per-agent warm counts for display, mirroring the loader's tri-state:
  // an explicit map is authoritative (including {} = disabled); an absent
  // map derives from the legacy pool_size/pool_agent pair. Map keys are
  // preserved VERBATIM — the "" key is the follow-the-default sentinel
  // (resolved to agent.default_agent at claim time), so folding it into the
  // concrete default-agent name for saving would pin standby to whatever
  // the default was at save time and break follow-the-default semantics.
  // Only the row LABEL resolves "" to the current default agent's name.
  const poolCounts: Record<string, number> = { ...(cfg.session.pool_agents ?? (
    (cfg.session.pool_size ?? 0) > 0 ? { [cfg.session.pool_agent ?? '']: cfg.session.pool_size } : {}
  )) }
  // Membership is explicit: only agents in the map (or added this session and
  // not yet committed) get a row. Map keys for since-deleted agents still
  // render so the stale entry stays removable. Everything else cold-starts.
  const pending = pendingPool.filter(n => !(n in poolCounts))
  const poolRows = [
    ...('' in poolCounts ? [''] : []),
    ...Object.keys(cfg.agents).filter(n => n in poolCounts || pending.includes(n)),
    ...Object.keys(poolCounts).filter(n => n !== '' && !(n in cfg.agents)),
  ]
  const poolAddable = Object.keys(cfg.agents).filter(n => !poolRows.includes(n))
  // The "(default agent)" marker goes on the row that FOLLOWS the default:
  // the "" sentinel row when present, else the row named after the current
  // default agent. When both exist the concrete row renders plain, keeping
  // the two labels (and their aria-labels) distinct.
  const poolRowLabel = (name: string) => {
    if (name === '')
      return `${cfg.default_agent} (${i18nT('pages.overview.kiroCrewCfgTab.default_agent')})`
    if (name === cfg.default_agent && !('' in poolCounts))
      return `${name} (${i18nT('pages.overview.kiroCrewCfgTab.default_agent')})`
    if (!(name in cfg.agents))
      return `${name} (${i18nT('pages.overview.kiroCrewCfgTab.deleted_agent')})`
    return name
  }
  const removePoolAgent = (name: string) => {
    setPendingPool(p => p.filter(x => x !== name))
    if (name in poolCounts) {
      const next = { ...poolCounts }
      delete next[name]
      save('session.pool_agents', next)
    }
  }

  return (
    <>
      {/* Agents */}
      <Card>
        <CardTitle><Bot className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.kirocrew_agents')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_agent_definitions', { label: provider.labels.agentTemplateField.toLowerCase() })} /></CardTitle>
        {agents.length === 0 ? (
          <EmptyState icon={<Bot className="lucide-inline" />} title={i18nT('pages.overview.kiroCrewCfgTab.no_agents_defined')} subtitle={i18nT('pages.overview.kiroCrewCfgTab.using_legacy_mode_agent_default_agent_as_agent_t')} />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{provider.labels.agentTemplateField}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.workspace')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.memory_store')}</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(([name, a]) => (
                <tr key={name}>
                  <td className="px-2.5 py-2 text-sm text-text font-medium">
                    {name} {name === cfg.default_agent && <Badge variant="aim">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
                  </td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.kiro_agent || '—'}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.workspace}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.memory_store}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* Workspaces */}
      <Card>
        <CardTitle><FolderOpen className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.workspaces')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_workspace_directories_each_agent_binds_to')} /></CardTitle>
        <table className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.directory')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.used_by')}</th>
            </tr>
          </thead>
          <tbody>
            {workspaces.map(([name, ws]) => {
              const usedBy = agents.filter(([, a]) => a.workspace === name).map(([n]) => n)
              return (
                <tr key={name}>
                  <td className="px-2.5 py-2 text-sm text-text font-medium">
                    {name} {name === cfg.default_workspace && <Badge variant="ok">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
                  </td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{ws.dir}</td>
                  <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Card>

      {/* Memory Stores */}
      <Card>
        <CardTitle><Brain className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.memory_stores')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_memory_stores_with_optional_per_store_embe')} /></CardTitle>
        {stores.length === 0 ? (
          <EmptyState icon={<Brain className="lucide-inline" />} title={i18nT('pages.overview.kiroCrewCfgTab.no_memory_stores')} subtitle={i18nT('pages.overview.kiroCrewCfgTab.using_global_memory_settings')} />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.description')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.embedding')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.used_by')}</th>
              </tr>
            </thead>
            <tbody>
              {stores.map(([name, ms]) => {
                const usedBy = agents.filter(([, a]) => a.memory_store === name).map(([n]) => n)
                return (
                  <tr key={name}>
                    <td className="px-2.5 py-2 text-sm text-text font-medium">
                      {name} {name === cfg.default_memory_store && <Badge variant="ok">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
                    </td>
                    <td className="px-2.5 py-2 text-[13px] text-muted">{ms.description || '—'}</td>
                    <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{ms.embedding_provider || <span className="italic">{i18nT('pages.overview.kiroCrewCfgTab.inherited_provider', { provider: cfg.memory.embedding_provider })}</span>}</td>
                    <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      {/* Subagent Settings */}
      <SubagentSettings cfg={cfg} onSaved={() => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })} />

      {/* Warm Pool */}
      {provider.capabilities.warmPool && (
      <Card>
        <CardTitle><Flame className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.warm_pool')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.restart_required_to_apply_changes', { description: provider.labels.warmPoolDescription })} /></CardTitle>
        <p className="text-muted text-[13px] mb-2">{i18nT('pages.overview.kiroCrewCfgTab.only_agents_added_here_keep_warm_sessions')}</p>
        {saveErr && <p className="text-danger text-[13px] mb-2">{saveErr}</p>}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          {poolRows.map(name => (
            <PoolAgentCount key={`poolagents-${name}-${rev}`}
              label={poolRowLabel(name)}
              agentKey={name} counts={poolCounts} max={10} busy={patchMut.isPending}
              isPending={pending.includes(name)} onSave={save}
              onRemove={() => removePoolAgent(name)} />
          ))}
          {poolRows.length === 0 && (
            <div className={rowCls}>
              <span className="text-muted text-[13px]">{i18nT('pages.overview.kiroCrewCfgTab.no_agents_on_standby')}</span>
            </div>
          )}
          {poolAddable.length > 0 && (
            <div className={rowCls}>
              <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.add_agent')}</span>
              <SimpleSelect
                aria-label={i18nT('pages.overview.kiroCrewCfgTab.add_agent')}
                className="h-7 px-2 py-0.5 text-[13px] font-mono"
                style={{ minWidth: 120 }}
                options={poolAddable}
                value=""
                triggerFallback={i18nT('pages.overview.kiroCrewCfgTab.select_agent')}
                onChange={v => { if (v) setPendingPool(p => p.includes(v) ? p : [...p, v]) }}
              />
            </div>
          )}
          <CfgNumber key={`poolttl-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.pool_ttl')} path="session.pool_ttl_secs" value={cfg.session.pool_ttl_secs} suffix="s" min={0} max={7200} hint={i18nT('pages.overview.kiroCrewCfgTab.max_age_for_pooled_processes_0_disables_expiry_r')} onSave={save} />
        </div>
      </Card>
      )}

      {/* Quick Info */}
      <Card>
        <CardTitle><Settings className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.config_summary')}</CardTitle>
        {saveErr && <p className="text-danger text-[13px] mb-2">{saveErr}</p>}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.provider')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.provider}</span></div>
          <CfgSelect key={`approval-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.approval_mode')} path="agent.approval_mode" value={cfg.agent.approval_mode} options={['auto', 'interactive']} hint={i18nT('pages.overview.kiroCrewCfgTab.immediate_auto_approves_all_tools_interactive_as')} onSave={save} />
          <CfgNumber key={`timeout-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.session_timeout')} path="session.timeout_secs" value={cfg.session.timeout_secs} suffix="s" min={60} max={86400} hint={i18nT('pages.overview.kiroCrewCfgTab.takes_effect_on_next_session_range_60_86400s')} onSave={save} />
          <CfgSelect key={`sandbox-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.sandbox')} path="agent.sandbox" value={cfg.agent.sandbox} options={['auto', 'off']} hint={i18nT('pages.overview.kiroCrewCfgTab.immediate_auto_enables_sandbox_for_untrusted_too')} onSave={save} />
          <CfgSelect key={`enforce-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.enforce_denied_commands')} path="agent.enforce_denied_commands" value={cfg.agent.enforce_denied_commands ?? 'all'} options={['all', 'kirocrew']} hint={i18nT('pages.overview.kiroCrewCfgTab.immediate_all_enforces_on_every_agent_kirocrew_o')} onSave={save} />
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.embedding_provider')}</span><span className="text-text font-mono text-[13px]">{cfg.memory.embedding_provider}</span></div>
          <CfgToggle key={`autoupdate-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.auto_update')} path="auto_update" value={cfg.auto_update} hint={i18nT('pages.overview.kiroCrewCfgTab.next_update_check_cycle')} onSave={save} />
          <CfgToggle key={`toolsearch-${rev}`} label={i18nT('pages.overview.kiroCrewCfgTab.mcp_tool_search')} path="agent.tool_search" value={cfg.agent.tool_search ?? true} hint={i18nT('pages.overview.kiroCrewCfgTab.enable_dynamic_mcp_tool_discovery_via_kiro_cli_t')} onSave={save} />
          <div className={readonlyCls}><span className="text-muted">{i18nT('pages.overview.kiroCrewCfgTab.max_channels')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channels}</span></div>
          <div className={readonlyCls}><span className="text-muted">{i18nT('pages.overview.kiroCrewCfgTab.max_channel_agents')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channel_agents}</span></div>
        </div>
      </Card>
    </>
  )
}

function SubagentSettings({ cfg, onSaved }: { cfg: KiroCrewCfg; onSaved: () => void }) {
  const [maxTurns, setMaxTurns] = useState(cfg.agent.subagent_max_turns ?? 100)
  const [maxSubs, setMaxSubs] = useState(cfg.agent.max_subagents ?? 3)
  const [autoMax, setAutoMax] = useState(cfg.agent.subagent_auto_max ?? 16)
  const hardCap = autoMax
  const [conductor, setConductor] = useState(cfg.agent.conductor_skill ?? false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<ReactNode>('')
  const [msgOk, setMsgOk] = useState(false)

  useEffect(() => {
    setMaxTurns(cfg.agent.subagent_max_turns ?? 100)
    setMaxSubs(cfg.agent.max_subagents ?? 3)
    setAutoMax(cfg.agent.subagent_auto_max ?? 16)
    setConductor(cfg.agent.conductor_skill ?? false)
  }, [cfg])

  const dirty = maxTurns !== (cfg.agent.subagent_max_turns ?? 100) || maxSubs !== (cfg.agent.max_subagents ?? 3) || autoMax !== (cfg.agent.subagent_auto_max ?? 16) || conductor !== (cfg.agent.conductor_skill ?? false)

  const save = async () => {
    setSaving(true); setMsg('')
    try {
      const res = await api.saveKirocrewConfig({ subagent_max_turns: maxTurns, max_subagents: maxSubs, subagent_auto_max: autoMax, conductor_skill: conductor })
      if (res.error) { setMsg(res.error); setMsgOk(false) } else { setMsg(<><Check className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.saved')}</>); setMsgOk(true); onSaved() }
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); setMsgOk(false) }
    finally { setSaving(false) }
  }

  return (
    <Card>
      <CardTitle><Bot className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.subagent_settings')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.controls_how_many_subagents_can_run_concurrently')} /></CardTitle>
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 max-[600px]:grid-cols-1">
        {/* label-has-for flags a label whose only control is a <button>; the
            toggle button is self-labeling (its text is the value) and the label
            wrapper only extends the click target to the row text — intentional. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label htmlFor="subagent-orchestrator-mode" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.orchestrator_mode')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.enable_conductor_skill_for_multi_agent_orchestra')} /></span>
          <button id="subagent-orchestrator-mode" aria-label={i18nT('pages.overview.kiroCrewCfgTab.orchestrator_mode')} onClick={() => setConductor(!conductor)}
            className={`px-3 py-1 rounded text-[13px] font-medium border cursor-pointer transition-all ${conductor ? 'bg-accent/10 border-accent text-accent' : 'bg-transparent border-border text-muted'}`}>
            {conductor ? i18nT('pages.overview.kiroCrewCfgTab.enabled') : i18nT('pages.overview.kiroCrewCfgTab.disabled')}
          </button>
        </label>
        <label htmlFor="subagent-max-turns" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.max_turns_per_subagent')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.tool_call_budget_per_subagent_1_200_default_100')} /></span>
          <input id="subagent-max-turns" aria-label={i18nT('pages.overview.kiroCrewCfgTab.max_turns_per_subagent')} type="number" min={1} max={200} value={maxTurns} onChange={e => setMaxTurns(parseInt(e.target.value) || 1)}
            className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
        </label>
        <label htmlFor="subagent-max-concurrent" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.max_concurrent_subagents')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.maximum_subagents_running_at_once', { cap: hardCap })} /></span>
          <span className="inline-flex items-center gap-2">
            {maxSubs === 0 && <span className="text-[11px] text-muted">{i18nT('pages.overview.kiroCrewCfgTab.auto')}</span>}
            <input id="subagent-max-concurrent" aria-label={i18nT('pages.overview.kiroCrewCfgTab.max_concurrent_subagents')} type="number" min={0} max={hardCap} value={maxSubs} onChange={e => { const v = parseInt(e.target.value); setMaxSubs(Number.isNaN(v) ? 0 : Math.max(0, v)) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </span>
        </label>
        {maxSubs === 0 && (
          <label htmlFor="subagent-auto-size-max" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
            <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.auto_size_max')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.ceiling_on_the_auto_sized_concurrent_subagent_co')} /></span>
            <input id="subagent-auto-size-max" aria-label={i18nT('pages.overview.kiroCrewCfgTab.auto_size_max')} type="number" min={1} max={64} value={autoMax} onChange={e => { const v = parseInt(e.target.value); setAutoMax(Number.isNaN(v) ? 1 : Math.min(64, Math.max(1, v))) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </label>
        )}
      </div>
      <div className="flex items-center gap-3 mt-3">
        <button onClick={save} disabled={!dirty || saving}
          className="px-3 py-1.5 rounded text-sm font-medium bg-accent text-accent-fg hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed">
          {saving ? i18nT('pages.overview.kiroCrewCfgTab.saving') : i18nT('pages.overview.kiroCrewCfgTab.save')}
        </button>
        {msg && <span className={`text-[13px] ${msgOk ? 'text-ok' : 'text-danger'}`}>{msg}</span>}
      </div>
    </Card>
  )
}
