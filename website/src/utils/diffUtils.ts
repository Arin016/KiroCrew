/** Shared diff parsing utilities used by DiffBlock and ToolInputText. */

export interface DiffLine {
  type: 'add' | 'del' | 'context' | 'hunk' | 'meta'
  content: string
  oldNum?: number
  newNum?: number
}

/** Parse unified diff text into structured DiffLine objects.
 *  Handles standard unified diff, kiro-specific +N:content format,
 *  and diff --git / index headers. */
export function parseDiffLines(code: string): DiffLine[] {
  const raw = code.split('\n')
  const result: DiffLine[] = []
  let oldN = 0, newN = 0
  let seenHunk = false

  for (const line of raw) {
    if (line.startsWith('@@')) {
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line)
      if (m) { oldN = parseInt(m[1]); newN = parseInt(m[2]) }
      seenHunk = true
      result.push({ type: 'hunk', content: line })
    } else if (line.startsWith('--- ') || line.startsWith('+++ ')) {
      result.push({ type: 'meta', content: line })
    } else if (!seenHunk && (line.startsWith('diff ') || line.startsWith('index '))) {
      result.push({ type: 'meta', content: line })
    } else if (line.startsWith('+')) {
      const kiroAdd = /^\+(\d+):(.*)/.exec(line)
      if (kiroAdd) {
        result.push({ type: 'add', content: kiroAdd[2], newNum: parseInt(kiroAdd[1]) })
      } else {
        result.push({ type: 'add', content: line.slice(1), newNum: newN })
        newN++
      }
    } else if (line.startsWith('-')) {
      const kiroDel = /^-(\d+):(.*)/.exec(line)
      if (kiroDel) {
        result.push({ type: 'del', content: kiroDel[2], oldNum: parseInt(kiroDel[1]) })
      } else {
        result.push({ type: 'del', content: line.slice(1), oldNum: oldN })
        oldN++
      }
    } else {
      const text = line.startsWith(' ') ? line.slice(1) : line
      result.push({ type: 'context', content: text, oldNum: oldN, newNum: newN })
      oldN++; newN++
    }
  }
  return result
}

/** Detect whether text contains unified diff content.
 *  Requires @@ hunk headers or paired ---/+++ file headers to avoid
 *  false positives on markdown lists, negative numbers, and CLI flags.
 *  Note: YAML front matter (---) + markdown +++ headings could false-positive,
 *  but this is unlikely in tool input context where content is code/JSON. */
export function isDiffText(text: string): boolean {
  const lines = text.split('\n')
  return lines.some(l => /^@@\s/.test(l)) ||
    (lines.some(l => /^--- /.test(l)) && lines.some(l => /^\+\+\+ /.test(l)))
}

/** Background color classes per diff line type. */
export const DIFF_BG: Record<DiffLine['type'], string> = {
  add: 'bg-diff-add', del: 'bg-diff-del', hunk: 'bg-diff-hunk', meta: '', context: ''
}

/** Foreground color classes per diff line type. */
export const DIFF_FG: Record<DiffLine['type'], string> = {
  add: 'text-diff-add-text', del: 'text-diff-del-text', hunk: 'text-diff-hunk-text',
  meta: 'text-diff-meta-text font-semibold', context: 'text-muted'
}
