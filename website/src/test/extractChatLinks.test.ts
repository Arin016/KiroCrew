import { describe, it, expect } from 'vitest'
import { extractChatLinks } from '../utils/extractChatLinks'
import type { ChatMessage } from '../types'

function msg(role: string, content: string): ChatMessage {
  return { role, content, ts: '2026-01-01T00:00:00Z' } as ChatMessage
}

describe('extractChatLinks', () => {
  it('extracts markdown links with text as label', () => {
    const messages = [msg('user', 'Check [Design Doc](https://example.com/abc123)')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].label).toBe('Design Doc')
    expect(links[0].url).toBe('https://example.com/abc123')
    expect(links[0].type).toBe('other')
    expect(links[0].fromMarkdown).toBe(true)
  })

  it('extracts bare URLs with fallback label', () => {
    const messages = [msg('user', 'see https://github.com/acme/repo/pull/42')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].label).toBe('#42')
    expect(links[0].type).toBe('cr')
    expect(links[0].fromMarkdown).toBeUndefined()
  })

  it('deduplicates same URL', () => {
    const messages = [
      msg('user', 'https://example.com/abc'),
      msg('assistant', 'https://example.com/abc'),
    ]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
  })

  it('does not dedup URLs with different fragments', () => {
    const messages = [
      msg('user', 'https://github.com/acme/repo/pull/123/files#diff-1'),
      msg('user', 'https://github.com/acme/repo/pull/123'),
    ]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(2)
  })

  it('classifies URL types correctly', () => {
    const messages = [msg('user', [
      'https://github.com/acme/repo/pull/111',
      'https://gitlab.com/acme/repo/-/merge_requests/7',
      'https://bitbucket.org/acme/repo/pull-requests/3',
      'https://example.com/other',
    ].join('\n'))]
    const links = extractChatLinks(messages)
    expect(links.map(l => l.type)).toEqual(['cr', 'cr', 'cr', 'other'])
  })

  it('handles 2-letter TLD bare domains', () => {
    const messages = [msg('user', 'check https://cursor.ai for details')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].url).toBe('https://cursor.ai')
  })

  it('does not include trailing dots in URL', () => {
    const messages = [msg('user', 'see https://example.com/path...')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].url).not.toContain('...')
  })

  it('skips non-user/assistant messages', () => {
    const messages = [msg('system', 'https://example.com/abc')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(0)
  })

  it('uses fallback for short markdown text', () => {
    const messages = [msg('user', '[ab](https://github.com/acme/repo/pull/999)')]
    const links = extractChatLinks(messages)
    expect(links[0].label).toBe('#999') // fallback because 'ab' < 3 chars
  })
})
