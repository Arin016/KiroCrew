/** Shared file-token utilities used by send() and renderUserContent(). */

export const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i

/** Boundary-aware regex for @token matching. Prevents `@foo.ts` from matching inside `@foo.tsx`. */
function tokenRegex(token: string, flags = ''): RegExp {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`@${escaped}(?=\\s|$)`, flags)
}

/** Parse file paths from message meta or [attached_file N] patterns in content. */
export function parseFiles(content: string, meta?: Record<string, unknown>): string[] {
  const metaFiles = (meta?.files || []) as string[]
  return metaFiles.length
    ? metaFiles
    : (content.match(/\[attached_file \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_file \d+\] /, ''))
}

/** Walk path segments to find the shortest @suffix present in text. */
export function buildRelMap(paths: string[], text: string): Map<string, string> {
  const map = new Map<string, string>()
  for (const p of paths) {
    const segs = p.split('/')
    for (let i = 1; i < segs.length; i++) {
      const suffix = segs.slice(i).join('/')
      if (tokenRegex(suffix).test(text) && !map.has(suffix)) { map.set(suffix, p); break }
    }
  }
  return map
}

/** Replace @rel tokens in text using a replacer function. */
export function replaceTokens(
  text: string, paths: string[], relMap: Map<string, string>,
  replacer: (fullPath: string, idx: number) => string,
): string {
  let result = text
  paths.forEach((p, i) => {
    const rel = [...relMap.entries()].find(([, v]) => v === p)?.[0]
    if (!rel) return
    result = result.replace(tokenRegex(rel, 'g'), () => replacer(p, i))
  })
  return result
}

/** Build send payload from raw input text and pending files. */
export interface SendPayload {
  txt: string        // LLM-facing content
  displayTxt: string // UI-facing content
  filePaths: string[]
  imgPaths: string[]
}

export function prepareSendPayload(raw: string, pendingFiles: string[]): SendPayload {
  // All pending files (uploaded via button/drag-drop) are always included.
  // The @-token in text is used for display replacement, not as a gate.
  const files = [...new Set(pendingFiles)]
  const imgPaths = files.filter(p => IMG_EXT.test(p))
  const filePaths = files.filter(p => !IMG_EXT.test(p))
  const imgMd = imgPaths.map(p => `![image](${p})`).join('\n')
  const relMap = buildRelMap(files, raw)

  // Assign sequential indices to all non-image files, ordered by upload order.
  // Referenced files get lower indices, unreferenced get higher — but indices
  // may not be monotonically increasing in the rendered text if @-mentions
  // appear in a different order than the upload order.
  const referencedPaths = new Set([...relMap.values()])
  const idxMap = new Map<string, number>()
  let n = 1
  // Assign indices in upload order: referenced files first, then unreferenced
  for (const p of filePaths) { if (referencedPaths.has(p) && !idxMap.has(p)) idxMap.set(p, n++) }
  for (const p of filePaths) { if (!referencedPaths.has(p) && !idxMap.has(p)) idxMap.set(p, n++) }

  const llmRaw = replaceTokens(
    replaceTokens(raw, imgPaths, relMap, () => ''),
    filePaths, relMap, (p) => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`,
  )
  const unreferenced = filePaths.filter(p => !referencedPaths.has(p))
  const unreferencedTokens = unreferenced.map(p => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`).join('\n')
  const displayRaw = replaceTokens(raw, imgPaths, relMap, () => '')

  return {
    txt: [imgMd, llmRaw, unreferencedTokens].filter(Boolean).join('\n'),
    displayTxt: [imgMd, displayRaw].filter(Boolean).join('\n'),
    filePaths,
    imgPaths,
  }
}
