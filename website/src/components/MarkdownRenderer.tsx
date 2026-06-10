import { memo, useEffect, useMemo, useRef, useId, useCallback, useState } from 'react'
import { Paperclip, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeRaw from 'rehype-raw'
import rehypeKatex from 'rehype-katex'
import type { PluggableList } from 'unified'
import mermaid from 'mermaid'
import '../utils/hljs'
import { api } from '../api/client'
import { useBlockAssembler, maskInlineCode } from '../hooks/useBlockAssembler'
import { urlTransform, ALLOWED_PROTOCOLS } from '../utils/urlTransform'
import DiffBlock from './DiffBlock'
import MonacoCodeBlock from './MonacoCodeBlock'
import type { ContentBlock } from '../types'

const PATH_RE = /^~?(?:\.{0,2}\/)?[\w.@~\/ -]*\/[\w.@~: -]*[\w.]$/

function isDarkTheme(): boolean {
  return document.documentElement.getAttribute('data-theme') === 'dark'
}

function initMermaid(): void {
  const dark = isDarkTheme()
  mermaid.initialize({
    startOnLoad: false,
    theme: dark ? 'dark' : 'default',
    themeVariables: dark ? {
      primaryColor: '#f59e32',
      primaryTextColor: '#e8e6e3',
      primaryBorderColor: '#3a3a3a',
      lineColor: '#888',
      secondaryColor: '#2a2a2a',
      tertiaryColor: '#1a1a1a',
    } : {
      primaryColor: '#f59e32',
      primaryTextColor: '#1a1a1a',
      primaryBorderColor: '#ccc',
      lineColor: '#666',
      secondaryColor: '#fff3e0',
      tertiaryColor: '#f5f5f5',
    },
    securityLevel: 'strict',
    fontFamily: 'inherit',
    // Throw on parse errors instead of injecting mermaid's error diagram into
    // a temp <div id="dmermaid-*"> on document.body. That temp node is leaked
    // when render() throws (cleanup only runs on success), so failed blocks
    // accumulated orphaned 512px error SVGs in the DOM. With this on, the
    // MermaidBlock .catch() shows a clean inline <pre> and nothing leaks.
    suppressErrorRendering: true,
  })
}

initMermaid()

import { CodeBlock } from './CodeBlock'

/** Forward the `data-sourcepos` attribute from rehypeSourcepos onto the
 *  rendered element. Used in every MD_COMPONENTS override; returns an
 *  empty-valued attribute when sourcePos is disabled (React omits it from
 *  the DOM). */
const sp = (p: any) => ({ 'data-sourcepos': p['data-sourcepos'] })

const MermaidBlock = memo(function MermaidBlock({ code }: { code: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const id = useId().replace(/:/g, '_')
  const renderedRef = useRef('')

  useEffect(() => {
    if (!ref.current || renderedRef.current === code) return
    renderedRef.current = code
    initMermaid()
    mermaid.render(`mermaid-${id}`, code).then(({ svg }) => {
      if (!ref.current) return
      const range = document.createRange()
      range.selectNodeContents(ref.current)
      range.deleteContents()
      ref.current.appendChild(range.createContextualFragment(svg))
    }).catch(() => {
      if (!ref.current) return
      const pre = document.createElement('pre')
      pre.className = 'text-danger text-[13px]'
      pre.textContent = code
      ref.current.textContent = ''
      ref.current.appendChild(pre)
    })
  }, [code, id])

  return <div ref={ref} className="my-3 flex justify-center overflow-x-auto min-h-[60px]" />
})

/** Generate a URL-safe slug from heading children (handles nested elements) */
function textOf(node: any): string {
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (node?.props?.alt) return node.props.alt
  if (node?.props?.children) return textOf(node.props.children)
  return ''
}
function slugify(children: any): string | undefined {
  const raw = textOf(children).toLowerCase().replace(/[^\w\s-]/g, '').replace(/\s+/g, '-').replace(/^-+|-+$/g, '')
  return raw || undefined
}

const MD_COMPONENTS: Record<string, React.ComponentType<any>> = {
  code({ className, children, ...props }: any) {
    const match = /language-(\w+)/.exec(className || '')
    const lang = match?.[1]
    const codeStr = String(children).replace(/\n$/, '')

    if (lang === 'mermaid') return <MermaidBlock code={codeStr} />

    if (!className) {
      if (PATH_RE.test(codeStr)) {
        return <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-accent text-sm font-mono cursor-pointer hover:underline" title="Click to open / Shift+click to reveal in Finder" {...props}>{children}</code>
      }
      return <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-accent text-sm font-mono" {...props}>{children}</code>
    }

    return <CodeBlock code={codeStr} lang={lang} complete={true} />
  },
  pre({ children }: any) { return <>{children}</> },
  table({ children, ...rest }: any) { return <div className="overflow-x-auto my-3"><table {...sp(rest)} className="w-full border-collapse text-sm">{children}</table></div> },
  th({ children, ...rest }: any) { return <th {...sp(rest)} className="text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated">{children}</th> },
  td({ children, ...rest }: any) { return <td {...sp(rest)} className="px-3 py-2 border-b border-border text-sm">{children}</td> },
  a({ href, children, ...rest }: any) { let ext = false; try { ext = !!href && ALLOWED_PROTOCOLS.has(new URL(href, 'http://x').protocol) } catch {} return <a {...sp(rest)} href={href} {...(ext ? {} : { target: '_blank', rel: 'noopener noreferrer' })} className="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">{children}</a> },
  blockquote({ children, ...rest }: any) { return <blockquote {...sp(rest)} className="border-l-[3px] border-accent pl-3 my-2 text-muted italic">{children}</blockquote> },
  hr(rest: any) { return <hr {...sp(rest)} className="border-border my-4" /> },
  h1({ children, ...rest }: any) { const id = slugify(children); return <h1 {...sp(rest)} id={id} className="text-xl font-bold mt-4 mb-2 text-text-strong">{children}</h1> },
  h2({ children, ...rest }: any) { const id = slugify(children); return <h2 {...sp(rest)} id={id} className="text-lg font-bold mt-3 mb-2 text-text-strong">{children}</h2> },
  h3({ children, ...rest }: any) { const id = slugify(children); return <h3 {...sp(rest)} id={id} className="text-base font-semibold mt-3 mb-1.5 text-text-strong">{children}</h3> },
  h4({ children, ...rest }: any) { const id = slugify(children); return <h4 {...sp(rest)} id={id} className="text-sm font-semibold mt-2 mb-1 text-text-strong">{children}</h4> },
  h5({ children, ...rest }: any) { const id = slugify(children); return <h5 {...sp(rest)} id={id} className="text-sm font-medium mt-2 mb-1 text-text-strong">{children}</h5> },
  h6({ children, ...rest }: any) { const id = slugify(children); return <h6 {...sp(rest)} id={id} className="text-[13px] font-medium mt-2 mb-1 text-muted">{children}</h6> },
  ul({ children, ...rest }: any) { return <ul {...sp(rest)} className="list-disc pl-8 my-2 space-y-1 marker:text-muted">{children}</ul> },
  ol({ children, ...rest }: any) { return <ol {...sp(rest)} className="list-decimal pl-8 my-2 space-y-1 marker:text-muted">{children}</ol> },
  li({ children, ...rest }: any) { return <li {...sp(rest)} className="text-sm leading-relaxed">{children}</li> },
  p({ children, ...rest }: any) { return <p {...sp(rest)} className="my-1.5 leading-relaxed">{children}</p> },
  strong({ children, ...rest }: any) { return <strong {...sp(rest)} className="font-semibold text-text-strong">{children}</strong> },
  em({ children, ...rest }: any) { return <em {...sp(rest)} className="italic">{children}</em> },
  img: ImgWithFallback,
}

/** Markdown image with a React-rendered Paperclip fallback when the URL is
 *  broken. Previously the onError handler swapped the <img> for a hand-built
 *  SVG via .replaceWith(), which mutated DOM React still owned and could later
 *  trigger "removeChild on Node" reconciliation crashes. */
function ImgWithFallback({ src, alt, ...props }: any) {
  const [errored, setErrored] = useState(false)
  if (!src) return null
  const isLocal = src.startsWith('/') || src.startsWith('~') || src.startsWith('.')
  const url = isLocal ? `/api/file-raw?path=${encodeURIComponent(src)}` : src
  if (errored) {
    return (
      <span className="text-sm text-muted inline-flex items-center gap-1">
        <Paperclip size={14} aria-hidden="true" />
        {' ' + (alt || src)}
      </span>
    )
  }
  return (
    <span className="inline-block my-2">
      <img
        src={url} alt={alt || ''} loading="lazy"
        className="max-w-[240px] max-h-[160px] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity"
        onClick={(e) => dispatchLightbox(e.currentTarget)}
        data-lightbox-image=""
        title={alt || src}
        onError={() => setErrored(true)}
        {...props}
      />
    </span>
  )
}

// Disable single-$ inline math so currency strings like `$9.99` don't
// accidentally trigger KaTeX math parsing. With singleDollarTextMath=true (the
// default in remark-math v6), chat messages containing multiple dollar amounts
// get parsed as one giant math expression spanning the first $ to the last,
// which KaTeX then fails to render -- producing HTML that React cannot commit
// and crashing the whole dashboard with "DOMException: String contains an
// invalid character" during completeWork. Only $$...$$ display-math blocks
// are treated as math now; single $ is plain text.

/**
 * Rehype plugin: strip dangerous HTML elements from the HAST tree.
 * Prevents XSS via raw HTML in markdown (iframe srcdoc, script, etc.)
 * while preserving safe structural elements authors expect.
 */
const DANGEROUS_TAGS = new Set([
  'script', 'iframe', 'object', 'embed', 'applet', 'form',
  'input', 'textarea', 'button', 'select', 'option',
  'link', 'meta', 'base', 'noscript', 'foreignObject',
])
const DANGEROUS_ATTR_RE = /^on/i  // onclick, onerror, onload, etc.
const DANGEROUS_PROTOCOLS = ['javascript:', 'data:', 'vbscript:']
const cleanUrl = (url: string) => url.replace(/[\x00-\x1f\x7f]/g, '').trim().toLowerCase()

function rehypeSanitize() {
  return (tree: any) => {
    const walk = (node: any, parent: any, index: number) => {
      if (node.type === 'element') {
        // Remove dangerous elements entirely
        if (DANGEROUS_TAGS.has(node.tagName)) {
          parent.children.splice(index, 1)
          return index  // re-check this index (shifted)
        }
        // Strip event handler attributes, dangerous protocol URLs, and srcdoc
        if (node.properties) {
          for (const [key, val] of Object.entries(node.properties)) {
            if (DANGEROUS_ATTR_RE.test(key)) {
              delete node.properties[key]
            } else if (typeof val === 'string') {
              const cleaned = cleanUrl(val)
              if (DANGEROUS_PROTOCOLS.some(p => cleaned.startsWith(p))) {
                // Allow data:image/* on img src (inline base64 images)
                if (node.tagName === 'img' && key === 'src' && cleaned.startsWith('data:image/')) {
                  continue
                }
                delete node.properties[key]
              }
            }
          }
          delete node.properties.srcdoc
        }
      }
      if (node.children) {
        for (let i = 0; i < node.children.length; i++) {
          const result = walk(node.children[i], node, i)
          if (typeof result === 'number') i = result - 1  // re-check after splice
        }
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) {
        const result = walk(tree.children[i], tree, i)
        if (typeof result === 'number') i = result - 1
      }
    }
  }
}

const REMARK_PLUGINS: PluggableList = [remarkGfm, [remarkMath, { singleDollarTextMath: false }]]
const REHYPE_PLUGINS: PluggableList = [[rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeSanitize, rehypeKatex]

/**
 * Rehype plugin that copies each hast element's source `position` onto a
 * `data-sourcepos` HTML attribute in CommonMark format `startLine:startCol-endLine:endCol`.
 * Used by the inline-commenting flow to map selection DOM → source coordinates.
 * Replaces the deprecated `sourcePos` option removed in react-markdown v10.
 */
function rehypeSourcepos() {
  return (tree: any) => {
    const walk = (node: any) => {
      if (node.type === 'element' && node.position?.start) {
        const s = node.position.start, e = node.position.end ?? s
        node.properties = node.properties || {}
        node.properties['data-sourcepos'] = `${s.line}:${s.column}-${e.line}:${e.column}`
      }
      if (node.children) for (const c of node.children) walk(c)
    }
    walk(tree)
  }
}
const REHYPE_PLUGINS_WITH_SOURCEPOS: PluggableList = [[rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeSanitize, rehypeKatex, rehypeSourcepos]
// NOTE: remark plugin config is shared via REMARK_PLUGINS above (singleDollarTextMath:
// false). The sourcepos variant only differs in the rehype chain.

/** Number of trailing characters glowed while a message streams. */
const GLOW_TAIL_CHARS = 30

/**
 * Rehype plugin: wrap the message's trailing text in a
 * `<span class="streaming-glow">` so the newest streamed words shimmer.
 *
 * Operates on the parsed HAST tree (not the markdown source and not the live
 * DOM), so it: (a) never builds a raw HTML string with LLM content — the span
 * is a real element node react-markdown renders as a React `<span>`; (b) never
 * bisects a markdown token — by this stage `**bold**` is already a `<strong>`
 * element, so splitting the last *text* node is always safe; (c) doesn't mutate
 * React-owned DOM, so it can't cause reconciliation crashes.
 *
 * Glows the whole last text node when it's short, else its last GLOW_TAIL_CHARS
 * on a space boundary (never mid-word). Skips text inside code/pre.
 */
function rehypeStreamingGlow(options?: { tailChars?: number }) {
  const tailChars = options?.tailChars ?? GLOW_TAIL_CHARS
  return (tree: any) => {
    // Collect every eligible text node (non-whitespace, not inside code/pre);
    // the streaming tail is the last one. Using an array (rather than a
    // closure-mutated `let`) keeps TypeScript's control-flow narrowing happy.
    const candidates: { parent: any; index: number; value: string }[] = []
    const walk = (node: any, parent: any, index: number, inCode: boolean) => {
      if (node.type === 'text') {
        if (!inCode && node.value && node.value.trim()) {
          candidates.push({ parent, index, value: node.value })
        }
        return
      }
      const code = inCode || node.tagName === 'code' || node.tagName === 'pre'
      if (node.children) {
        for (let i = 0; i < node.children.length; i++) walk(node.children[i], node, i, code)
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) walk(tree.children[i], tree, i, false)
    }
    const target = candidates[candidates.length - 1]
    if (!target) return
    const { parent, index, value } = target
    let cut: number
    if (value.length <= tailChars) {
      cut = 0
    } else {
      const sp = value.lastIndexOf(' ', value.length - tailChars)
      cut = sp > 0 ? sp : value.length - tailChars
    }
    const before = value.slice(0, cut)
    const tail = value.slice(cut)
    if (!tail.trim()) return
    const span = {
      type: 'element',
      tagName: 'span',
      properties: { className: ['streaming-glow'] },
      children: [{ type: 'text', value: tail }],
    }
    parent.children.splice(index, 1, ...(before ? [{ type: 'text', value: before }, span] : [span]))
  }
}

export function fixCodeFences(s: string): string {
  // Escape bare "N." lines so markdown doesn't render them as ordered lists.
  // CommonMark: 0-3 leading spaces = list item, 4+ = indented code block.
  // Tracks backtick and tilde fences with length matching per CommonMark spec.
  let inFence = false
  let fenceMarker = ''
  s = s.replace(/^( {0,3}(```+|~~~+)[\w+#-]*.*|( {0,3}\d+)\.([ \t\r]*))$/gm, (match, _, fence, num, trail) => {
    if (fence) {
      if (!inFence) { inFence = true; fenceMarker = fence }
      else if (
        fence[0] === fenceMarker[0] &&
        fence.length >= fenceMarker.length &&
        /^[ \t\r]*$/.test(match.slice(match.indexOf(fence) + fence.length))
      ) { inFence = false }
      return match
    }
    if (inFence || num === undefined) return match
    return num + '\\.' + trail
  })
  // Ensure blank line before opening fences that are glued to preceding text
  s = s.replace(/([^\n])(\n?)(```\w*\n)/g, (_, pre, nl, fence) =>
    nl ? pre + nl + fence : pre + '\n\n' + fence
  )
  // Split closing fences glued to trailing text: ```358KB → ```\n358KB
  // Preserves valid opening fences (```diff, ```json5, ```c++) via negative lookahead
  s = s.replace(/^(```)(?![a-zA-Z][\w+#-]*\s*$)(.+)$/gm, '$1\n$2')
  // Split opening fences glued to uppercase text (legacy fix)
  s = s.replace(/```([A-Z])/g, '```\n$1')
  return s
}

const MCWIDGET_STRIP_RE = /<mcwidget[\s\S]*?<\/mcwidget>|<mcwidget[\s\S]*$/g

/**
 * Strip stray `<mcwidget>` tags that leak through to a markdown block during
 * streaming transitions, while preserving any tag mentions that appear inside
 * inline-code spans (e.g. when the agent is documenting widget syntax).
 *
 * Builds a per-line inline-code mask, runs the strip regex against the masked
 * text to find ranges, then splices those ranges out of the original content.
 * Mask preserves offsets so match indices are valid against the original.
 */
function stripStrayWidgetTags(content: string): string {
  if (!content.includes('<mcwidget')) return content
  const masked = content.split('\n').map(l => maskInlineCode(l)).join('\n')
  if (!masked.includes('<mcwidget')) return content
  const ranges: Array<[number, number]> = []
  MCWIDGET_STRIP_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = MCWIDGET_STRIP_RE.exec(masked)) !== null) {
    ranges.push([m.index, m.index + m[0].length])
    if (m[0].length === 0) MCWIDGET_STRIP_RE.lastIndex++
  }
  if (ranges.length === 0) return content
  let out = ''
  let pos = 0
  for (const [start, end] of ranges) {
    out += content.slice(pos, start)
    pos = end
  }
  out += content.slice(pos)
  return out
}

const MarkdownBlock = memo(function MarkdownBlock({ content, sourcePos, startLine, glow }: { content: string; sourcePos?: boolean; startLine?: number; glow?: boolean }) {
  // Strip any <mcwidget> tags that leak through during streaming transitions,
  // but preserve tag mentions inside inline-code spans.
  const clean = stripStrayWidgetTags(content)
  if (!clean.trim()) return null
  const baseRehype = sourcePos ? REHYPE_PLUGINS_WITH_SOURCEPOS : REHYPE_PLUGINS
  // Append the glow plugin (last, so it runs on the final tree) only for the
  // streaming tail block — see MarkdownRenderer's `glow` prop.
  const rehypePlugins: PluggableList = glow
    ? [...baseRehype, [rehypeStreamingGlow, { tailChars: GLOW_TAIL_CHARS }]]
    : baseRehype
  const md = (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={rehypePlugins} urlTransform={urlTransform} components={MD_COMPONENTS}>
      {fixCodeFences(clean)}
    </ReactMarkdown>
  )
  return sourcePos ? <div data-block-start={startLine ?? 1}>{md}</div> : md
})

import WidgetFrame from './WidgetFrame'
import WidgetPlaceholder from './WidgetPlaceholder'

/** Try to extract a file path from chat text immediately preceding a diff
 * block. Tools sometimes emit "Created /path/to/file:" or "Modified ..."
 * before a bare diff with no +++/--- headers; this hint lets DiffBlock's
 * Open file button work in those cases (Mesh-1654 round 9).
 */
function extractPathHintFromText(text: string | undefined): string | undefined {
  if (!text) return undefined
  // Last non-empty line before the diff is the most likely carrier of
  // "Created /path:" or "Edited /path:" — scan a few lines back rather
  // than the whole block, to keep this cheap and avoid false positives.
  const lines = text.trimEnd().split('\n').slice(-5)
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim().replace(/[:.,]+$/, '')
    if (!line) continue
    // Patterns we accept:
    //   Created /abs/path
    //   Modified /abs/path
    //   Wrote /abs/path
    //   Updated /abs/path
    //   /abs/path        (bare absolute path)
    //   ~/relative/path  (home-relative)
    //   `/abs/path`      (backtick-wrapped)
    const stripped = line.replace(/^`|`$/g, '')
    const m = /(?:Created|Modified|Wrote|Updated|Edited|Saved|File|Path)?\s*[:\s]?\s*`?(\/[^\s`]+|~\/[^\s`]+)`?/i.exec(stripped)
    if (m && m[1]) return m[1]
  }
  return undefined
}

function BlockRenderer({ block, prevBlock, onFileOpen, sourcePos, messageTs, widgetIndex, glow }: { block: ContentBlock; prevBlock?: ContentBlock; onFileOpen?: (path: string) => void; sourcePos?: boolean; messageTs?: string; widgetIndex?: number; glow?: boolean }) {
  switch (block.type) {
    case 'diff': {
      const pathHint = prevBlock?.type === 'markdown'
        ? extractPathHintFromText(prevBlock.content)
        : undefined
      return <DiffBlock code={block.content} complete={block.complete} onFileOpen={onFileOpen} pathHint={pathHint} />
    }
    case 'mermaid':
      return block.complete ? <MermaidBlock code={block.content} /> : (
        <div className="my-2 p-3 bg-bg-elevated border border-border rounded-md text-muted text-[12px] italic animate-pulse">generating diagram…</div>
      )
    case 'code':
      return <MonacoCodeBlock code={block.content} lang={block.language} complete={block.complete} />
    case 'widget':
      return block.complete
        ? <WidgetFrame html={block.content} title={block.language} slug={block.slug} messageTs={messageTs} widgetIndex={widgetIndex} />
        : <WidgetPlaceholder title={block.language} />
    case 'markdown':
      return <MarkdownBlock content={block.content} sourcePos={sourcePos} startLine={block.startLine} glow={glow} />
  }
}

export default memo(function MarkdownRenderer({ content, streaming = false, onFileOpen, rawMode = false, sourcePos = false, messageTs, glow = false }: { content: string; streaming?: boolean; onFileOpen?: (path: string) => void; rawMode?: boolean; sourcePos?: boolean; messageTs?: string; glow?: boolean }) {
  const blocks = useBlockAssembler(content, streaming)

  const handleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement
    if (el.tagName === 'CODE' && PATH_RE.test(el.textContent || '')) {
      e.preventDefault()
      if (onFileOpen && !e.shiftKey) onFileOpen(el.textContent!.trim())
      else api.revealPath(el.textContent!.trim())
    }
  }, [onFileOpen])

  // Pre-compute the widget index for each widget block (0-based ordinal of
  // widgets within this message). WidgetFrame uses (messageTs, widgetIndex)
  // to derive a stable slug when the agent didn't emit an explicit one, so
  // bookmark state survives refreshes and prevents save→refresh duplicates.
  // Memoized so each BlockRenderer gets a stable widgetIndex reference
  // between renders (consistent with the hook-memoization elsewhere in
  // this CR — defeats memo() defeats if anyone later wraps BlockRenderer).
  //
  // Must run before any conditional return — Rules of Hooks. (rawMode flips
  // via a settings toggle which usually re-mounts this component anyway,
  // but we keep hook order strict for safety.)
  const widgetIndices = useMemo(() => {
    const out: number[] = new Array(blocks.length).fill(-1)
    let n = 0
    for (let i = 0; i < blocks.length; i++) {
      if (blocks[i].type === 'widget') { out[i] = n; n++ }
    }
    return out
  }, [blocks])

  // Index of the last markdown block — the streaming tail that gets the glow
  // (only when `glow` is set). -1 if the message ends in a non-markdown block.
  const lastMarkdownIdx = useMemo(() => {
    for (let i = blocks.length - 1; i >= 0; i--) if (blocks[i].type === 'markdown') return i
    return -1
  }, [blocks])

  if (rawMode) {
    return <pre className="text-[13px] font-mono whitespace-pre-wrap break-words leading-relaxed text-muted">{content}</pre>
  }

  return (
    <div className="group" onClick={handleClick} data-image-scope="">
      {blocks.map((block, i) => (
        // Key on startLine (stable across streaming) instead of block.type, so
        // a code -> diff reclassification mid-stream doesn't unmount the
        // in-progress component. Falls back to index for blocks without a
        // startLine (e.g. extracted widgets). The "idx-" prefix avoids
        // collision with real startLine numbers.
        <BlockRenderer
          key={block.startLine != null ? `line-${block.startLine}` : `idx-${i}`}
          block={block} prevBlock={blocks[i - 1]} onFileOpen={onFileOpen} sourcePos={sourcePos}
          messageTs={messageTs}
          widgetIndex={widgetIndices[i] >= 0 ? widgetIndices[i] : undefined}
          glow={glow && i === lastMarkdownIdx}
        />
      ))}
    </div>
  )
})

type LightboxImage = { src: string; alt: string }
type LightboxDetail = { images: LightboxImage[]; index: number }

/** Build the lightbox payload for an image click. The set is "all images
 *  inside the nearest [data-image-scope] ancestor"; for markdown messages
 *  that's a MarkdownRenderer instance (one per chat message), and for the
 *  chat-input thumbnail strip it's the strip's outer div. */
export function dispatchLightbox(target: HTMLImageElement): void {
  const scope = target.closest('[data-image-scope]') as HTMLElement | null
  let detail: LightboxDetail = { images: [{ src: target.src, alt: target.alt }], index: 0 }
  if (scope) {
    const els = Array.from(scope.querySelectorAll<HTMLImageElement>('img[data-lightbox-image]'))
    if (els.length > 0) {
      detail = {
        images: els.map(el => ({ src: el.src, alt: el.alt })),
        index: Math.max(0, els.indexOf(target)),
      }
    }
  }
  window.dispatchEvent(new CustomEvent('lightbox', { detail }))
}

/** Lightbox overlay -- mount once in the app, listens for 'lightbox' custom
 *  events. Escape closes; ArrowLeft/ArrowRight navigate within the image set
 *  (clamped at the ends). Accepts both the structured { images, index }
 *  payload and the legacy { src, alt } single-image shape. */
export function Lightbox() {
  const [state, setState] = useState<LightboxDetail | null>(null)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as Partial<LightboxDetail> & Partial<LightboxImage> | undefined
      if (!detail) { setState(null); return }
      if (Array.isArray(detail.images) && detail.images.length > 0) {
        const raw = Number.isInteger(detail.index) ? (detail.index as number) : 0
        const idx = Math.max(0, Math.min(raw, detail.images.length - 1))
        setState({ images: detail.images, index: idx })
      } else if (typeof detail.src === 'string') {
        setState({ images: [{ src: detail.src, alt: detail.alt || '' }], index: 0 })
      }
    }
    window.addEventListener('lightbox', handler)
    return () => window.removeEventListener('lightbox', handler)
  }, [])
  const isOpen = state !== null
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setState(null)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setState(s => (s && s.index > 0 ? { ...s, index: s.index - 1 } : s))
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setState(s => (s && s.index < s.images.length - 1 ? { ...s, index: s.index + 1 } : s))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen])
  if (!state) return null
  const img = state.images[state.index]
  return (
    <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center cursor-pointer" onClick={() => setState(null)}>
      <img src={img.src} alt={img.alt} className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg shadow-2xl" onClick={e => e.stopPropagation()} />
      <button aria-label="Close" className="absolute top-4 right-4 text-white/80 hover:text-white text-2xl" onClick={() => setState(null)}><X className="lucide-inline" /></button>
    </div>
  )
}