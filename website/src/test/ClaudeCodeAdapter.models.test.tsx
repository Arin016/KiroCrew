import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
    kiroclawConfig: vi.fn().mockResolvedValue({ agent: { cc_model: 'opus' } }),
  },
}))

import { api } from '../api/client'
import { ClaudeCodeAdapter } from '../providers/adapters/claude-code'

describe('ClaudeCodeAdapter.fetchAvailableModels', () => {
  beforeEach(() => vi.clearAllMocks())

  it('surfaces the backend-advertised versioned models with friendly labels', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'claude-opus-4-8-1m', display_name: 'Opus 4.8 (1M context)', description: 'Newest' },
      { model_name: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6', description: 'Everyday tasks' },
    ])
    const models = await new ClaudeCodeAdapter().fetchAvailableModels()
    // name stays the real model ID the backend expects on switch
    expect(models[0].name).toBe('claude-opus-4-8-1m')
    // friendly display_name folded into the description
    expect(models[0].description).toBe('Opus 4.8 (1M context) · Newest')
    // a -1m id is recognized as an extended (1M) context window
    expect(models[0].contextWindow).toBe(1_000_000)
    expect(models[0].supportsExtendedContext).toBe(true)
    expect(models[1].name).toBe('claude-sonnet-4-6')
    expect(models[1].description).toBe('Sonnet 4.6 · Everyday tasks')
  })

  it('omits a redundant display_name equal to the model id', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'opus', display_name: 'opus', description: 'default' },
    ])
    const models = await new ClaudeCodeAdapter().fetchAvailableModels()
    expect(models[0].description).toBe('default')
  })

  it('falls back to the static catalog when the API returns nothing', async () => {
    ;(api.models as any).mockResolvedValue([])
    const models = await new ClaudeCodeAdapter().fetchAvailableModels()
    expect(models.length).toBeGreaterThan(0)
    expect(models.some(m => m.name.includes('opus'))).toBe(true)
  })

  it('falls back when the API throws', async () => {
    ;(api.models as any).mockRejectedValue(new Error('boom'))
    const models = await new ClaudeCodeAdapter().fetchAvailableModels()
    expect(models.length).toBeGreaterThan(0)
  })
})

describe('ClaudeCodeAdapter.resolveModel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('resolves an empty cc_model to the canonical default key, not bare opus', async () => {
    // An unconfigured user must get the explicit canonical default; the backend
    // translates it to the Opus 4.8 1M provider id at the factory boundary.
    ;(api.kiroclawConfig as any).mockResolvedValue({ agent: { cc_model: '' } })
    const model = await new ClaudeCodeAdapter().resolveModel('kiroclaw')
    expect(model).toBe('opus-4.8-1m')
  })

  it('honors an explicitly configured cc_model', async () => {
    ;(api.kiroclawConfig as any).mockResolvedValue({ agent: { cc_model: 'claude-opus-4.7' } })
    const model = await new ClaudeCodeAdapter().resolveModel('kiroclaw')
    expect(model).toBe('claude-opus-4.7')
  })

  it('falls back to the canonical default when the config call throws', async () => {
    ;(api.kiroclawConfig as any).mockRejectedValue(new Error('boom'))
    const model = await new ClaudeCodeAdapter().resolveModel('kiroclaw')
    expect(model).toBe('opus-4.8-1m')
  })

  it('getDefaultModel returns the canonical default key', () => {
    expect(new ClaudeCodeAdapter().getDefaultModel()).toBe('opus-4.8-1m')
  })

  it('getContextWindow resolves canonical key + provider id + [1m] to 1M', () => {
    const a = new ClaudeCodeAdapter()
    expect(a.getContextWindow('opus-4.8-1m')).toBe(1_000_000)
    expect(a.getContextWindow('global.anthropic.claude-opus-4-8[1m]')).toBe(1_000_000)
    expect(a.getContextWindow('opus-4.8')).toBe(200_000)
  })

  it('1m boundary excludes digits, matching the Python _has_1m_token', () => {
    const a = new ClaudeCodeAdapter()
    // A standalone 1m token still resolves to 1M (unlisted, forward-compat).
    expect(a.getContextWindow('claude-future-1m')).toBe(1_000_000)
    // "10m"/"21m" must NOT false-match the 1m token (digit boundary) — parity
    // with the backend model_registry.window(), else FE shows 1M / BE 200K.
    expect(a.getContextWindow('some-10m-model')).toBe(200_000)
    expect(a.getContextWindow('weird-21m-thing')).toBe(200_000)
  })
})
