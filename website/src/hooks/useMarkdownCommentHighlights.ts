/** Find the character offset of the best-matching occurrence of `anchor` in
 *  `text`. When `startOffset` is present, picks the occurrence nearest that
 *  offset (handles repeated identical text). Without an offset, falls back to
 *  the first match. Returns -1 when not found. Exported for unit tests +
 *  consumed by MarkdownPanel's CSS Highlight apply() loop. */
export function findBestOccurrence(text: string, anchor: string, startOffset?: number): number {
  let bestIdx = -1
  if (startOffset != null) {
    let from = 0
    let bestDist = Infinity
    for (;;) {
      const i = text.indexOf(anchor, from)
      if (i < 0) break
      const dist = Math.abs(i - startOffset)
      if (dist < bestDist) { bestDist = dist; bestIdx = i }
      from = i + 1
    }
  } else {
    bestIdx = text.indexOf(anchor)
  }
  return bestIdx
}
