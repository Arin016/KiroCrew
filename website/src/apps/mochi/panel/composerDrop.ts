/**
 * Composer drop/paste ingest — drag files or photos onto the chat box.
 *
 * NOT from the original Mochi: this is the one feature the desktop-buddy fork
 * added that is worth keeping. It lives here rather than inside the vendored
 * ChatPanel so the divergence from upstream stays a handful of lines at the call
 * site, and so the ingest rules are testable without rendering the panel.
 *
 * HOW MANY IMAGES: as many as you like, and this is why the fork's approach was
 * replaced. The fork read one image into base64 and put it in Mochi's single
 * `screenshot` slot, so a second photo was impossible. KiroCrew's own mechanism
 * has no such limit: core's ACP client scans the outgoing message for absolute
 * image PATHS and inlines each one as its own image block. So every dropped file
 * is uploaded and referenced by path:
 *
 *   - images       -> `![image](/path.png)`     (markdown, so the sent bubble
 *                                                renders a thumbnail too)
 *   - other files  -> `[attached_file N] /path`
 *
 * Both forms are the dashboard composer's conventions, so the agent reads a
 * Mochi attachment exactly as it reads one from the main chat.
 *
 * A dropped file in a page has no filesystem path — the browser hands over only
 * the bytes — so uploading first is not a choice, it is the only route.
 *
 * The single `screenshot` slot is untouched and still carries the screen-capture
 * (snip) path, which produces base64 without ever touching disk.
 */

/** Core's upload route rejects anything outside its own extension allow-list. */
const UPLOAD_URL = '/api/upload/file'

/** Mirrors the extensions core's ACP client will inline (client.py). */
const IMAGE_PATH_RE = /\.(?:png|jpe?g|gif|webp|bmp|svg)$/i

export interface IngestResult {
  /** Uploaded image paths, in drop order. */
  images: string[]
  /** Uploaded non-image paths, in drop order. */
  files: string[]
  /** User-facing reason the drop was partly or wholly refused. */
  error?: string
}

/** The files a drop or paste actually carried. */
export function filesFrom(source: DataTransfer | null | undefined): File[] {
  if (!source) return []
  const out: File[] = []
  // `items` is the only place a PASTED image appears; `files` covers drops.
  for (const item of Array.from(source.items ?? [])) {
    if (item.kind === 'file') {
      const file = item.getAsFile()
      if (file) out.push(file)
    }
  }
  if (out.length === 0) out.push(...Array.from(source.files ?? []))
  return out
}

function isImagePath(path: string): boolean {
  return IMAGE_PATH_RE.test(path)
}

/**
 * Upload everything in one multipart request and split the returned paths.
 *
 * One request rather than one per file: the route accepts repeated `file` parts
 * and cleans up all of them if any is rejected, so a partial batch cannot leave
 * orphans on disk.
 */
export async function ingestFiles(files: File[]): Promise<IngestResult> {
  if (files.length === 0) return { images: [], files: [] }

  const form = new FormData()
  for (const file of files) form.append('file', file, file.name)

  try {
    const res = await fetch(UPLOAD_URL, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    const body = (await res.json().catch(() => ({}))) as { paths?: string[]; error?: string }
    if (!res.ok) {
      // The route refuses extensions outside its allow-list. Report it — the
      // fork discarded such files silently, which reads as the app being broken
      // rather than the file being unsupported.
      return { images: [], files: [], error: body.error ?? `Upload failed (${res.status})` }
    }
    const paths = Array.isArray(body.paths) ? body.paths : []
    return {
      images: paths.filter(isImagePath),
      files: paths.filter((p) => !isImagePath(p)),
    }
  } catch (err) {
    return {
      images: [],
      files: [],
      error: err instanceof Error ? err.message : 'Upload failed',
    }
  }
}

/**
 * Reference lines for a pending attachment list, appended to the message AT SEND
 * TIME rather than pasted into the composer.
 *
 * The composer used to hold these lines so that the text was the single source of
 * truth. That leaked plumbing into the thing the user is typing: dropping three
 * photos filled the box with markdown they then had to type around. Attachments
 * are therefore held as state and only serialised on send -- still one source of
 * truth, just not the visible one.
 *
 * Images use markdown so the SENT bubble shows thumbnails; core's ACP client
 * finds the path inside the markdown just the same and inlines the real image.
 * Non-image files use the `[attached_file N]` convention, numbered from 1 in the
 * order shown in the strip.
 */
export function attachmentLines(items: readonly PendingAttachment[]): string {
  const images = items.filter((a) => a.isImage)
  const files = items.filter((a) => !a.isImage)
  const lines = [
    ...images.map((a) => `![image](${a.path})`),
    ...files.map((a, i) => `[attached_file ${i + 1}] ${a.path}`),
  ]
  return lines.join('\n')
}

/** Message body to send: the typed text plus any attachment references. */
export function composeMessage(input: string, items: readonly PendingAttachment[]): string {
  const refs = attachmentLines(items)
  const typed = input.trim()
  if (refs === '') return typed
  return typed === '' ? refs : `${typed}\n${refs}`
}

/** Attachment descriptors for an ingest result, in strip display order. */
export function attachmentsFrom(result: IngestResult): PendingAttachment[] {
  return [
    ...result.images.map((path) => ({ path, name: basename(path), isImage: true })),
    ...result.files.map((path) => ({ path, name: basename(path), isImage: false })),
  ]
}

function basename(path: string): string {
  return path.split('/').pop() || path
}

// ── Pending-attachment strip ──────────────────────────────────────
//
// Held as state by the composer and serialised only on send (see composeMessage),
// so the box shows chips instead of reference markdown.

export interface PendingAttachment {
  path: string
  /** Basename, for the chip label. */
  name: string
  isImage: boolean
}


