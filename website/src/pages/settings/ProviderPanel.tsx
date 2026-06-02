import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsSelect, SettingsInput, SettingsButtonGroup } from '../../components/settings'
import { Btn, Badge } from '../../components/ui'
import { api } from '../../api/client'
import { useProvider } from '../../providers'
import { useState } from 'react'
import { Server, Zap, Cloud, CheckCircle2, AlertTriangle, RefreshCw, Download, FolderInput } from 'lucide-react'

type KiroClawConfig = {
  agent?: {
    provider?: string
    model?: string
    cc_model?: string
    bedrock_model_id?: string
    bedrock_region?: string
  }
}

const PROVIDER_OPTIONS = [
  { value: 'acp', label: 'Kiro ACP', icon: <Server size={14} /> },
  { value: 'claude_code', label: 'Claude Code', icon: <Zap size={14} /> },
  { value: 'bedrock', label: 'Bedrock', icon: <Cloud size={14} /> },
]

const BEDROCK_REGION_OPTIONS = ['us-west-2', 'us-east-1', 'eu-west-1', 'ap-northeast-1']

export function ProviderPanel() {
  const adapter = useProvider()
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')

  const mcQ = useQuery<KiroClawConfig>({
    queryKey: ['kiroclawConfig'],
    queryFn: () => api.kiroclawConfig(),
  })

  const agent = mcQ.data?.agent ?? {}
  const provider = agent.provider || 'acp'

  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) =>
      api.patchConfig(path, value),
    onMutate: async ({ path, value }) => {
      await qc.cancelQueries({ queryKey: ['kiroclawConfig'] })
      const prev = qc.getQueryData<KiroClawConfig>(['kiroclawConfig'])
      const next = structuredClone(prev ?? {})
      if (!next.agent) next.agent = {}
      const field = path.replace('agent.', '')
      ;(next.agent as Record<string, unknown>)[field] = value
      qc.setQueryData(['kiroclawConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['kiroclawConfig'], ctx.prev)
      setSaveError('Failed to save config')
      setTimeout(() => setSaveError(''), 3000)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['kiroclawConfig'] }),
  })

  const patch = (path: string, value: unknown) => patchMut.mutate({ path, value })

  if (mcQ.isLoading) {
    return <div className="text-muted text-sm py-12 text-center">Loading configuration...</div>
  }

  return (
    <div>
      {saveError && (
        <div className="bg-danger/10 border border-danger/20 rounded-lg p-3 mb-4 text-sm text-danger animate-rise">
          {saveError}
        </div>
      )}

      <SettingsSection title="LLM Provider">
        <SettingsCard>
          <SettingsButtonGroup
            label="Provider Backend"
            description="Select the LLM provider for agent sessions. Changing provider requires restarting active sessions."
            value={provider}
            options={PROVIDER_OPTIONS}
            onChange={v => patch('agent.provider', v)}
          />
          <div className="text-[11px] text-muted mt-1 pl-0.5">
            {provider === 'acp' && `${adapter.labels.sessionProcess} with JSON-RPC protocol. Full MCP tools, interactive approval, OS sandbox.`}
            {provider === 'claude_code' && `Claude Code via ACP adapter (Bedrock-routed). Full MCP tools, native context management, ${adapter.getContextWindow(agent.cc_model || adapter.getDefaultModel()) >= 1_000_000 ? '1M' : '200K'} token window.`}
            {provider === 'bedrock' && `${adapter.labels.sessionProcess}. Text-only Q&A, no tool execution. Fast, low-latency.`}
          </div>
        </SettingsCard>
      </SettingsSection>

      {provider === 'claude_code' && (
        <>
          <SettingsSection title="Claude Code Settings">
            <SettingsCard>
              <SettingsInput
                label="Model"
                description={`Model override for Claude Code sessions. Leave empty for default (${adapter.getDefaultModel()}).`}
                value={agent.cc_model || ''}
                onChange={v => patch('agent.cc_model', v)}
                placeholder={`e.g. ${adapter.getDefaultModel()}`}
              />
            </SettingsCard>
          </SettingsSection>
          <ClaudeCodeMigration />
        </>
      )}

      {provider === 'bedrock' && (
        <SettingsSection title="Bedrock Settings">
          <SettingsCard>
            <SettingsInput
              label="Model ID"
              description="Bedrock model identifier."
              value={agent.bedrock_model_id || ''}
              onChange={v => patch('agent.bedrock_model_id', v)}
              placeholder="anthropic.claude-sonnet-4-20250514"
            />
            <SettingsSelect
              label="Region"
              description="AWS region for Bedrock API calls."
              value={agent.bedrock_region || 'us-west-2'}
              options={BEDROCK_REGION_OPTIONS}
              onChange={v => patch('agent.bedrock_region', v)}
            />
          </SettingsCard>
        </SettingsSection>
      )}

      {provider === 'acp' && (
        <SettingsSection title="ACP Settings">
          <SettingsCard>
            <div className="text-[12px] text-muted py-2">
              ACP uses {adapter.labels.sessionProcess} with the model configured in your agent config.
              Model and MCP servers are managed via <code className="text-accent">kiroclaw setup</code> and {adapter.labels.pluginRegistryName.toLowerCase()}.
            </div>
          </SettingsCard>
        </SettingsSection>
      )}
    </div>
  )
}

function ClaudeCodeMigration() {
  const qc = useQueryClient()
  const [toast, setToast] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  const previewQ = useQuery({
    queryKey: ['ccMirrorPreview'],
    queryFn: () => api.ccMirrorPreview(),
    staleTime: 60_000,
  })
  const missingQ = useQuery({
    queryKey: ['ccAimMissing'],
    queryFn: () => api.ccAimMissing(),
    staleTime: 60_000,
  })

  const flash = (kind: 'ok' | 'warn' | 'err', text: string) => {
    setToast({ kind, text })
    setTimeout(() => setToast(null), 4000)
  }

  const mirrorMut = useMutation({
    mutationFn: (force: boolean) => api.ccMirrorRun(force),
    onSuccess: (data: { summary?: { mirrored: number; skipped: number; errors: number } }) => {
      const s = data?.summary
      const ok = !s?.errors
      flash(
        ok ? 'ok' : 'warn',
        s ? `Mirrored ${s.mirrored}, skipped ${s.skipped}${s.errors ? `, ${s.errors} error(s)` : ''}` : 'Mirror complete',
      )
      qc.invalidateQueries({ queryKey: ['ccMirrorPreview'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
      qc.invalidateQueries({ queryKey: ['skills'] })
    },
    onError: (err: Error) => flash('err', `Mirror failed: ${err.message}`),
  })

  const syncMut = useMutation({
    mutationFn: (packages?: string[]) => api.ccAimSync(packages),
    onSuccess: data => {
      const installed = data.installed?.length ?? 0
      const failed = data.failed?.length ?? 0
      flash(
        failed === 0 ? 'ok' : 'warn',
        `Installed ${installed}${failed ? `, ${failed} failed` : ''}`,
      )
      qc.invalidateQueries({ queryKey: ['ccAimMissing'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (err: Error) => flash('err', `AIM sync failed: ${err.message}`),
  })

  const summary = previewQ.data?.summary
  const detected = (summary?.agents_total ?? 0) + (summary?.mcp_total ?? 0) + (summary?.skills_total ?? 0)
  const missing = missingQ.data?.missing ?? []

  return (
    <SettingsSection title="Migrate from kiro to Claude Code">
      {toast && (
        <div
          className={`mb-3 rounded-lg p-3 text-[13px] animate-rise border ${
            toast.kind === 'ok'
              ? 'bg-ok-subtle/50 border-ok/20 text-ok'
              : toast.kind === 'warn'
                ? 'bg-warn-subtle/50 border-warn/20 text-warn'
                : 'bg-danger/10 border-danger/20 text-danger'
          }`}
          role="status"
        >
          {toast.text}
        </div>
      )}
      <SettingsCard>
        <div className="flex items-start gap-3">
          <FolderInput className="lucide-inline shrink-0 mt-0.5 text-muted" />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-medium text-text">Mirror local kiro config</div>
            <div className="text-[12px] text-muted mt-0.5">
              Copy <code>~/.kiro/agents</code>, <code>settings/mcp.json</code>, and <code>skills</code> to <code>~/.claude/</code>.
              Translates tool names, hook events, and auto-approve rules. Skipped files retain existing CC config.
            </div>
            <div className="text-[12px] text-muted mt-2">
              {previewQ.isLoading && <span>Scanning ~/.kiro…</span>}
              {previewQ.isError && <span className="text-danger">Preview failed: {(previewQ.error as Error).message}</span>}
              {previewQ.isSuccess && detected === 0 && (
                <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="lucide-inline text-ok" /> Nothing to mirror — ~/.kiro is empty or already mirrored.</span>
              )}
              {previewQ.isSuccess && detected > 0 && summary && (
                <span>
                  Detected {summary.agents_total} agent{summary.agents_total === 1 ? '' : 's'} ·{' '}
                  {summary.mcp_total} MCP entr{summary.mcp_total === 1 ? 'y' : 'ies'} ·{' '}
                  {summary.skills_total} skill{summary.skills_total === 1 ? '' : 's'}.
                  {summary.skipped > 0 && ` ${summary.skipped} already mirrored.`}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-3">
              <Btn
                primary
                disabled={mirrorMut.isPending || detected === 0}
                onClick={() => mirrorMut.mutate(false)}
              >
                {mirrorMut.isPending && mirrorMut.variables === false ? (
                  <RefreshCw className="lucide-inline animate-spin" />
                ) : (
                  <Download className="lucide-inline" />
                )}
                Mirror to ~/.claude
              </Btn>
              <Btn
                disabled={mirrorMut.isPending || detected === 0}
                onClick={() => {
                  if (window.confirm('Force overwrite will replace any existing files in ~/.claude with the kiro versions. Continue?')) {
                    mirrorMut.mutate(true)
                  }
                }}
              >
                {mirrorMut.isPending && mirrorMut.variables === true ? (
                  <RefreshCw className="lucide-inline animate-spin" />
                ) : (
                  <AlertTriangle className="lucide-inline" />
                )}
                Force overwrite
              </Btn>
              <Btn
                disabled={previewQ.isFetching}
                onClick={() => qc.invalidateQueries({ queryKey: ['ccMirrorPreview'] })}
                aria-label="Refresh preview"
              >
                <RefreshCw className={`lucide-inline ${previewQ.isFetching ? 'animate-spin' : ''}`} />
                Refresh
              </Btn>
            </div>
            <div className="text-[11px] text-muted mt-2">
              CLI equivalent: <code className="text-accent">kiroclaw mirror kiro-to-cc</code>
            </div>
          </div>
        </div>
      </SettingsCard>

      <SettingsCard>
        <div className="flex items-start gap-3">
          <Download className="lucide-inline shrink-0 mt-0.5 text-muted" />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-medium text-text">Install AIM packages for Claude Code</div>
            <div className="text-[12px] text-muted mt-0.5">
              Install kiro AIM packages as Claude Code plugins via <code>aim plugins install</code>.
              Standalone mode is enabled so hooks, MCP servers, and permissions survive.
            </div>
            <div className="mt-2">
              {missingQ.isLoading && <span className="text-[12px] text-muted">Checking AIM packages…</span>}
              {missingQ.isError && (
                <span className="text-[12px] text-danger">Lookup failed: {(missingQ.error as Error).message}</span>
              )}
              {missingQ.isSuccess && missing.length === 0 && (
                <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
                  <CheckCircle2 className="lucide-inline" /> All kiro AIM packages are installed for Claude Code.
                </span>
              )}
              {missingQ.isSuccess && missing.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {missing.map(pkg => (
                    <Badge key={pkg} variant="warn">
                      {pkg}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 mt-3">
              <Btn
                primary
                disabled={syncMut.isPending || missing.length === 0}
                onClick={() => syncMut.mutate(undefined)}
              >
                {syncMut.isPending ? (
                  <RefreshCw className="lucide-inline animate-spin" />
                ) : (
                  <Download className="lucide-inline" />
                )}
                Install all ({missing.length})
              </Btn>
              <Btn
                disabled={missingQ.isFetching}
                onClick={() => qc.invalidateQueries({ queryKey: ['ccAimMissing'] })}
                aria-label="Refresh AIM package list"
              >
                <RefreshCw className={`lucide-inline ${missingQ.isFetching ? 'animate-spin' : ''}`} />
                Refresh
              </Btn>
            </div>
            <div className="text-[11px] text-muted mt-2">
              CLI equivalent: <code className="text-accent">kiroclaw aim sync-cc</code>
            </div>
          </div>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
