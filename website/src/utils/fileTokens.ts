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
  // Keep metadata in the same order as token numbers so backend consumers can
  // resolve [attached_file N] directly without scanning every path.
  const indexedFilePaths = [
    ...filePaths.filter(p => referencedPaths.has(p)),
    ...filePaths.filter(p => !referencedPaths.has(p)),
  ]
  const idxMap = new Map(indexedFilePaths.map((p, i) => [p, i + 1]))

  const llmRaw = replaceTokens(
    replaceTokens(raw, imgPaths, relMap, () => ''),
    filePaths, relMap, (p) => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`,
  )
  const unreferenced = filePaths.filter(p => !referencedPaths.has(p))
  const unreferencedTokens = unreferenced.map(p => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`).join('\n')
  const displayRaw = replaceTokens(raw, imgPaths, relMap, () => '')

  // Separate the pasted-image markdown from the typed text with a blank line
  // (a Markdown paragraph break) so the image renders in its own block and the
  // text drops to the next line, instead of flowing inline after the image (a
  // single '\n' is only a soft break). Applied to BOTH the LLM-facing `txt`
  // and the UI-facing `displayTxt`, so the *persisted* message keeps the break
  // on every surface that replays stored content — dashboard re-render after a
  // turn, gateway restart, Slack replay, exports — not just the in-memory
  // optimistic bubble. The extra blank line is safe for image attachment: the
  // ACP path (kiro-cli) extracts images in AcpClient._send_prompt by matching
  // the absolute file path and inlines them as a base64 `image` content block.
  // It is newline-agnostic and pulls the image into its own content block, so
  // the surrounding whitespace never changes what the model receives. The
  // caption keeps a single '\n' to its appended [attached_file N] tokens.
  const textBody = [llmRaw, unreferencedTokens].filter(Boolean).join('\n')
  return {
    txt: [imgMd, textBody].filter(Boolean).join('\n\n'),
    displayTxt: [imgMd, displayRaw].filter(Boolean).join('\n\n'),
    filePaths: indexedFilePaths,
    imgPaths,
  }
}
