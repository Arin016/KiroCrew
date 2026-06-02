import { describe, it, expect, vi, afterEach } from 'vitest'

describe('Content Width', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('CONTENT_WIDTH map has correct values', async () => {
    const { CONTENT_WIDTH } = await vi.importActual('../pages/chat/ChatSettings') as any
    expect(CONTENT_WIDTH.compact.messages).toBe('900px')
    expect(CONTENT_WIDTH.compact.input).toBe('916px')
    expect(CONTENT_WIDTH.comfortable.messages).toBe('84%')
    expect(CONTENT_WIDTH.comfortable.input).toBe('85%')
    expect(CONTENT_WIDTH.full.messages).toBe('92%')
    expect(CONTENT_WIDTH.full.input).toBe('93%')
  })

  it('loadChatConfig falls back to compact on invalid contentWidth', async () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem')
      .mockReturnValue(JSON.stringify({ contentWidth: 'bogus' }))
    const { loadChatConfig } = await vi.importActual('../pages/chat/ChatSettings') as any
    expect(loadChatConfig().contentWidth).toBe('compact')
    spy.mockRestore()
  })

  it('loadChatConfig preserves valid contentWidth', async () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem')
      .mockReturnValue(JSON.stringify({ contentWidth: 'full' }))
    const { loadChatConfig } = await vi.importActual('../pages/chat/ChatSettings') as any
    expect(loadChatConfig().contentWidth).toBe('full')
    spy.mockRestore()
  })
})
