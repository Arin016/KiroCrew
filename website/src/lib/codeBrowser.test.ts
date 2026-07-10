import { describe, expect, it } from 'vitest'

import { codeBrowserBranchUrl, codeBrowserCommitUrl } from './codeBrowser'

describe('codeBrowser URL helpers', () => {
  it('builds a branch tree URL', () => {
    expect(codeBrowserBranchUrl('main')).toBe(
      'https://github.com/kirodotdev-labs/kiroclaw/tree/main',
    )
  })

  it('keeps slashes literal in a branch ref (feat/foo)', () => {
    expect(codeBrowserBranchUrl('feat/foo')).toBe(
      'https://github.com/kirodotdev-labs/kiroclaw/tree/feat/foo',
    )
  })

  it('escapes unsafe chars (space) while preserving the path', () => {
    expect(codeBrowserBranchUrl('wip branch')).toBe(
      'https://github.com/kirodotdev-labs/kiroclaw/tree/wip%20branch',
    )
  })

  it('builds a commit URL from a short SHA', () => {
    expect(codeBrowserCommitUrl('9866ae7a')).toBe(
      'https://github.com/kirodotdev-labs/kiroclaw/commit/9866ae7a',
    )
  })
})
