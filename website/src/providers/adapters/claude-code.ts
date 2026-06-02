import { api } from '../../api/client'
import modelTokensRaw from '../../model_tokens.json'
import type {
  ProviderAdapter,
  ProviderCapabilities,
  ProviderLabels,
  AgentBinding,
  NormalizedUsage,
  NormalizedProviderHook,
  NormalizedPlugin,
  ModelInfo,
  PermissionMode,
} from '../types'

const MODEL_TOKENS: Record<string, number> = Object.fromEntries(
  Object.entries(modelTokensRaw).filter(([k]) => !k.startsWith('_')) as [string, number][]
)
const DEFAULT_CONTEXT = 200_000

// KiroClaw's resolved default CC model. Must match the backend
// _CC_DEFAULT_MODEL (providers/claude_code.py). An empty cc_model resolves to
// this rather than the bare 'opus' alias — the adapter expands 'opus' to its
// own models[0], which on current claude-agent builds is an OLD Opus (4.1).
const CC_DEFAULT_MODEL = 'global.anthropic.claude-opus-4-8[1m]'

export class ClaudeCodeAdapter implements ProviderAdapter {
  readonly id = 'claude_code' as const
  readonly displayName = 'Claude Code'

  readonly capabilities: ProviderCapabilities = {
    hooks: false,
    pluginRegistry: true,
    agentTemplates: false,
    usageBilling: true,
    warmPool: true,
    modelResolution: true,
    toolExecution: true,
    subagents: true,
    sandbox: true,
    contextWindow: true,
    modelSwitching: true,
    permissionModes: true,
    sessionResume: true,
    compaction: true,
    reasoningEffort: true,
  }

  readonly labels: ProviderLabels = {
    sessionProcess: 'claude-code subprocess',
    agentTemplateField: 'CLAUDE.md Config',
    processCountLabel: 'claude_code',
    warmPoolDescription: 'Pre-spawn Claude Code processes for instant session start.',
    configFile: 'settings.json',
    pluginRegistryName: 'AIM Plugins',
    hooksSection: 'Claude Code Hooks',
  }

  resolveAgentTemplate(agent: AgentBinding): string {
    return agent.name
  }

  async resolveModel(_templateName: string): Promise<string> {
    try {
      const config = await api.kiroclawConfig()
      return config?.agent?.cc_model || CC_DEFAULT_MODEL
    } catch {
      return CC_DEFAULT_MODEL
    }
  }

  async fetchUsage(): Promise<NormalizedUsage> {
    const data = await api.providerUsage()
    const s = data.sessions || {}
    const tokens = data.tokens
    return {
      sessions: {
        total: s.total_sessions ?? 0,
        today: { sessions: s.today?.sessions ?? 0, messages: s.today?.messages ?? 0, toolCalls: s.today?.tool_calls ?? 0 },
        thisWeek: { sessions: s.this_week?.sessions ?? 0, messages: s.this_week?.messages ?? 0, toolCalls: s.this_week?.tool_calls ?? 0 },
        thisMonth: { sessions: s.this_month?.sessions ?? 0, messages: s.this_month?.messages ?? 0, toolCalls: s.this_month?.tool_calls ?? 0 },
        avgMsgsPerSession: s.avg_msgs_per_session ?? 0,
        dailyHistory: (s.daily_history || []).map((d: any) => ({
          date: d.date,
          sessions: d.sessions,
          messages: d.messages,
          toolCalls: d.tool_calls,
        })),
      },
      billing: data.budget ? {
        plan: 'Per-session budget',
        used: data.budget.spent_usd,
        limit: data.budget.max_usd || undefined,
        unit: 'usd',
        percentUsed: data.budget.max_usd ? Math.round((data.budget.spent_usd ?? 0) / data.budget.max_usd * 100) : undefined,
      } : null,
      tokens: tokens ? {
        input: tokens.total_input ?? 0,
        output: tokens.total_output ?? 0,
        cacheCreation: tokens.cache_creation ?? 0,
        cacheRead: tokens.cache_read ?? 0,
        total: tokens.total ?? 0,
      } : undefined,
      costUsd: data.cost_usd ?? undefined,
      totalTurns: data.total_turns ?? undefined,
      totalDurationMs: data.total_duration_ms ?? undefined,
      tokenDailyHistory: (data.token_daily_history || []).map((d: any) => ({
        date: d.date,
        input: d.input ?? 0,
        output: d.output ?? 0,
        cacheCreate: d.cache_create ?? 0,
        cacheRead: d.cache_read ?? 0,
        costUsd: d.cost_usd ?? 0,
        models: d.models ? Object.fromEntries(
          Object.entries(d.models).map(([k, v]: [string, any]) => [k, {
            input: v.input ?? 0,
            output: v.output ?? 0,
            cacheCreate: v.cache_create ?? 0,
            cacheRead: v.cache_read ?? 0,
            costUsd: v.cost_usd ?? 0,
          }])
        ) : undefined,
        providers: d.providers ? Object.fromEntries(
          Object.entries(d.providers).map(([k, v]: [string, any]) => [k, {
            input: v.input ?? 0,
            output: v.output ?? 0,
            cacheCreate: v.cache_create ?? 0,
            cacheRead: v.cache_read ?? 0,
            costUsd: v.cost_usd ?? 0,
          }])
        ) : undefined,
        providerModels: d.provider_models ? Object.fromEntries(
          Object.entries(d.provider_models).map(([p, models]: [string, any]) => [p, Object.fromEntries(
            Object.entries(models).map(([m, v]: [string, any]) => [m, {
              input: v.input ?? 0,
              output: v.output ?? 0,
              cacheCreate: v.cache_create ?? 0,
              cacheRead: v.cache_read ?? 0,
              costUsd: v.cost_usd ?? 0,
            }])
          )])
        ) : undefined,
      })),
      tokenProviders: data.token_providers ?? [],
      tokenModels: data.token_models ?? [],
      tokenProviderModels: data.token_provider_models ?? {},
    }
  }

  async fetchProviderHooks(): Promise<Record<string, NormalizedProviderHook[]>> {
    try {
      const data = await api.hooks()
      const hooks = data.hooks || data || []
      const result: Record<string, NormalizedProviderHook[]> = {}
      for (const h of hooks as any[]) {
        const event = h.event || 'unknown'
        if (!result[event]) result[event] = []
        result[event].push({
          event,
          command: h.command,
          matcher: h.matcher,
          source: 'settings.json',
        })
      }
      return result
    } catch {
      return {}
    }
  }

  async listPlugins(): Promise<NormalizedPlugin[]> {
    const [skills, mcp] = await Promise.all([
      api.aimSkillsList().catch(() => []),
      api.aimMcpList().catch(() => []),
    ])
    const plugins: NormalizedPlugin[] = []
    for (const s of skills as any[]) {
      plugins.push({ id: s.package || s.name, name: s.name, type: 'skill', source: 'aim-plugin', version: s.version })
    }
    for (const m of mcp as any[]) {
      plugins.push({ id: m.server_id || m.name, name: m.name, type: 'mcp', source: 'aim-plugin', version: m.version })
    }
    return plugins
  }

  async installPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp', versionSet?: string) {
    if (type === 'skill') return api.aimSkillsInstall(pkg, versionSet)
    if (type === 'mcp') return api.aimMcpInstall(pkg)
    return { ok: false, error: 'Claude Code does not support standalone agent template installation. Use AIM plugins.' }
  }

  async uninstallPlugin(pkg: string, type: 'agent' | 'skill' | 'mcp') {
    if (type === 'skill') return api.aimSkillsUninstall(pkg)
    if (type === 'mcp') return api.aimMcpUninstall(pkg)
    return { ok: false, error: 'Not supported for this plugin type.' }
  }

  async updatePlugins(type: 'agent' | 'skill' | 'mcp') {
    return api.aimUpdate(type === 'mcp' ? 'mcp' : 'skills')
  }

  async fetchAvailableModels(): Promise<ModelInfo[]> {
    try {
      const models = await api.models()
      if (!Array.isArray(models)) return this._defaultModels()
      const result = models.map((m: any) => {
        // name is the model ID sent to the backend; surface the friendly
        // display_name (e.g. "Opus 4.8 (1M context)") in the description so
        // the dropdown is readable while selection still passes the real ID.
        const friendly = m.display_name && m.display_name !== m.model_name ? m.display_name : ''
        const desc = [friendly, m.description].filter(Boolean).join(' · ')
        const ctx = m.model_name.includes('[1m]') || /(^|[^a-z])1m([^a-z]|$)/i.test(m.model_name)
          ? 1_000_000
          : MODEL_TOKENS[m.model_name] ?? DEFAULT_CONTEXT
        return {
          name: m.model_name,
          description: desc,
          contextWindow: ctx,
          supportsExtendedContext: ctx >= 1_000_000,
        }
      })
      return result.length > 0 ? result : this._defaultModels()
    } catch {
      return this._defaultModels()
    }
  }

  getContextWindow(model: string): number {
    if (model.includes('[1m]') || model.includes('1m')) return 1_000_000
    return MODEL_TOKENS[model] ?? DEFAULT_CONTEXT
  }

  getDefaultModel(): string {
    // Opus 4.8 with the 1M context window. See claude-agent-acp docs for
    // the explicit Bedrock model ID syntax: global.anthropic.<model>[<window>].
    return CC_DEFAULT_MODEL
  }

  getPermissionModes(): PermissionMode[] {
    return [
      { id: 'default', label: 'Default', description: 'Approve tool calls interactively via callback' },
      { id: 'plan', label: 'Plan (Read-only)', description: 'Only allow read operations, block all writes' },
      { id: 'bypassPermissions', label: 'Bypass Permissions', description: 'Skip all permission prompts (--dangerously-skip-permissions)' },
    ]
  }

  private _defaultModels(): ModelInfo[] {
    // Mirrors the set the `claude` CLI actually offers (Opus 4.8 1M, Opus 4.7,
    // Opus 4.6, Sonnet 4.6), most-capable first. Used only when /api/models
    // returns nothing; the live list comes from the backend (curated-first).
    // Full Bedrock inference-profile ids (global.anthropic.…) — bare versioned
    // ids are rejected by Bedrock with a 400. Matches the backend curated set
    // so the dropdown never shows duplicates. [1m] = 1M context window.
    return [
      { name: 'global.anthropic.claude-opus-4-8[1m]', description: 'Claude Opus 4.8 (default, 1M context)', contextWindow: 1_000_000 },
      { name: 'global.anthropic.claude-opus-4-8', description: 'Claude Opus 4.8 (200K context)', contextWindow: 200_000 },
      { name: 'global.anthropic.claude-opus-4-7[1m]', description: 'Opus 4.7 · Most capable for complex work, 1M context', contextWindow: 1_000_000 },
      { name: 'global.anthropic.claude-sonnet-4-6[1m]', description: 'Sonnet 4.6 · Best for everyday tasks, 1M context', contextWindow: 1_000_000 },
    ]
  }
}
