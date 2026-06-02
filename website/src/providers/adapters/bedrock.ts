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

export class BedrockAdapter implements ProviderAdapter {
  readonly id = 'bedrock' as const
  readonly displayName = 'Bedrock'

  readonly capabilities: ProviderCapabilities = {
    hooks: false,
    pluginRegistry: false,
    agentTemplates: false,
    usageBilling: true,
    warmPool: false,
    modelResolution: true,
    toolExecution: false,
    subagents: false,
    sandbox: false,
    contextWindow: true,
    modelSwitching: false,
    permissionModes: false,
    sessionResume: false,
    compaction: false,
    reasoningEffort: false,
  }

  readonly labels: ProviderLabels = {
    sessionProcess: 'Bedrock HTTP API',
    agentTemplateField: 'Model ID',
    processCountLabel: 'bedrock_api',
    warmPoolDescription: '',
    configFile: '',
    pluginRegistryName: '',
    hooksSection: '',
  }

  resolveAgentTemplate(agent: AgentBinding): string {
    return agent.name
  }

  async resolveModel(_templateName: string): Promise<string> {
    try {
      const config = await api.kiroclawConfig()
      return config?.agent?.bedrock_model_id || ''
    } catch {
      return ''
    }
  }

  async fetchUsage(): Promise<NormalizedUsage> {
    const data = await api.providerUsage()
    const s = data.sessions || {}
    return {
      sessions: {
        total: s.total_sessions ?? 0,
        today: { sessions: s.today?.sessions ?? 0, messages: s.today?.messages ?? 0, toolCalls: 0 },
        thisWeek: { sessions: s.this_week?.sessions ?? 0, messages: s.this_week?.messages ?? 0, toolCalls: 0 },
        thisMonth: { sessions: s.this_month?.sessions ?? 0, messages: s.this_month?.messages ?? 0, toolCalls: 0 },
        avgMsgsPerSession: s.avg_msgs_per_session ?? 0,
        dailyHistory: (s.daily_history || []).map((d: any) => ({
          date: d.date,
          sessions: d.sessions,
          messages: d.messages,
          toolCalls: 0,
        })),
      },
      billing: data.tokens ? {
        plan: 'Token usage',
        used: data.tokens.total,
        limit: undefined,
        unit: 'tokens',
      } : null,
    }
  }

  async fetchProviderHooks(): Promise<Record<string, NormalizedProviderHook[]>> {
    return {}
  }

  async listPlugins(): Promise<NormalizedPlugin[]> {
    return []
  }

  async installPlugin(_pkg: string, _type: 'agent' | 'skill' | 'mcp') {
    return { ok: false, error: 'Bedrock provider does not support plugins.' } as const
  }

  async uninstallPlugin(_pkg: string, _type: 'agent' | 'skill' | 'mcp') {
    return { ok: false, error: 'Bedrock provider does not support plugins.' } as const
  }

  async updatePlugins(_type: 'agent' | 'skill' | 'mcp') {
    return { ok: false, error: 'Bedrock provider does not support plugins.' } as const
  }

  async fetchAvailableModels(): Promise<ModelInfo[]> {
    return [
      { name: 'anthropic.claude-sonnet-4-20250514-v1:0', description: 'Claude Sonnet 4 (Bedrock)', contextWindow: 200_000 },
      { name: 'us.anthropic.claude-sonnet-4-20250514-v1:0', description: 'Claude Sonnet 4 (US cross-region)', contextWindow: 200_000 },
      { name: 'us.anthropic.claude-3-7-sonnet-20250219-v1:0', description: 'Claude 3.7 Sonnet (US)', contextWindow: 200_000 },
      { name: 'amazon.nova-pro-v1:0', description: 'Amazon Nova Pro', contextWindow: 300_000 },
      { name: 'amazon.nova-lite-v1:0', description: 'Amazon Nova Lite', contextWindow: 300_000 },
    ]
  }

  getContextWindow(model: string): number {
    return MODEL_TOKENS[model] ?? DEFAULT_CONTEXT
  }

  getDefaultModel(): string {
    return 'anthropic.claude-sonnet-4-20250514-v1:0'
  }

  getPermissionModes(): PermissionMode[] {
    return []
  }
}
