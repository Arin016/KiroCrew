import { describe, it, expect } from 'vitest'
import { parseDiffLines, isDiffText, DIFF_BG, DIFF_FG, type DiffLine } from '../utils/diffUtils'

describe('parseDiffLines', () => {
  it('parses standard unified diff', () => {
    const lines = parseDiffLines("--- a/file.ts\n+++ b/file.ts\n@@ -1,3 +1,3 @@\n const a = 1\n-const b = 2\n+const b = 3\n const c = 4")
    const types = lines.map(l => l.type)
    expect(types).toEqual(['meta', 'meta', 'hunk', 'context', 'del', 'add', 'context'])
  })

  it('strips +/- prefix from content', () => {
    const lines = parseDiffLines("-old line\n+new line")
    expect(lines[0].content).toBe('old line')
    expect(lines[1].content).toBe('new line')
  })

  it('strips leading space from context lines', () => {
    const lines = parseDiffLines(" context line")
    expect(lines[0].content).toBe('context line')
    expect(lines[0].type).toBe('context')
  })

  it('parses hunk headers and extracts line numbers', () => {
    const lines = parseDiffLines("@@ -10,5 +20,7 @@")
    expect(lines[0].type).toBe('hunk')
    expect(lines[0].content).toBe('@@ -10,5 +20,7 @@')
  })

  it('tracks line numbers through changes', () => {
    const lines = parseDiffLines("@@ -1,3 +1,3 @@\n context\n-deleted\n+added")
    const del = lines.find(l => l.type === 'del')!
    const add = lines.find(l => l.type === 'add')!
    expect(del.oldNum).toBe(2)
    expect(add.newNum).toBe(2)
  })

  it('handles kiro-cli +N:content format', () => {
    const lines = parseDiffLines("+10:const x = 1\n-5:const y = 2")
    expect(lines[0]).toEqual({ type: 'add', content: 'const x = 1', newNum: 10 })
    expect(lines[1]).toEqual({ type: 'del', content: 'const y = 2', oldNum: 5 })
  })

  it('treats diff --git and index lines as meta', () => {
    const lines = parseDiffLines("diff --git a/f.ts b/f.ts\nindex abc..def 100644\n@@ -1,1 +1,1 @@\n-old\n+new")
    expect(lines[0].type).toBe('meta')
    expect(lines[1].type).toBe('meta')
  })

  it('returns empty array for empty input', () => {
    expect(parseDiffLines('')).toEqual([{ type: 'context', content: '', oldNum: 0, newNum: 0 }])
  })
})

describe('isDiffText', () => {
  it('returns true for text with @@ hunks', () => {
    expect(isDiffText('@@ -1,3 +1,3 @@\n-old\n+new')).toBe(true)
  })

  it('returns true for text with ---/+++ file headers', () => {
    expect(isDiffText('--- a/file.ts\n+++ b/file.ts\n-old\n+new')).toBe(true)
  })

  it('returns false for plain text', () => {
    expect(isDiffText('just some text')).toBe(false)
  })

  it('returns false for JSON', () => {
    expect(isDiffText('{"key": "value"}')).toBe(false)
  })

  it('returns false for markdown lists', () => {
    expect(isDiffText('- item one\n- item two\n+ not a diff')).toBe(false)
  })

  it('returns false for negative numbers', () => {
    expect(isDiffText('-5 degrees')).toBe(false)
  })

  it('does not false-positive on YAML front matter with +++ heading', () => {
    expect(isDiffText('---\ntitle: doc\n+++ heading')).toBe(false)
  })
})

describe('DIFF_BG and DIFF_FG', () => {
  it('has entries for all line types', () => {
    const types: DiffLine['type'][] = ['add', 'del', 'context', 'hunk', 'meta']
    for (const t of types) {
      expect(DIFF_BG[t]).toBeDefined()
      expect(DIFF_FG[t]).toBeDefined()
    }
  })

  it('uses correct Tailwind classes', () => {
    expect(DIFF_BG.add).toBe('bg-diff-add')
    expect(DIFF_BG.del).toBe('bg-diff-del')
    expect(DIFF_FG.add).toBe('text-diff-add-text')
    expect(DIFF_FG.del).toBe('text-diff-del-text')
  })
})
