import type { ChatMessage } from '../types'

export type LinkType = 'cr' | 'other'

export interface ExtractedLink {
  url: string
  type: LinkType
  label: string
  msgIdx: number
  fromMarkdown?: boolean
}

/** Classify a URL into a known type (generic git/PR vs everything else) */
function classifyUrl(url: string): LinkType {
  // Generic pull request / code review detection (GitHub, GitLab, Bitbucket, etc.)
  if (/\/(?:pull|pull-requests|merge_requests)\/\d+/i.test(url) || /\/reviews\/[\w-]+/i.test(url)) return 'cr'
  return 'other'
}

/** Generate a fallback label from URL structure */
function fallbackLabel(url: string, type: LinkType): string {
  switch (type) {
    case 'cr': {
      const m = url.match(/\/(?:pull|pull-requests|merge_requests)\/(\d+)/i) || url.match(/\/reviews\/([\w-]+)/i)
      return m ? `#${m[1]}` : 'PR'
    }
    default: { try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url.slice(0, 30) } }
  }
}

/** Normalize URL for dedup (strip trailing slash) */
function normalizeUrl(url: string): string {
  return url.replace(/\/+$/, '')
}

/** Check if a URL is already seen (normalized exact match) */
function isDuplicate(url: string, seen: Set<string>): boolean {
  return seen.has(normalizeUrl(url))
}

// Match markdown links: [text](url)
const MD_LINK_RE = /\[([^\]]{1,100})\]\((https?:\/\/[^)]+)\)/g
// Match bare URLs not inside markdown link syntax (strip trailing punctuation)
const BARE_URL_RE = /https?:\/\/[\w][\w.-]*\.[a-z]{2,}(?:[\w/._?&#=-]*[\w/&#=-])?/g

/** Extract a label from text surrounding a bare URL */


export function extractChatLinks(messages: ChatMessage[]): ExtractedLink[] {
  const seen = new Set<string>()
  const links: ExtractedLink[] = []

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]
    if (msg.role !== 'user' && msg.role !== 'assistant') continue
    const text = msg.content || ''

    // Pass 1: Extract markdown links [label](url) — best labels
    MD_LINK_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MD_LINK_RE.exec(text)) !== null) {
      const [, rawLabel, url] = m
      if (isDuplicate(url, seen)) continue
      seen.add(normalizeUrl(url))
      const type = classifyUrl(url)
      const label = rawLabel.replace(/\*+/g, '').replace(/`/g, '').trim()
      links.push({ url, type, label: label.length >= 3 ? label.slice(0, 60) : fallbackLabel(url, type), msgIdx: i, fromMarkdown: true })
    }

    // Pass 2: Extract bare URLs with nearby context
    BARE_URL_RE.lastIndex = 0
    while ((m = BARE_URL_RE.exec(text)) !== null) {
      const url = m[0]
      if (isDuplicate(url, seen)) continue
      seen.add(normalizeUrl(url))
      const type = classifyUrl(url)
      links.push({ url, type, label: fallbackLabel(url, type), msgIdx: i })
    }
  }

  return links
}
