import type { ReactNode } from 'react'
import { parseDiffLines, isDiffText, DIFF_BG, DIFF_FG } from '../utils/diffUtils'

/** Inline tool input renderer with diff coloring and JSON syntax highlighting.
 *  Used in approval popups, activity viewer, and collapsed tool groups. */
export function ToolInputText({ text }: { text: string }): ReactNode {
  const trimmed = text.trimStart()
  // JSON-like highlighting — works on truncated JSON too
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    if (text.length > 50_000) return <span>{text}</span>
    const parts: ReactNode[] = []
    const re = /("(?:[^"\\]*(?:\\.[^"\\]*)*)")\s*:|("(?:[^"\\]*(?:\\.[^"\\]*)*)")|(true|false|null)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
    let last = 0
    let m: RegExpExecArray | null
    let idx = 0
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parts.push(<span key={idx++}>{text.slice(last, m.index)}</span>)
      if (m[1]) parts.push(<span key={idx++} style={{color:'var(--json-key)'}}>{m[1]}</span>, <span key={idx++}>:</span>)
      else if (m[2]) parts.push(<span key={idx++} style={{color:'var(--json-str)'}}>{m[2]}</span>)
      else if (m[3]) parts.push(<span key={idx++} style={{color:'var(--json-bool)'}}>{m[3]}</span>)
      else if (m[4]) parts.push(<span key={idx++} style={{color:'var(--json-num)'}}>{m[4]}</span>)
      last = m.index + m[0].length
    }
    if (last > 0) {
      if (last < text.length) parts.push(<span key={idx}>{text.slice(last)}</span>)
      return <>{parts}</>
    }
  }
  // Diff highlighting — only if text contains diff markers
  if (isDiffText(text)) {
    const lines = parseDiffLines(text)
    return (
      <>
        {lines.map((line, i) => {
          if (line.type === 'meta') return null
          return <div key={i} className={`${DIFF_BG[line.type]} ${DIFF_FG[line.type]}`}>{line.type === 'hunk' ? line.content : (line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ') + line.content}</div>
        })}
      </>
    )
  }
  // Default: plain text — inherit parent's color so the rendering stays
  // visually consistent with the JSON-highlighted path (whose non-token
  // spans also inherit). Wrapping in `text-muted` here was the cause of
  // input/output panels looking dim for tools that didn't trip the JSON
  // regex (edge whitespace, partial streams, non-JSON shell output).
  return <span>{text}</span>
}
