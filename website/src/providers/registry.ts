import type { ProviderAdapter, ProviderId } from './types'
import { AcpAdapter } from './adapters/acp'
import { ClaudeCodeAdapter } from './adapters/claude-code'
import { BedrockAdapter } from './adapters/bedrock'

const ADAPTERS: Record<ProviderId, ProviderAdapter> = {
  acp: new AcpAdapter(),
  claude_code: new ClaudeCodeAdapter(),
  bedrock: new BedrockAdapter(),
}

export function getAdapter(id: ProviderId): ProviderAdapter {
  // claude_code is the default provider; fall back to it for unknown ids.
  return ADAPTERS[id] ?? ADAPTERS.claude_code
}

export function getAllProviderIds(): ProviderId[] {
  return Object.keys(ADAPTERS) as ProviderId[]
}
