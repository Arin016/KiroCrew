import { describe, it, expect } from 'vitest'
import { findBestOccurrence } from '../hooks/useMarkdownCommentHighlights'

describe('MarkdownPanel inline highlight matching (startOffset)', () => {
  it('returns first occurrence when no startOffset is stored (backward compat)', () => {
    const text = 'foo bar foo baz foo'
    expect(findBestOccurrence(text, 'foo')).toBe(0)
    expect(findBestOccurrence(text, 'foo', undefined)).toBe(0)
  })

  it('picks second occurrence when startOffset matches it', () => {
    const text = 'foo bar foo baz foo'
    const second = text.indexOf('foo', 1) // 8
    expect(findBestOccurrence(text, 'foo', second)).toBe(second)
  })

  it('picks third occurrence when startOffset matches it', () => {
    const text = 'foo bar foo baz foo'
    const third = text.lastIndexOf('foo') // 16
    expect(findBestOccurrence(text, 'foo', third)).toBe(third)
  })

  it('picks the nearest occurrence when startOffset is between two matches', () => {
    // "foo" at 0, 8, 16. startOffset=10 is nearest to 8.
    const text = 'foo bar foo baz foo'
    expect(findBestOccurrence(text, 'foo', 10)).toBe(8)
  })

  it('picks the nearest occurrence when startOffset is past all matches', () => {
    const text = 'foo bar foo baz foo'
    const last = text.lastIndexOf('foo') // 16
    expect(findBestOccurrence(text, 'foo', 9999)).toBe(last)
  })

  it('returns -1 when anchor is not found', () => {
    expect(findBestOccurrence('hello world', 'missing')).toBe(-1)
    expect(findBestOccurrence('hello world', 'missing', 5)).toBe(-1)
  })

  it('handles single occurrence with startOffset correctly', () => {
    const text = 'unique phrase here'
    expect(findBestOccurrence(text, 'unique phrase', 0)).toBe(0)
    expect(findBestOccurrence(text, 'unique phrase', 999)).toBe(0) // only one, always picks it
  })

  it('handles identical repeated sentences', () => {
    const text = 'set the value. set the value. set the value.'
    const first = 0
    const second = text.indexOf('set the value', 1) // 15
    const third = text.lastIndexOf('set the value') // 30
    expect(findBestOccurrence(text, 'set the value', first)).toBe(first)
    expect(findBestOccurrence(text, 'set the value', second)).toBe(second)
    expect(findBestOccurrence(text, 'set the value', third)).toBe(third)
  })
})
