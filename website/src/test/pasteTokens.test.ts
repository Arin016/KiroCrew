import { describe, it, expect } from 'vitest'
import {
  shouldCollapse,
  countLines,
  formatToken,
  makePasteId,
  nextSeq,
  findTokenRanges,
  tokenRangeAt,
  pruneBlocks,
  expandAll,
  mergePreservedPastes,
  PASTE_TOKEN_REGEX,
  type PasteBlock,
} from '../utils/pasteTokens'

const block = (overrides: Partial<PasteBlock> = {}): PasteBlock => ({
  id: overrides.id ?? makePasteId(),
  seq: overrides.seq ?? 1,
  lines: overrides.lines ?? 3,
  content: overrides.content ?? 'a\nb\nc',
})

describe('pasteTokens', () => {
  describe('shouldCollapse', () => {
    it('collapses 3-line paste', () => { expect(shouldCollapse('a\nb\nc')).toBe(true) })
    it('collapses 200+ char one-liner', () => { expect(shouldCollapse('x'.repeat(250))).toBe(true) })
    it('does not collapse 2-line short paste', () => { expect(shouldCollapse('a\nb')).toBe(false) })
    it('does not collapse empty', () => { expect(shouldCollapse('')).toBe(false) })
  })

  describe('countLines', () => {
    it('1 for no newlines', () => { expect(countLines('hello')).toBe(1) })
    it('trailing newline counts', () => { expect(countLines('a\nb\n')).toBe(3) })
    it('empty = 0', () => { expect(countLines('')).toBe(0) })
  })

  describe('formatToken + regex', () => {
    it('produces canonical token', () => {
      expect(formatToken(block({ seq: 3, lines: 42 }))).toBe('[ Paste #3 · 42 lines ]')
    })
    it('regex captures seq and lines', () => {
      const b = block({ seq: 7, lines: 12 })
      const s = `hey ${formatToken(b)} there`
      PASTE_TOKEN_REGEX.lastIndex = 0
      const m = PASTE_TOKEN_REGEX.exec(s)
      expect(m?.[1]).toBe('7')
      expect(m?.[2]).toBe('12')
    })
  })

  describe('nextSeq', () => {
    it('starts at 1 when empty', () => { expect(nextSeq([])).toBe(1) })
    it('is max+1 regardless of gaps', () => {
      expect(nextSeq([block({ seq: 1 }), block({ seq: 4 })])).toBe(5)
    })
  })

  describe('findTokenRanges', () => {
    it('pairs by seq', () => {
      const b1 = block({ id: 'a', seq: 1, lines: 3, content: 'X' })
      const b2 = block({ id: 'b', seq: 2, lines: 9, content: 'Y' })
      const text = `${formatToken(b1)} mid ${formatToken(b2)}`
      const r = findTokenRanges(text, [b1, b2])
      expect(r.map(x => x.block.id)).toEqual(['a', 'b'])
    })

    it('preserves document order even with seq gaps', () => {
      const b1 = block({ id: 'a', seq: 5 })
      const b2 = block({ id: 'b', seq: 2 })
      const text = `${formatToken(b2)} ${formatToken(b1)}`
      const r = findTokenRanges(text, [b1, b2])
      expect(r.map(x => x.block.id)).toEqual(['b', 'a'])
    })

    it('ignores tokens whose seq is unknown', () => {
      const known = block({ id: 'k', seq: 1 })
      const unknown = block({ id: 'u', seq: 99 })
      const text = `${formatToken(known)} ${formatToken(unknown)}`
      const r = findTokenRanges(text, [known])
      expect(r).toHaveLength(1)
      expect(r[0].block.id).toBe('k')
    })
  })

  describe('tokenRangeAt', () => {
    it('returns range when caret is inside', () => {
      const b = block({ seq: 1 })
      const text = `xx ${formatToken(b)} yy`
      const caret = text.indexOf('[') + 4
      expect(tokenRangeAt(text, [b], caret)?.block.id).toBe(b.id)
    })
    it('returns null outside', () => {
      expect(tokenRangeAt(`a ${formatToken(block())} b`, [block()], 0)).toBeNull()
    })
  })

  describe('pruneBlocks', () => {
    it('drops blocks without a surviving token', () => {
      const keep = block({ id: 'k', seq: 1 })
      const drop = block({ id: 'd', seq: 2 })
      expect(pruneBlocks(`${formatToken(keep)}`, [keep, drop])).toEqual([keep])
    })
    it('returns same ref if unchanged', () => {
      const b = block({ seq: 1 })
      const input = [b]
      expect(pruneBlocks(formatToken(b), input)).toBe(input)
    })
  })

  describe('expandAll', () => {
    it('inlines content', () => {
      const b1 = block({ id: 'a', seq: 1, content: 'AAA' })
      const b2 = block({ id: 'b', seq: 2, content: 'BBB' })
      expect(expandAll(`x ${formatToken(b1)} y ${formatToken(b2)} z`, [b1, b2]))
        .toBe('x AAA y BBB z')
    })
    it('leaves unknown-seq tokens alone', () => {
      const known = block({ seq: 1, content: 'K' })
      const unknown = block({ seq: 99 })
      expect(expandAll(`${formatToken(known)} and ${formatToken(unknown)}`, [known]))
        .toBe(`K and ${formatToken(unknown)}`)
    })
  })

  describe('mergePreservedPastes', () => {
    it('re-attaches tokens + pastes when expansion matches incoming content', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `prefix ${formatToken(b)} suffix`, meta: { pastes: [b] } },
        { role: 'assistant', content: 'ok' },
      ]
      const incoming = [
        { role: 'user', content: 'prefix line1\nline2\nline3 suffix' }, // backend-expanded
        { role: 'assistant', content: 'ok' },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe(existing[0].content)
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect(out[1].content).toBe('ok')
    })

    it('returns incoming unchanged when no existing pastes', () => {
      const incoming = [{ role: 'user', content: 'hi' }]
      expect(mergePreservedPastes([{ role: 'user', content: 'hi' }], incoming)).toBe(incoming)
    })

    it('consumes FIFO when multiple user messages have pastes', () => {
      const b1 = block({ id: 'a', seq: 1, content: 'AAA' })
      const b2 = block({ id: 'b', seq: 1, content: 'BBB' })
      const existing = [
        { role: 'user', content: `X ${formatToken(b1)}`, meta: { pastes: [b1] } },
        { role: 'assistant', content: 'r1' },
        { role: 'user', content: `Y ${formatToken(b2)}`, meta: { pastes: [b2] } },
      ]
      const incoming = [
        { role: 'user', content: 'X AAA' },
        { role: 'assistant', content: 'r1' },
        { role: 'user', content: 'Y BBB' },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe(existing[0].content)
      expect(out[2].content).toBe(existing[2].content)
    })

    it('leaves message alone when no existing entry matches its expanded content', () => {
      const b = block({ id: 'a', seq: 1, content: 'AAA' })
      const existing = [{ role: 'user', content: `X ${formatToken(b)}`, meta: { pastes: [b] } }]
      const incoming = [
        { role: 'user', content: 'some other text' }, // no match
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe('some other text')
      expect(out[0].meta).toBeUndefined()
    })
  })
})
