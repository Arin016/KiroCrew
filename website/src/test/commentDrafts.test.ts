import { describe, it, expect, beforeEach } from 'vitest'
import {
  COMMENT_DRAFTS_KEY,
  COMMENT_DRAFT_MAX_FILES,
  loadCommentDrafts,
  saveCommentDrafts,
  setCommentsForFile,
} from '../utils/commentDrafts'
import type { InlineComment } from '../components/CommentOverlay'

const mk = (id: string, anchor: string, text: string): InlineComment => ({ id, anchor, text })

describe('commentDrafts', () => {
  beforeEach(() => { localStorage.clear() })

  it('roundtrips drafts through localStorage (survives close/refresh)', () => {
    const drafts = {
      '/a/README.md': [mk('1', 'hello', 'fix typo')],
      '/b/notes.md': [mk('2', 'world', 'add context'), mk('3', 'foo', 'rename')],
    }
    saveCommentDrafts(drafts)
    expect(loadCommentDrafts()).toEqual(drafts)
    expect(localStorage.getItem(COMMENT_DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadCommentDrafts()).toEqual({})
    localStorage.setItem(COMMENT_DRAFTS_KEY, 'not json')
    expect(loadCommentDrafts()).toEqual({})
    localStorage.setItem(COMMENT_DRAFTS_KEY, '[]')
    expect(loadCommentDrafts()).toEqual({})
    localStorage.setItem(COMMENT_DRAFTS_KEY, 'null')
    expect(loadCommentDrafts()).toEqual({})
  })

  it('drops entries with the wrong shape (defensive load)', () => {
    localStorage.setItem(COMMENT_DRAFTS_KEY, JSON.stringify({
      '/good.md': [mk('1', 'a', 'b')],
      '/bad-not-array.md': 'oops',
      '/bad-empty.md': [],
      '/bad-missing-fields.md': [{ id: 'x' }],
    }))
    expect(loadCommentDrafts()).toEqual({ '/good.md': [mk('1', 'a', 'b')] })
  })

  it('setCommentsForFile stores non-empty and deletes empty', () => {
    const d: Record<string, InlineComment[]> = { '/a.md': [mk('1', 'x', 'y')] }
    setCommentsForFile(d, '/a.md', [mk('2', 'p', 'q')])
    expect(d).toEqual({ '/a.md': [mk('2', 'p', 'q')] })
    setCommentsForFile(d, '/a.md', [])
    expect(d).toEqual({})
  })

  it('saveCommentDrafts swallows QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(() => saveCommentDrafts({ '/a.md': [mk('1', 'x', 'y')] })).not.toThrow()
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('saveCommentDrafts does not mutate caller if setItem throws (no silent data loss)', () => {
    const drafts: Record<string, InlineComment[]> = {}
    for (let i = 0; i < COMMENT_DRAFT_MAX_FILES + 5; i++) {
      drafts[`/f${i}.md`] = [mk(`id${i}`, 'a', `c${i}`)]
    }
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      saveCommentDrafts(drafts)
    } finally {
      Storage.prototype.setItem = orig
    }
    // No eviction applied because persist failed — caller retains every entry
    expect(Object.keys(drafts).length).toBe(COMMENT_DRAFT_MAX_FILES + 5)
    expect(drafts['/f0.md']).toBeDefined()
  })

  it('saveCommentDrafts evicts oldest files when over cap', () => {
    const drafts: Record<string, InlineComment[]> = {}
    for (let i = 0; i < COMMENT_DRAFT_MAX_FILES + 5; i++) {
      drafts[`/f${i}.md`] = [mk(`id${i}`, 'a', `c${i}`)]
    }
    saveCommentDrafts(drafts)
    expect(Object.keys(drafts).length).toBe(COMMENT_DRAFT_MAX_FILES)
    expect(drafts['/f0.md']).toBeUndefined()
    expect(drafts['/f4.md']).toBeUndefined()
    expect(drafts['/f5.md']).toBeDefined()
    expect(Object.keys(loadCommentDrafts()).length).toBe(COMMENT_DRAFT_MAX_FILES)
  })

  it('setCommentsForFile refreshes insertion order (LRU - recently edited survives eviction)', () => {
    const drafts: Record<string, InlineComment[]> = {}
    for (let i = 0; i < COMMENT_DRAFT_MAX_FILES; i++) drafts[`/f${i}.md`] = [mk(`id${i}`, 'a', `c${i}`)]
    // Keep editing /f0.md — it should refresh insertion position
    setCommentsForFile(drafts, '/f0.md', [mk('id0', 'a', 'still typing')])
    // Add a new file, triggering eviction
    setCommentsForFile(drafts, '/new.md', [mk('idN', 'b', 'brand new')])
    saveCommentDrafts(drafts)
    expect(drafts['/f0.md']).toEqual([mk('id0', 'a', 'still typing')])
    expect(drafts['/f1.md']).toBeUndefined()  // evicted (oldest untouched)
    expect(drafts['/new.md']).toEqual([mk('idN', 'b', 'brand new')])
  })

  it('submitting a file does not resurrect its draft on reload', () => {
    saveCommentDrafts({ '/a.md': [mk('1', 'x', 'y')] })
    const drafts = loadCommentDrafts()
    // submit → comments cleared via setCommentsForFile([])
    setCommentsForFile(drafts, '/a.md', [])
    saveCommentDrafts(drafts)
    expect(loadCommentDrafts()).toEqual({})
  })

  it('two files have independent drafts', () => {
    const drafts: Record<string, InlineComment[]> = {}
    setCommentsForFile(drafts, '/a.md', [mk('1', 'a', 'fix A')])
    setCommentsForFile(drafts, '/b.md', [mk('2', 'b', 'fix B')])
    saveCommentDrafts(drafts)
    const loaded = loadCommentDrafts()
    expect(loaded['/a.md']).toEqual([mk('1', 'a', 'fix A')])
    expect(loaded['/b.md']).toEqual([mk('2', 'b', 'fix B')])
  })
})
