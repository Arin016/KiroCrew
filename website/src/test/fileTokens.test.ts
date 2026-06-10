import { describe, it, expect } from 'vitest'
import { prepareSendPayload } from '../utils/fileTokens'

describe('prepareSendPayload', () => {
  it('includes non-image files without @-mention', () => {
    const result = prepareSendPayload('hello', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1]')
    expect(result.txt).toContain('/tmp/data.csv')
    expect(result.filePaths).toEqual(['/tmp/data.csv'])
  })

  it('includes image files as markdown', () => {
    const result = prepareSendPayload('check this', ['/tmp/photo.png'])
    expect(result.txt).toContain('![image](/tmp/photo.png)')
    expect(result.imgPaths).toEqual(['/tmp/photo.png'])
  })

  it('separates the image from the message text with a blank line in displayTxt', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // Blank line (Markdown paragraph break) so the image renders in its own
    // block and the text drops to the next line, not inline after the image.
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)\n\nmy caption')
    expect(result.displayTxt).toContain('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only displayTxt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)')
  })

  it('separates the image from the message text with a blank line in txt (LLM-facing)', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // The blank-line separation is persisted in the LLM-facing `txt`, not just
    // the optimistic displayTxt, so the image renders in its own block on every
    // surface that replays stored content (dashboard re-render after a turn,
    // gateway restart, Slack replay, exports) — the original bug was the
    // single-'\n' persisted content collapsing image + caption onto one line.
    expect(result.txt).toBe('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only txt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.txt).toBe('![image](/tmp/photo.png)')
  })

  it('separates image from caption with a blank line but keeps single newline to appended file tokens', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png', '/tmp/data.csv'])
    // image block -> blank line -> caption -> single newline -> [attached_file].
    expect(result.txt).toBe(
      '![image](/tmp/photo.png)\n\nmy caption\n[attached_file 1] /tmp/data.csv',
    )
  })

  it('includes mixed image and non-image files', () => {
    const result = prepareSendPayload('here', ['/tmp/a.png', '/tmp/b.zip'])
    expect(result.imgPaths).toEqual(['/tmp/a.png'])
    expect(result.filePaths).toEqual(['/tmp/b.zip'])
    expect(result.txt).toContain('![image]')
    expect(result.txt).toContain('[attached_file')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).toContain('![image]')
  })

  it('includes @-referenced files inline and unreferenced as appended tokens', () => {
    const result = prepareSendPayload(
      'see @data.csv for details',
      ['/tmp/data.csv', '/tmp/extra.log'],
    )
    expect(result.filePaths).toContain('/tmp/data.csv')
    expect(result.filePaths).toContain('/tmp/extra.log')
    expect(result.txt).toContain('/tmp/extra.log')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).not.toContain('/tmp/extra.log')
  })

  it('returns empty filePaths when no files pending', () => {
    const result = prepareSendPayload('just text', [])
    expect(result.filePaths).toEqual([])
    expect(result.imgPaths).toEqual([])
  })

  it('replaces @-referenced token inline in txt', () => {
    const result = prepareSendPayload('see @data.csv', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).not.toContain('@data.csv')
  })

  it('deduplicates when same file appears twice', () => {
    const result = prepareSendPayload('hello', ['/tmp/a.csv', '/tmp/a.csv'])
    expect(result.filePaths).toEqual(['/tmp/a.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/a.csv')
  })

  it('assigns unique token numbers when @-ref is not the first file', () => {
    const result = prepareSendPayload(
      'see @data.csv',
      ['/tmp/extra.log', '/tmp/data.csv'],
    )
    const indices = [...result.txt.matchAll(/\[attached_file (\d+)\]/g)].map(m => m[1])
    expect(indices.length).toBe(2)
    expect(new Set(indices).size).toBe(indices.length)
  })
})
