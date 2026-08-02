/**
 * Composer drop/paste ingest.
 *
 * This is the one desktop-buddy-fork feature kept deliberately, so it has no
 * upstream to diff against — its rules only exist here. Each case below pins a
 * behaviour whose failure is silent: a file that vanishes with no message, a
 * second image quietly discarded, or an attachment written in a format the agent
 * does not recognise.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  filesFrom,
  ingestFiles,
  attachmentsFrom,
  composeMessage,
} from '../panel/composerDrop'

function imageFile(name = 'shot.png'): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: 'image/png' })
}

function textFile(name = 'notes.txt'): File {
  return new File(['hello'], name, { type: 'text/plain' })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('filesFrom', () => {
  it('reads pasted files out of items, not files', () => {
    // A pasted image appears ONLY under `items` — reading `files` first is why
    // paste support is usually written and then found not to work.
    const file = imageFile()
    const dt = {
      items: [{ kind: 'file', getAsFile: () => file }],
      files: [],
    } as unknown as DataTransfer
    expect(filesFrom(dt)).toEqual([file])
  })

  it('falls back to files for a drop', () => {
    const file = textFile()
    const dt = { items: [], files: [file] } as unknown as DataTransfer
    expect(filesFrom(dt)).toEqual([file])
  })

  it('ignores non-file items such as dragged text', () => {
    const dt = {
      items: [{ kind: 'string', getAsFile: () => null }],
      files: [],
    } as unknown as DataTransfer
    expect(filesFrom(dt)).toEqual([])
  })

  it('tolerates a missing transfer', () => {
    expect(filesFrom(null)).toEqual([])
  })
})

describe('ingestFiles', () => {
  const okUpload = (paths: string[]) =>
    vi.fn(async () => ({ ok: true, json: async () => ({ paths }) }))

  it('supports MANY images — the limit the fork had is gone', async () => {
    // The fork read one image into the single `screenshot` slot, so a second
    // photo was impossible. Core's ACP client inlines every image PATH it finds
    // in the message, so the count is unbounded.
    vi.stubGlobal('fetch', okUpload(['/u/a.png', '/u/b.jpg', '/u/c.webp']))
    const result = await ingestFiles([imageFile('a.png'), imageFile('b.jpg'), imageFile('c.webp')])
    expect(result.images).toEqual(['/u/a.png', '/u/b.jpg', '/u/c.webp'])
    expect(result.files).toEqual([])
  })

  it('uploads everything in ONE request', async () => {
    const fetchMock = okUpload(['/u/a.png', '/u/n.txt'])
    vi.stubGlobal('fetch', fetchMock)
    await ingestFiles([imageFile(), textFile()])
    // One request: the route cleans up the whole batch if any part is rejected,
    // so per-file requests could leave orphans on disk.
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('splits images from other files by extension', async () => {
    vi.stubGlobal('fetch', okUpload(['/u/shot.PNG', '/u/notes.txt', '/u/d.pdf']))
    const result = await ingestFiles([imageFile(), textFile()])
    // Case-insensitive: an uppercase extension is still an image.
    expect(result.images).toEqual(['/u/shot.PNG'])
    expect(result.files).toEqual(['/u/notes.txt', '/u/d.pdf'])
  })

  it('surfaces a refused upload', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 415, json: async () => ({ error: 'nope' }) })),
    )
    const result = await ingestFiles([textFile('run.sh')])
    // Core's route has an extension allow-list; a rejection must reach the user.
    expect(result.images).toEqual([])
    expect(result.files).toEqual([])
    expect(result.error).toBe('nope')
  })

  it('surfaces a network failure rather than resolving empty', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline') }))
    expect((await ingestFiles([textFile()])).error).toBe('offline')
  })

  it('is a no-op for an empty drop', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await ingestFiles([])).toEqual({ images: [], files: [] })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('composeMessage / attachmentsFrom', () => {
  const items = [
    { path: '/u/a.png', name: 'a.png', isImage: true },
    { path: '/u/b.png', name: 'b.png', isImage: true },
    { path: '/u/notes.pdf', name: 'notes.pdf', isImage: false },
  ]

  it('keeps reference markdown OUT of the typed text and appends it on send', () => {
    // The whole point of the state-backed strip: the composer shows chips, the
    // sent body carries the references.
    const body = composeMessage('look at these', items)
    expect(body.startsWith('look at these\n')).toBe(true)
    expect(body).toContain('![image](/u/a.png)')
    expect(body).toContain('![image](/u/b.png)')
    expect(body).toContain('[attached_file 1] /u/notes.pdf')
  })

  it('sends references alone when the user typed nothing', () => {
    expect(composeMessage('', [items[0]])).toBe('![image](/u/a.png)')
  })

  it('returns just the typed text when nothing is attached', () => {
    expect(composeMessage('hello', [])).toBe('hello')
  })

  it('numbers non-image files from 1 in strip order', () => {
    const files = [
      { path: '/u/one.pdf', name: 'one.pdf', isImage: false },
      { path: '/u/two.pdf', name: 'two.pdf', isImage: false },
    ]
    const body = composeMessage('', files)
    expect(body).toBe('[attached_file 1] /u/one.pdf\n[attached_file 2] /u/two.pdf')
  })

  it('labels chips by basename and marks images', () => {
    const out = attachmentsFrom({ images: ['/deep/dir/a.PNG'], files: ['/deep/dir/b.txt'] })
    expect(out).toEqual([
      { path: '/deep/dir/a.PNG', name: 'a.PNG', isImage: true },
      { path: '/deep/dir/b.txt', name: 'b.txt', isImage: false },
    ])
  })
})
