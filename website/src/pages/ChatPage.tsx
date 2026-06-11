import { useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useIsMobile } from '../hooks/useIsMobile'
import { useSwipeEdge } from '../hooks/useSwipeEdge'
import { useAppSelector, useAppDispatch, store } from '../store'
import {
  switchSlot, createSlot, deleteSlot, fetchHistory,
  appendMessage, resumeFromHistory, forkSlot,
  setSlotRunning, setSlotStopping, setPendingInput, resolveByApprovalId, clearPendingPermissions, cancelQueuedMessage,
  setVoiceAudio,
  toggleActivity, openActivityToTab,
  setActiveSlot, truncateAfterIndex, replaceMessages,
  requestStop, clearQuestionCard,
} from '../store/chatSlice'
import { removeNotificationByTs } from '../store/notificationsSlice'
import { interceptSlashCommand } from './chat/ChatInput'
import { updateSlot, changeApprovalMode, sseSlotTitle, sseSlotColor } from '../store/dashboardSlice'
import { filterSlotsBySurface, filterUnreadKeysBySurface } from '../surfaces/registry'
import { api } from '../api/client'
import { useProvider } from '../providers'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { fileReadUrl } from '../utils/fileReadUrl'
import { handleStopPress } from '../utils/stopDebounce'
import { EmptyState, Btn, Input } from '../components/ui'
import MarkdownPanel from '../components/MarkdownPanel'
import DiffPanel from '../components/DiffPanel'
import { type FileChangeEntry, countLines } from '../components/FileChangeChips'
import PastedChip from '../components/PastedChip'
import SnipOverlay from '../components/SnipOverlay'
import { captureScreen, screenSnipSupported } from '../hooks/useScreenSnip'
import { useTouchedFiles } from '../hooks/useTouchedFiles'
import { useTheme } from '../hooks/useTheme'
import CollapsibleToolGroup from './chat/CollapsibleToolGroup'
import type { DisplayItem, TurnItem } from './chat/types'
import { useScrollManager } from './chat/useScrollManager'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { parseFiles, buildRelMap, prepareSendPayload } from '../utils/fileTokens'
import { type PasteBlock, expandAll as expandPasteTokens, findTokenRanges, pruneBlocks as pruneBlocksUtil, saveStoredPaste } from '../utils/pasteTokens'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
const GROUPABLE = new Set(['thinking', 'permission'])
/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100
export const PREFILL_STORAGE_KEY = 'kiroclaw_prefill'
import WelcomeView from '../components/WelcomeView'
import { usePanelState, useDiffPanel } from '../hooks/usePanelState'
import { useBranding } from '../hooks/useBranding'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAgents } from '../hooks/useAgents'
import AgentDropdownList from '../components/AgentDropdownList'
import ProjectPicker from '../components/ProjectPicker'
import ModelDropdownList from '../components/ModelDropdownList'

import ChatInput from '../components/ChatInput'
import { modelSupportsEffort } from '../lib/effort'
import QuestionCard from '../components/QuestionCard'
import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import FlyingQuote from '../components/FlyingQuote'
import { useMessageSearch } from '../hooks/useMessageSearch'
import SearchHighlightContext, { MessageSearchScope } from '../hooks/SearchHighlightContext'
import SearchBar from '../components/SearchBar'
import QueueStack from '../components/QueueStack'
import { useVoiceInput, voiceInputSupported } from '../hooks/useVoiceInput'
import { ChatFooter, AssistantMessage, UserMessage, parseOptions } from './chat'
import MarkdownRenderer from '../components/MarkdownRenderer'
import TypewriterText from '../components/TypewriterText'
import ActivityViewer from './chat/ActivityViewer'
import ChatNavPanel from './chat/ChatNavPanel'
import { useChatNavigation } from '../hooks/useChatNavigation'
import SubagentProgressBar from './chat/SubagentProgressBar'
import ChatSidebar, { SIDEBAR_MIN, SIDEBAR_MAX } from './ChatSidebar'
import { copySessionLink, toSlug } from '../utils/shareUrl'
import { DRAFT_SAVE_DEBOUNCE_MS, loadDrafts, saveDrafts as persistDrafts, setDraft } from '../utils/chatDrafts'
import { loadFileDrafts, saveFileDrafts as persistFileDrafts, setFileDraft } from '../utils/chatFileDrafts'
import { loadPasteDrafts, savePasteDrafts as persistPasteDrafts, setPasteDraft } from '../utils/chatPasteDrafts'
import { findPrevUserMsgDisplayIdx } from '../utils/findPrevUserMsgDisplayIdx'
import OverlayDrawer from '../components/OverlayDrawer'
import { loadChatConfig, CONTENT_WIDTH, type ChatConfig } from './chat/ChatSettings'
import { useKnowledgeFetch, extractKnowledgeQuery, expandKnowledgeBlock } from './chat/useKnowledgeFetch'
import { KnowledgePicker } from './chat/KnowledgePicker'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { ShieldCheck, BookOpen, Handshake, Rocket, EyeOff, Circle, Wrench, Loader, AlertTriangle, PanelRight, Pen, MessageSquareShare, ChevronDown, ChevronRight, Plug, ArrowDown, ArrowUp, MessageSquare, MessageSquareDot, Sparkles, VenetianMask, Clock, Locate, ListTree, Link2, Hash, Undo2, Check } from 'lucide-react'

import InfoTip from '../components/InfoTip'
import { FileCard } from '../components/FileCard'

const APPROVAL_SEGMENTS = [
  { key: 'normal' as const, label: 'Normal', icon: <ShieldCheck size={13} />, tooltip: 'KiroClaw asks you before doing anything', desc: 'KiroClaw checks with you before doing anything' },
  { key: 'trust_reads' as const, label: 'Reads', icon: <BookOpen size={13} />, tooltip: 'KiroClaw looks things up on its own, but asks before making changes', desc: 'KiroClaw looks things up on its own, but asks before making any changes' },
  { key: 'trust' as const, label: 'Trust', icon: <Handshake size={13} />, tooltip: 'In this chat, KiroClaw works without asking you first', desc: 'In this chat, KiroClaw works without asking you first' },
  { key: 'yolo' as const, label: 'YOLO', icon: <Rocket size={13} />, tooltip: 'In every chat, KiroClaw works without asking you first', desc: 'In every chat, KiroClaw works without asking you first' },
]
import { AnimatePresence, motion } from 'framer-motion'
import DetailPanel from '../components/DetailPanel'

import type { ChatMessage } from '../types'

import ToolCallLine from './chat/ToolCallLine'
import { renderMcpOAuthMessage } from './chat/McpOAuthBanner'
import TurnBlock from './chat/TurnBlock'
import Clickable from '../components/Clickable'
import StopEventCard from './chat/StopEventCard'
import { tryQuickSend } from '../lib/quickSend'
import { rewindWithRollback } from '../lib/rewindCall'


const IDLE_DEFAULT = { kind: 'idle', text: 'Ready', ts: 0 } as const

/** Live session status badge — shows current phase with elapsed timer. */
function ChatHeaderMenu({ activeSlot, currentSlot, slackChannels, onSlackLink, slotKey, colorIndex, agent, onReveal, mode }: {
  activeSlot: string | null; currentSlot: any; slackChannels: { id: string; name: string }[] | null | undefined
  onSlackLink: (channelId?: string) => void; slotKey?: string; colorIndex?: number | null; agent?: string; onReveal?: () => void; mode?: string
}) {
  const [open, setOpen] = useState(false)
  const [mcpHover, setMcpHover] = useState(false)
  const { data: servers = [] } = useQuery<{ name: string; enabled?: boolean }[]>({
    queryKey: ['mcp-servers', agent],
    queryFn: () => api.mcpActive(agent || undefined),
    enabled: mcpHover,
  })
  const mcpTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const dispatch = useAppDispatch()
  const { paletteColors } = useSessionPalette()
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])
  useEffect(() => { if (!open) { setMcpHover(false) } }, [open])

  const pickColor = (idx: number | null) => {
    if (!slotKey) return
    dispatch(sseSlotColor({ key: slotKey, color_index: idx }))
    api.setSlotColor(slotKey, idx).catch(() => {})
    setOpen(false)
  }

  return (
    <div ref={ref} className="relative flex items-center">
      <button className="px-0.5 py-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none transition-all" onClick={() => setOpen(!open)}>
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[180px]">
          <div className="relative" onMouseEnter={() => { if (mcpTimer.current) clearTimeout(mcpTimer.current); setMcpHover(true) }} onMouseLeave={() => { mcpTimer.current = setTimeout(() => setMcpHover(false), 200) }}>
            <div className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text hover:bg-bg-hover cursor-default">
              <Plug size={13} className="shrink-0 text-muted" />
              <span className="flex-1">MCP servers</span>
              <ChevronRight size={12} className="text-muted" />
            </div>
            {mcpHover && (
              <div className="absolute left-full top-0 ml-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-2 px-3 min-w-[220px] max-w-[280px] max-h-[300px] overflow-y-auto" onMouseEnter={() => { if (mcpTimer.current) clearTimeout(mcpTimer.current) }} onMouseLeave={() => { mcpTimer.current = setTimeout(() => setMcpHover(false), 200) }}>
                <div className="text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">MCP Servers {servers.length > 0 && `(${servers.filter((s: {enabled?: boolean}) => s.enabled !== false).length}/${servers.length})`}</div>
                {servers.length === 0 ? <div className="text-muted text-[12px] italic">Loading…</div> : servers.map((s: {name: string; enabled?: boolean}) => (
                  <div key={s.name} className={`flex items-center gap-2 py-0.5 text-[12px] ${s.enabled === false ? 'opacity-40' : ''}`}>
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.enabled === false ? 'bg-muted' : 'bg-ok'}`} />
                    <code className="text-text">{s.name}</code>
                  </div>
                ))}
              </div>
            )}
          </div>
          {activeSlot && onReveal && (
            <>
              <div className="mx-2 my-1 border-b border-border" />
              <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { onReveal(); setOpen(false) }}>
                <Locate size={13} className="shrink-0 text-muted" /> Reveal in sidebar
              </button>
              <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { copySessionLink(activeSlot, currentSlot?.title, undefined, mode); setOpen(false) }}>
                <Link2 size={13} className="shrink-0 text-muted" /> Copy session link
              </button>
            </>
          )}
          {activeSlot && slackChannels != null && !currentSlot?.slack_linked && (
            <>
              <div className="mx-2 my-1 border-b border-border" />
              {slackChannels.length === 0 ? (
                <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { onSlackLink(); setOpen(false) }}>
                  <MessageSquareShare size={13} /> Send to Slack
                </button>
              ) : (
                <>
                  <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { onSlackLink(); setOpen(false) }}>
                    <MessageSquareShare size={13} /> Send to Slack
                  </button>
                  {slackChannels.filter(c => c.id !== 'dm').map(ch => (
                    <button key={ch.id} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={() => { onSlackLink(ch.id); setOpen(false) }}>
                      # {ch.name}
                    </button>
                  ))}
                </>
              )}
            </>
          )}
          {activeSlot && currentSlot?.slack_linked && (
            <>
              <div className="mx-2 my-1 border-b border-border" />
              <button className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-ok cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover" onClick={async () => { try { await api.slackLink(activeSlot) } catch {} setOpen(false) }}>
                <MessageSquareShare size={13} /> Post reminder in Slack
              </button>
            </>
          )}
          <div className="mx-2 my-0.5 border-b border-border" />
          <div className="flex items-center gap-1.5 px-3 py-1.5">
              <button type="button" className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex == null ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => pickColor(null)} title="No color" />
              {paletteColors.map((c, i) => (
                <button type="button" key={i} className={`w-4 h-4 rounded-full border-[1.5px] cursor-pointer transition-transform hover:scale-125 ${colorIndex === i ? 'border-text-strong scale-110' : 'border-transparent'}`} style={{ background: c }} onClick={() => pickColor(i)} title={`Color ${i + 1}`} />
              ))}
          </div>
        </div>
      )}
    </div>
  )
}

function SessionStatus() {
  const detail = useAppSelector(s => s.chat.slotStatusDetail[s.chat.activeSlot ?? '']) ?? IDLE_DEFAULT
  const [elapsed, setElapsed] = useState(0)
  const tsRef = useRef(detail.ts)
  tsRef.current = detail.ts
  useEffect(() => {
    if (detail.kind === 'idle') { setElapsed(0); return }
    setElapsed(0)
    const t = setInterval(() => setElapsed(Math.round((Date.now() - tsRef.current) / 1000)), 1000)
    return () => clearInterval(t)
  }, [detail.kind])

  const icon = detail.kind === 'idle' ? <PanelRight size={14} /> : detail.kind === 'thinking' ? <Loader size={11} className="animate-spin" /> : detail.kind === 'streaming' ? <Circle size={9} fill="currentColor" /> : detail.kind === 'tool' ? <Wrench size={11} /> : <Loader size={11} className="animate-spin" />
  const warn = (detail.kind === 'thinking' && elapsed > 30) || (detail.kind === 'streaming' && elapsed > 15)
  const label = warn ? <><AlertTriangle size={11} /> Slow ({elapsed}s)</> : detail.kind === 'idle' ? null : <><span className="truncate">{detail.text}</span><span className="shrink-0"> ({elapsed}s)</span></>

  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] font-mono max-w-[30vw] rounded-md px-1.5 py-0.5 bg-bg/80 backdrop-blur-sm ${warn ? 'text-amber-400' : 'text-muted'}`} title={detail.kind === 'idle' ? 'Ready' : `${detail.text} · ${new Date(detail.ts).toLocaleTimeString()}`}>
      <span className="shrink-0">{icon}</span> {label}
    </span>
  )
}


/** Render user message content with file chips and image markdown. Handles:
 *  - Fresh messages: meta.files present, displayTxt has @relative/path tokens
 *  - Replayed history: no meta.files, content has [attached_file N] /full/path
 *  - Mixed content: images + file attachments in the same message */
function KnowledgeBubbleChip({ knowledge }: { knowledge: { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <span className="block mb-1">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-center gap-1 text-[11px] text-accent bg-accent/10 rounded px-1.5 py-0.5 border-none cursor-pointer hover:bg-accent/20 transition-colors"
        aria-expanded={expanded}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} knowledge context`}
      >
        <BookOpen size={12} className="shrink-0" /> {knowledge.items} knowledge {knowledge.items === 1 ? 'item' : 'items'} · {knowledge.tokens.toLocaleString()} tokens
      </button>
      {expanded && knowledge.content && (
        <div className="mt-1 max-h-[300px] overflow-auto rounded border border-border bg-bg-elevated p-2 text-[11px]">
          {knowledge.content.map((item, i) => (
            <div key={i} className="mb-2 last:mb-0">
              <div className="font-medium text-text-strong">{item.title}</div>
              <pre className="mt-0.5 whitespace-pre-wrap text-muted font-mono leading-[1.4]" style={{ wordBreak: 'break-word' }}>{item.text}</pre>
            </div>
          ))}
        </div>
      )}
    </span>
  )
}

export function renderUserContent(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void) {
  const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
  const knowledge = meta?.knowledge as { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } | undefined

  const knowledgeBadge = knowledge ? (
    <KnowledgeBubbleChip knowledge={knowledge} />
  ) : null

  if (!pastes.length) return <>{knowledgeBadge}{renderFileSegment(content, meta, onFileOpen, 'seg')}</>

  const ranges = findTokenRanges(content, pastes)
  if (!ranges.length) return <>{knowledgeBadge}{renderFileSegment(content, meta, onFileOpen, 'seg')}</>

  // Paste chips are inline by nature, so to keep them flowing with the
  // surrounding text (e.g. "hey [chip] thanks"), render each text segment
  // inline — preserves whitespace and doesn't wrap text in a <p> the way
  // MarkdownRenderer does. Trade-off: block-level markdown (lists, code
  // blocks, headings) inside a message that also contains a paste will
  // render as literal text. That's a rare combination for user messages.
  const out: React.ReactNode[] = []
  let lastIdx = 0
  ranges.forEach((r, i) => {
    // Consume one newline on each side of the token so the chip (inline) and
    // its expanded block absorb the line-break that ChatInput.handlePaste
    // forces around the token. Without this, expanding the chip adds an extra
    // visible line (its own block-level display + the still-rendered \n).
    const trimStart = content[r.start - 1] === '\n' ? r.start - 1 : r.start
    const trimEnd = content[r.end] === '\n' ? r.end + 1 : r.end
    if (trimStart > lastIdx) {
      const seg = content.slice(lastIdx, trimStart)
      if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, `t${i}`))
    }
    out.push(<PastedChip key={`p${i}-${r.block.id}`} block={r.block} />)
    lastIdx = trimEnd
  })
  if (lastIdx < content.length) {
    const seg = content.slice(lastIdx)
    if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, 'tend'))
  }
  return knowledgeBadge ? <>{knowledgeBadge}{out}</> : out
}

/** Inline-flow renderer for a text segment adjacent to a paste chip.
 *  Handles @-file tokens as inline chips; other text is rendered as a
 *  whitespace-preserving span (no markdown). */
function renderInlineSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string) {
  const parsedFiles = parseFiles(content, meta)
  if (!parsedFiles.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{content}</span>
  }
  const metaFiles = (meta?.files || []) as string[]
  let display = content
  const tokenMap = new Map<string, string>()
  if (!metaFiles.length) {
    const basenames = parsedFiles.map(p => p.split('/').pop() || p)
    const dupes = new Set(basenames.filter((n, i) => basenames.indexOf(n) !== i))
    display = display.replace(/\[attached_file \d+\] (\S+)/g, (_, p: string) => {
      const name = p.split('/').pop() || p
      const displayName = dupes.has(name) ? p.split('/').slice(-2).join('/') : name
      tokenMap.set(displayName, p)
      return `@${displayName}`
    })
  } else {
    buildRelMap(parsedFiles, display).forEach((fullPath, suffix) => tokenMap.set(suffix, fullPath))
  }
  if (!tokenMap.size) return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{display}</span>

  const keys = [...tokenMap.keys()].slice(0, 20)
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
  return (
    <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const fullPath = tok && tokenMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors" title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={`Open file ${fullPath}`}>@{tok}</Clickable>
          )
        }
        return <span key={`${keyBase}-p${i}`}>{part}</span>
      })}
    </span>
  )
}

/** File-chip + markdown rendering for a text segment (no paste tokens inside). */
function renderFileSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string) {
  const parsedFiles = parseFiles(content, meta)
  const metaFiles = (meta?.files || []) as string[]

  // No files — always render markdown (user messages support bold, code, links, etc.)
  if (!parsedFiles.length) {
    return <MarkdownRenderer content={content} />
  }

  let display = content
  const tokenMap = new Map<string, string>()

  if (!metaFiles.length) {
    // Replayed history: two-pass basename disambiguation
    const basenames = parsedFiles.map(p => p.split('/').pop() || p)
    const dupes = new Set(basenames.filter((n, i) => basenames.indexOf(n) !== i))
    display = display.replace(/\[attached_file \d+\] (\S+)/g, (_, p: string) => {
      const name = p.split('/').pop() || p
      const displayName = dupes.has(name) ? p.split('/').slice(-2).join('/') : name
      tokenMap.set(displayName, p)
      return `@${displayName}`
    })
  } else {
    // Fresh message: reuse shared suffix-matching
    buildRelMap(parsedFiles, display).forEach((fullPath, suffix) => tokenMap.set(suffix, fullPath))
  }

  if (!tokenMap.size) return display

  // Cap tokens to prevent ReDoS from many alternations
  const keys = [...tokenMap.keys()].slice(0, 20)
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))

  return parts.map((part, i) => {
    const tok = part.match(/^@(.+)$/)?.[1]
    const fullPath = tok && tokenMap.get(tok)
    if (fullPath) {
      return (
        <Clickable key={`${keyBase}-f${i}`} className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
          title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={`Open file ${fullPath}`}>@{tok}</Clickable>
      )
    }
    return part ? <MarkdownRenderer key={`${keyBase}-m${i}`} content={part.trim()} /> : null
  })
}

export default function ChatPage({ mode, embedded, embedMode }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions' } = {}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const provider = useProvider()
  const { botName, avatar } = useBranding()
  const [searchParams, setSearchParams] = useSearchParams()
  const slots = useAppSelector(s => s.dashboard.slots)
  // Filter to slots belonging to this page's surface. Each ChatPage instance
  // is configured with a `mode` prop whose value matches a built-in
  // surface's `slotMode` ('' for /chat, 'orchestrator' for /orchestrated).
  // `filterSlotsBySurface` reads `slot.surface ?? slot.mode ?? ''` so we
  // honor the backend's forward-compat surface field while still working
  // with payloads that pre-date it.
  const filteredSlots = useMemo(() => filterSlotsBySurface(slots, mode || ''), [slots, mode])
  const filteredSlotsRef = useRef(filteredSlots)
  filteredSlotsRef.current = filteredSlots
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  // Scope the unread list to the current surface so the sidebar's
  // "show only unread" toggle, its tooltip count, and the auto-drain
  // effect all agree with the visible session list. Without this scoping,
  // an orchestrator-mode unread inflates the toggle count on /chat (and
  // vice versa) — the same class of cross-mode leak the surface registry
  // fixed for the nav badge. Pass `filteredSlots` (already partitioned by
  // surface above) so the helper skips its inner filter pass.
  const surfaceUnreadSlots = useMemo(
    () => filterUnreadKeysBySurface(unreadSlots, filteredSlots, mode || ''),
    [unreadSlots, filteredSlots, mode],
  )
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const connected = useAppSelector(s => s.dashboard.connected)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const messages = useAppSelector(s => s.chat.messages)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const knowledgeFetch = useKnowledgeFetch(activeSlot)
  const knowledgeFetchRef = useRef(knowledgeFetch)
  knowledgeFetchRef.current = knowledgeFetch
  // User-sent messages (oldest → newest) for ↑/↓ prompt history in the input.
  // Deduplicate consecutive identical prompts to match shell/REPL behavior.
  // `messages` gets a new reference on every streaming chunk; preserve the
  // previous array when user-message content is unchanged so `sentMessages`
  // stays referentially stable and doesn't re-run downstream effects.
  const sentMessagesRef = useRef<string[]>([])
  const sentMessagesSlotRef = useRef<string | null>(null)
  // Per-slot timestamp (ms) of the last soft-stop press, used to arm the
  // force-kill. A force press (second click while soft_pending) arriving
  // within FORCE_KILL_ARMING_MS of that slot's soft stop is treated as an
  // accidental rapid double-tap and ignored, so users can't hard-kill by
  // mashing Stop. Keyed by slot so switching slots can't measure one slot's
  // press against another slot's timestamp.
  const softStopAtMapRef = useRef<Map<string, number>>(new Map())
  const sentMessages = useMemo(() => {
    const out: string[] = []
    for (const m of messages) {
      if (m.role !== 'user') continue
      const text = m.rawText ?? m.content
      if (!text || text === out[out.length - 1]) continue
      out.push(text)
    }
    // Reset the cached reference when switching slots — otherwise two
    // conversations with matching length+tail would share the prior array.
    if (sentMessagesSlotRef.current !== activeSlot) {
      sentMessagesSlotRef.current = activeSlot ?? null
      sentMessagesRef.current = out
      return out
    }
    // Append-only within a slot — full element-wise compare (array is small).
    const prev = sentMessagesRef.current
    if (prev.length === out.length && prev.every((v, i) => v === out[i])) {
      return prev
    }
    sentMessagesRef.current = out
    return out
  }, [messages, activeSlot])
  const slotRunning = useAppSelector(s => s.chat.slotRunning)
  const slotStopping = useAppSelector(s => s.chat.slotStopping)
  const slotLoading = useAppSelector(s => s.chat.slotLoading)
  const pendingQuestion = useAppSelector(s => s.chat.pendingQuestion)
  const slotState = useAppSelector(s => s.chat.slotState)
  const pendingApproval = useAppSelector(s => { const slot = s.dashboard.slots.find(sl => sl.key === s.chat.activeSlot); return slot?.pending_approval ?? false })
  const contextPct = useAppSelector(s => s.chat.slotContextPct[s.chat.activeSlot ?? ''] ?? 0)
  const contextTokens = useAppSelector(s => s.chat.slotContextTokens?.[s.chat.activeSlot ?? ''])
  const subagents = useAppSelector(s => s.chat.subagents)
  const toolLog = useAppSelector(s => s.chat.toolLog)
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const slotHasMore = useAppSelector(s => s.chat.slotHasMore)
  const history = useAppSelector(s => s.chat.history)
  const historyHasMore = useAppSelector(s => s.chat.historyHasMore)

  const drafts = useRef<Record<string, string>>(null!)
  if (drafts.current === null) drafts.current = loadDrafts()
  const fileDrafts = useRef<Record<string, string[]>>(null!)
  if (fileDrafts.current === null) fileDrafts.current = loadFileDrafts()
  // Per-slot collapsed-paste blocks backing the `[ Paste #N · M lines ]` tokens
  // in `input`. Persisted (localStorage, same TTL as text drafts) so the chip
  // survives slot switches / refresh instead of degrading to literal text.
  const pasteDrafts = useRef<Record<string, PasteBlock[]>>(null!)
  if (pasteDrafts.current === null) pasteDrafts.current = loadPasteDrafts()
  const saveDraftsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveDrafts = useCallback(() => { persistDrafts(drafts.current); persistFileDrafts(fileDrafts.current); persistPasteDrafts(pasteDrafts.current) }, [])
  const saveDraftsDebounced = useCallback(() => {
    if (saveDraftsTimer.current) clearTimeout(saveDraftsTimer.current)
    saveDraftsTimer.current = setTimeout(() => { saveDraftsTimer.current = null; saveDrafts() }, DRAFT_SAVE_DEBOUNCE_MS)
  }, [saveDrafts])
  const flushDrafts = useCallback(() => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    saveDrafts()
  }, [saveDrafts])
  const prevSlot = useRef<string | null>(null)
  // Latest-value ref for `activeSlot`, updated every render. Used by async
  // upload callbacks (takeScreenshot, uploadFiles) to detect when the user
  // has switched slots between the initial click and the promise resolving,
  // so the uploaded file lands in the original slot's draft instead of
  // silently appearing in whatever slot is now active.
  const activeSlotRef = useRef(activeSlot); activeSlotRef.current = activeSlot
  const [input, setInput] = useState(() => activeSlot ? drafts.current[activeSlot] ?? '' : '')

  // History suggestions ("Continue a previous chat?") shown above the input on the welcome screen.
  const sendingRef = useRef(false)
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyDismissed, setHistoryDismissed] = useState(false)
  useEffect(() => {
    const q = input.trim()
    if (!q) { setHistoryQuery(''); setHistoryDismissed(false); return }
    setHistoryDismissed(false)
    const t = setTimeout(() => setHistoryQuery(q.toLowerCase()), 300)
    return () => clearTimeout(t)
  }, [input])
  const historySuggestions = useMemo(() =>
    historyQuery && history.length
      ? history.filter(s => (s.title || '').toLowerCase().includes(historyQuery) || s.key.toLowerCase().includes(historyQuery)).slice(0, 5)
      : [],
    [historyQuery, history])
  const isWelcomeState = messages.length === 0 && !slotRunning && !slotLoading && !sendingRef.current && !knowledgeFetch.results.length && !knowledgeFetch.loading && !knowledgeFetch.pendingKnowledge
  const showHistorySuggestions = isWelcomeState && historySuggestions.length > 0 && !historyDismissed
  useEffect(() => {
    if (!showHistorySuggestions) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setHistoryDismissed(true) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showHistorySuggestions])
  // Browse mode is per-session (keyed by slot), not page-global: enabling it in
  // one session must not bleed into another. ChatPage never remounts on slot
  // switch, so a single boolean would leak across every session. Kept in-memory
  // only (resets on reload), matching the prior non-persisted behavior. Mesh-2055.
  const [browseModeBySlot, setBrowseModeBySlot] = useState<Record<string, boolean>>({})
  const browseMode = activeSlot ? (browseModeBySlot[activeSlot] ?? false) : false
  const toggleBrowseMode = () => {
    const slot = activeSlotRef.current
    if (!slot) return
    setBrowseModeBySlot(prev => ({ ...prev, [slot]: !(prev[slot] ?? false) }))
  }
  const pendingInput = useAppSelector(s => s.chat.pendingInput)

  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger)
  const { open: agentDropdown, setOpen: setAgentDropdown, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgents } = useFilteredDropdown(installedAgents)
  const { data: availableModels = [{ name: 'auto', description: 'Default' }] } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => {
      const models = await provider.fetchAvailableModels()
      return [{ name: 'auto', description: 'Default' }, ...models.filter(m => m.name !== 'auto')]
    },
  })
  const { open: modelDropdown, setOpen: setModelDropdown, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(availableModels)
  const [pendingAgent, _setPendingAgent] = useState('')  // agent for next new slot
  const pendingAgentRef = useRef('')
  const setPendingAgent = useCallback((v: string) => { pendingAgentRef.current = v; _setPendingAgent(v) }, [])
  const [pendingModel, _setPendingModel] = useState('')  // model for next new slot
  const pendingModelRef = useRef('')
  const setPendingModel = useCallback((v: string) => { pendingModelRef.current = v; _setPendingModel(v) }, [])
  const pendingProjectRef = useRef('')
  const setPendingProject = useCallback((v: string) => { pendingProjectRef.current = v }, [])
  const [resolvedModel, setResolvedModel] = useState('')  // resolved model for slots without model
  // Sync pendingModel with default agent's model on initial load
  const _initAgent = pendingAgent || defaultAgent || 'default'
  const _initMc = installedAgents.find(a => a.name === _initAgent)
  const _initTemplateName = installedAgents.length > 0 ? provider.resolveAgentTemplate(_initMc || { name: _initAgent }) : ''
  const { data: _initResolvedModel } = useQuery({
    queryKey: ['resolved-model', _initTemplateName, provider.id],
    queryFn: () => provider.resolveModel(_initTemplateName),
    enabled: !!_initTemplateName && !pendingModel,
  })
  useEffect(() => { if (_initResolvedModel && !pendingModel) setPendingModel(_initResolvedModel) }, [_initResolvedModel]) // eslint-disable-line react-hooks/exhaustive-deps
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  const { data: slackChannels } = useQuery({
    queryKey: ['slack-channels'],
    queryFn: () => api.slackChannels().then(c => Array.isArray(c) ? c as {id: string, name: string}[] : null),
  })
  const [handoffOpen, setHandoffOpen] = useState(false)
  useEffect(() => {
    if (!handoffOpen) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') setHandoffOpen(false) }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [handoffOpen])
  const slackLinkMutation = useMutation({
    mutationFn: (channel?: string) => api.slackLink(activeSlot!, channel),
    onSuccess: (r) => {
      if (r.ok) dispatch(updateSlot({ key: activeSlot!, slack_linked: true, slack_channel: r.channel, slack_thread_ts: r.thread_ts }))
      setHandoffOpen(false)
    },
    onError: () => { setHandoffOpen(false) },
  })
  const planActionMutation = useMutation({
    mutationFn: ({ slot, action }: { slot: string; action: string }) => api.planAction(slot, action),
  })
  const handleSlackLink = useCallback((channel?: string) => {
    if (!activeSlot || slackLinkMutation.isPending) return
    slackLinkMutation.mutate(channel)
  }, [activeSlot, slackLinkMutation])
  const [approvalDropdown, setApprovalDropdown] = useState(false)
  const [approvalBtnRect, setApprovalBtnRect] = useState<DOMRect | null>(null)
  const [reasoningEffortDropdown, setReasoningEffortDropdown] = useState(false)
  const [reasoningEffortBtnRect, setReasoningEffortBtnRect] = useState<DOMRect | null>(null)
  const reasoningEffortDropdownRef = useRef<HTMLDivElement>(null)
  const [autoNudgeOpen, setAutoNudgeOpen] = useState(false)
  const [autoNudgeBtnRect, setAutoNudgeBtnRect] = useState<DOMRect | null>(null)
  const [autoNudgeLoop, setAutoNudgeLoop] = useState<AutoNudgeLoop | null>(null)
  const approvalDropdownRef = useRef<HTMLDivElement>(null)
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)
  const [yoloConfirm, setYoloConfirm] = useState(0)
  useEffect(() => {
    if (!approvalDropdown) return
    const handler = (e: MouseEvent) => {
      if (approvalDropdownRef.current?.contains(e.target as Node)) return
      if (approvalBtnRect) {
        const r = approvalBtnRect
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
      }
      setApprovalDropdown(false)
      setYoloConfirm(0)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [approvalDropdown])

  // ── Reasoning effort dropdown click-outside ──
  useEffect(() => {
    if (!reasoningEffortDropdown) return
    const handler = (e: MouseEvent) => {
      if (reasoningEffortDropdownRef.current?.contains(e.target as Node)) return
      if (reasoningEffortBtnRect) {
        const r = reasoningEffortBtnRect
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
      }
      setReasoningEffortDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [reasoningEffortDropdown, reasoningEffortBtnRect])

  // ── Auto-nudge: fetch loop state for active slot, subscribe to WS updates ──
  useEffect(() => {
    // Clear stale state and close the popover on slot switch so it remounts
    // with fresh useState initializers sourced from the new slot's loop.
    // Otherwise the popover's internal message/idleSecs/maxCycles retain
    // values from the previously-active slot and a Start click would arm the
    // wrong nudge on the new session.
    setAutoNudgeLoop(null)
    setAutoNudgeOpen(false)
    if (!activeSlot) return
    let cancelled = false
    fetch(`/api/autonudge/slot/${encodeURIComponent(activeSlot)}`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setAutoNudgeLoop(d.loop || null) })
      .catch(() => {})
    const onEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail as { slot?: string; loop?: AutoNudgeLoop; event?: string }
      if (!detail || detail.slot !== activeSlot) return
      setAutoNudgeLoop(detail.event === 'removed' ? null : (detail.loop ?? null))
    }
    window.addEventListener('autonudge_state', onEvent)
    return () => { cancelled = true; window.removeEventListener('autonudge_state', onEvent) }
  }, [activeSlot])
  const {
    scrollerRef,
    scrollToDisplayIndex,
  } = useScrollManager()

  // Single scroll controller: the virtualizer (`virt`, created below) owns
  // follow + scroll-to-bottom. These refs bridge the early effects/handlers
  // (declared before `virt` in source order) to the virtualizer's API without
  // a temporal-dead-zone hazard — they are populated right after `virt` is
  // created and only read inside callbacks/effects that run post-render.
  const isAtBottomRef = useRef(true)
  const vScrollToBottomRef = useRef<(behavior?: ScrollBehavior) => void>(() => {})
  const mountIndexRef = useRef<(index: number) => boolean>(() => false)
  const scrollToIndexSmoothRef = useRef<(index: number, opts?: { align?: 'start' | 'center'; offset?: number }) => void>(() => {})

  const [prefillHint, setPrefillHint] = useState(false)
  const autoSendRef = useRef<string | null>(null)
  const newSessionRef = useRef(false)
  // True while the challenge-redirect token effect is creating/linking its
  // session. Blocks the auto-select effect from switching to a different slot
  // (which would orphan the freshly slack-linked session and break mirroring).
  const tokenConsumingRef = useRef(
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('token'),
  )
  const inputRef = useRef(input)
  inputRef.current = input
  const browseModeRef = useRef(browseMode)
  browseModeRef.current = browseMode

  // Auto-dismiss prefill hint after 10 seconds
  useEffect(() => {
    if (!prefillHint) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint])

  // Consume pendingInput from Redux (e.g. from "Chat" button on Projects page)
  useEffect(() => {
    if (pendingInput) {
      dispatch(setPendingInput(null))
      const shouldAutoSend = embedded ? false : searchParams.get('autoSend') === '1'
      const wantNew = embedded ? false : searchParams.get('newSession') === '1'
      if (!embedded && (searchParams.get('prefill') || shouldAutoSend)) setSearchParams({}, { replace: true })
      if (shouldAutoSend) { autoSendRef.current = pendingInput; newSessionRef.current = wantNew } else {
        if (activeSlot) { setDraft(drafts.current, activeSlot, pendingInput); saveDraftsDebounced() }
        setInput(pendingInput)
        setPrefillHint(true)
      }
    }
  }, [pendingInput, activeSlot, dispatch, searchParams, setSearchParams, saveDraftsDebounced])

  // Consume chat launch intent from app-sdk (useChatLauncher writes to window.__mc_chat_launch)
  useEffect(() => {
    const intent = (window as any).__mc_chat_launch
    if (!intent || Date.now() - (intent.ts ?? 0) > 10_000) return
    delete (window as any).__mc_chat_launch
    if (intent.agent) setPendingAgent(intent.agent)
    if (intent.message) { autoSendRef.current = intent.message; newSessionRef.current = true }
  }, [])

  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. No auto-send — the user
  // must press Enter to confirm.
  //
  // Three cases, driven by signed claims in the token:
  //  1. session_key present → the originating Slack thread is already linked to
  //     a dashboard session; reconnect to THAT session instead of making a new
  //     one (fixes "thread reply spawns a disconnected session").
  //  2. channel + thread_ts present (no session_key) → fresh thread; create a
  //     new session and auto-link it back to that Slack thread so agent
  //     responses flow into the thread.
  //  3. neither → plain new session (e.g. a top-level channel message).
  // In all cases the prompt is seeded via PREFILL_STORAGE_KEY (the channel the
  // slot-restore effect honors) AND set directly once the target slot is
  // active, so the previous slot's draft can't clobber it.
  useEffect(() => {
    // tokenConsumingRef is initialized true when a token is in the URL; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    const token = new URLSearchParams(window.location.search).get('token')
    if (!token) { tokenConsumingRef.current = false; return }
    // Always strip token from URL to prevent leakage via referrer/history
    window.history.replaceState({}, '', window.location.pathname)
    const prompt = extractPromptFromToken(token)
    if (!prompt) { tokenConsumingRef.current = false; return }
    const { sessionKey, channel, threadTs } = extractSlackContextFromToken(token)
    // Backend session keys are history keys (dashboard:chat-…); the frontend
    // slot key is the bare form.
    const targetSlot = sessionKey ? sessionKey.replace(/^dashboard:/, '') : null
    tokenConsumingRef.current = true
    ;(async () => {
     try {
      let slotKey: string | null = null
      if (targetSlot) {
        // Case 1: reconnect to the existing linked session.
        try {
          await dispatch(switchSlot(targetSlot)).unwrap()
          slotKey = targetSlot
        } catch {
          // Session vanished (deleted/expired) — fall back to a new one.
        }
      }
      if (!slotKey) {
        // No targetSlot (or reconnect failed): create the session HERE and,
        // for a fresh thread, slack-link it so responses mirror to Slack.
        try {
          const slot: any = await dispatch(createSlot({ mode })).unwrap()
          slotKey = slot?.key ?? null
        } catch {
          // ignore — fall back to prefilling the current slot
        }
        // Case 2: auto-link the new session back to the originating thread so
        // responses flow into Slack. Best-effort; failure just leaves it
        // unlinked.
        if (slotKey && channel && threadTs) {
          try { await api.slackLink(slotKey, channel, threadTs) } catch { /* non-fatal */ }
        }
      }
      // We have created/reconnected AND made the target slot active. Critically,
      // clear newSessionRef and pin activeSlot to this slot so send() reuses it
      // on Enter — otherwise send()'s forceNew path would spawn a SECOND,
      // unlinked slot and break Slack mirroring.
      if (slotKey) {
        newSessionRef.current = false
        dispatch(switchSlot(slotKey))
        sessionStorage.setItem(
          PREFILL_STORAGE_KEY,
          JSON.stringify({ slotKey, prompt, ts: Date.now() }),
        )
      }
      setInput(prompt)
      setPrefillHint(true)
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { inputRef.current = input; if (activeSlot) { setDraft(drafts.current, activeSlot, input); saveDraftsDebounced() } }, [input, saveDraftsDebounced]) // eslint-disable-line react-hooks/exhaustive-deps -- activeSlot intentionally omitted; slot-change effect handles that transition
  // Per-slot draft: save current → restore target (persisted to localStorage)
  useEffect(() => {
    // Re-hydrate from localStorage — only pull in keys we don't already have
    // in-memory, so unflushed drafts from rapid slot switches aren't clobbered.
    const stored = loadDrafts()
    for (const [k, v] of Object.entries(stored)) { if (!(k in drafts.current)) drafts.current[k] = v }
    const storedFiles = loadFileDrafts()
    for (const [k, v] of Object.entries(storedFiles)) { if (!(k in fileDrafts.current)) fileDrafts.current[k] = v }
    const storedPastes = loadPasteDrafts()
    for (const [k, v] of Object.entries(storedPastes)) { if (!(k in pasteDrafts.current)) pasteDrafts.current[k] = v }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    prevSlot.current = activeSlot
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    const draftFallback = activeSlot ? drafts.current[activeSlot] ?? '' : ''
    if (raw) {
      try {
        const { slotKey, prompt, ts } = JSON.parse(raw)
        if (Date.now() - (ts ?? 0) > 30_000) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
        else if (slotKey === activeSlot) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(prompt) }
        else { setInput(draftFallback) }
      } catch { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
    } else { setInput(draftFallback) }
    // Restore the incoming slot's staged file attachments (copy so the
    // live state array and the stored draft don't share a reference).
    setPendingFiles(activeSlot ? (fileDrafts.current[activeSlot] ?? []).slice() : [])
    // Restore the incoming slot's collapsed-paste blocks (deep copy so the live
    // state and the stored draft don't share references). Without this the
    // token text rehydrates from the text draft but its backing block is gone,
    // leaving a dead `[ Paste #N · M lines ]` literal in the input.
    setPasteBlocks(activeSlot
      ? (pasteDrafts.current[activeSlot] ?? []).map(b => ({ ...b }))
      : [])
    knowledgeFetchRef.current.clearResults()
    setUploadError('')
    setUploadNotice('')
    flushDrafts()
  }, [activeSlot, flushDrafts])
  // Persist drafts on unmount (navigating away from chat page)
  useEffect(() => () => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    flushDrafts()
  }, [flushDrafts])
  // Flush pending draft save on tab close / refresh (debounce may not fire)
  useEffect(() => {
    const h = () => {
      if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
      if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
      if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
      flushDrafts()
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [flushDrafts])
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [projectPickerOpen, setProjectPickerOpen] = useState(false)
  const [projectBtnRect, setProjectBtnRect] = useState<DOMRect | null>(null)

  // Prevent Chrome from navigating to dropped files.
  // Must be on document to catch drops anywhere on the page.
  useEffect(() => {
    const preventNav = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }
    }
    document.addEventListener('dragover', preventNav)
    document.addEventListener('drop', preventNav)
    return () => {
      document.removeEventListener('dragover', preventNav)
      document.removeEventListener('drop', preventNav)
    }
  }, [])

  const [dragOver, setDragOver] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const [snipFrame, setSnipFrame] = useState<HTMLCanvasElement | null>(null)
  const pendingFilesRef = useRef(pendingFiles)
  useEffect(() => {
    pendingFilesRef.current = pendingFiles
    if (activeSlot) {
      setFileDraft(fileDrafts.current, activeSlot, pendingFiles)
      saveDraftsDebounced()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- activeSlot
    // intentionally omitted; slot-change effect handles that transition
  }, [pendingFiles, saveDraftsDebounced])
  // Collapsed paste blocks backing the `[ Paste #N · M lines ]` tokens in
  // `input`. Persisted per-slot via chatPasteDrafts (localStorage, 30-day TTL)
  // so they survive slot switches / refresh; cleared on send and slot delete.
  const [pasteBlocks, setPasteBlocks] = useState<PasteBlock[]>([])
  const pasteBlocksRef = useRef(pasteBlocks)
  useEffect(() => {
    pasteBlocksRef.current = pasteBlocks
    // Live-persist the active slot's blocks so a slot switch / refresh restores
    // them alongside the text draft (mirrors the pendingFiles effect above).
    if (activeSlot) {
      setPasteDraft(pasteDrafts.current, activeSlot, pasteBlocks)
      saveDraftsDebounced()
    }
    // activeSlot intentionally omitted; slot-change effect handles that transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pasteBlocks, saveDraftsDebounced])
  const [uploadError, setUploadError] = useState('')
  const [uploadNotice, setUploadNotice] = useState('')
  const isMac = useAppSelector(s => s.dashboard.status?.platform) === 'darwin'
  const { data: sttCfg } = useQuery({
    queryKey: ['stt-config'],
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean }>,
  })
  const sttStreaming = !!sttCfg?.streaming
  const frozenInputRef = useRef<string | null>(null)
  // Drops late-arriving partials/finals from a previous slot or stopped
  // session. `stop()` is async (up to 5s for backend close) — without
  // this guard, a delayed onFinal would overwrite text the user has
  // already typed in the new slot.
  const sttDisarmedRef = useRef(false)
  const voice = useVoiceInput(
    useCallback((text: string) => {
      if (sttDisarmedRef.current) return
      const base = frozenInputRef.current ?? ''
      setInput(base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
      frozenInputRef.current = null
    }, []),
    {
      streaming: sttStreaming,
      onPartial: useCallback((text: string) => {
        if (sttDisarmedRef.current) return
        // Snapshot BEFORE setInput so the updater stays pure (no ref
        // mutation inside a function React may invoke twice).
        if (frozenInputRef.current === null) frozenInputRef.current = inputRef.current
        const base = frozenInputRef.current
        setInput(base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
      }, []),
    }
  )
  // Keep a ref to the latest `voice` so effects that intentionally omit
  // `voice` from their deps always invoke the current instance — otherwise
  // they'd capture a stale `toggle`/`recording` whenever `voice` identity
  // changes (e.g. when `sttStreaming` flips).
  const voiceRef = useRef(voice)
  useEffect(() => { voiceRef.current = voice }, [voice])
  // Re-arm when the user explicitly (re)starts recording — wrap toggle.
  // Depend on the individual stable members actually read so this callback
  // is only re-created when they change. `[voice]` would recreate every
  // render (hooks don't memoize their return by default), re-rendering all
  // child components that receive `toggleVoice` as a prop.
  const toggleVoice = useCallback(() => {
    if (!voice.recording) {
      sttDisarmedRef.current = false
      // Reset stale snapshot from a prior session that ended without
      // finals — otherwise onPartial sees a non-null ref, skips
      // re-snapshotting, and text typed between sessions is dropped.
      frozenInputRef.current = null
    }
    voice.toggle()
  }, [voice.recording, voice.toggle])
  // Stop any in-flight recording and drop the frozen prefix when the user
  // switches slots so a late-arriving transcript can't leak into the wrong
  // session. Disarm first so any in-flight final from the previous slot is
  // silently dropped when it eventually arrives.
  useEffect(() => {
    frozenInputRef.current = null
    sttDisarmedRef.current = true
    if (voiceRef.current.recording) voiceRef.current.toggle()
  }, [activeSlot])
  // (Streaming-off teardown now lives in useVoiceInput — see its effect on
  // [streamEnabled, streamRecording, streamStop]. Routing through voice.toggle
  // here is racy because `useVoiceInput` flips its returned `recording` to the
  // batch value on the same render that `streamEnabled` goes false.)

  const panel = usePanelState()
  const diffPanel = useDiffPanel()
  const [diffLineNumbers, setDiffLineNumbers] = useState(false)
  const touchedFiles = useTouchedFiles(activeSlot ?? undefined)

  // Auto-track files touched by tool calls (read, write, grep, glob)
  const lastToolLen = useRef(0)
  useEffect(() => {
    if (toolLog.length <= lastToolLen.current) { lastToolLen.current = toolLog.length; return }
    const newEntries = toolLog.slice(lastToolLen.current)
    lastToolLen.current = toolLog.length
    for (const e of newEntries) {
      if (e.type !== 'tool' || !e.input) continue
      const name = e.text?.replace(/^🔧\s*/, '') ?? ''
      // Extract paths from tool input JSON preview
      try {
        const inp = e.input
        let paths: string[] = []
        if (/^(read|write)$/i.test(name)) {
          // read: {"operations":[{"path":"/..."}]}  write: {"path":"/..."}
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/g)
          if (pm) paths = pm.map(m => m.match(/"(\/[^"]+)"$/)?.[1]).filter(Boolean) as string[]
        } else if (/^grep$/i.test(name)) {
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/)
          if (pm?.[1]) paths = [pm[1]]
        } else if (/^glob$/i.test(name)) {
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/)
          if (pm?.[1]) paths = [pm[1]]
        }
        for (const p of paths) {
          if (touchedFiles.shouldScanAdd(e.ts)) touchedFiles.addFile(p, 'tool')
        }
      } catch { /* ignore parse errors */ }
    }
  }, [toolLog.length, touchedFiles.addFile, touchedFiles.shouldScanAdd])

  const { colorTheme } = useTheme()
  // Mirror colorTheme into a ref so the `send` callback (which does not depend
  // on colorTheme, to avoid re-creating on every theme switch) can always read
  // the current theme without going stale — otherwise a theme change with no
  // activeSlot change sends the previous theme's color_theme to the backend,
  // mis-injecting the persona.
  const colorThemeRef = useRef(colorTheme)
  useEffect(() => { colorThemeRef.current = colorTheme }, [colorTheme])
  // Read file content via queryClient.fetchQuery so we get React Query's
  // caching/deduplication on repeated opens (re-opening the same file is
  // instant for ~10s) AND proper error semantics (queryFn throws → catch
  // block runs). useMutation was the wrong tool for a read operation.
  // The `ok` flag gates whether the file is recorded in history — 404s and
  // other HTTP failures show a placeholder in the panel but should NOT
  // pollute the history list with files that don't exist on disk.
  const handleFileOpen = useCallback(async (filePath: string) => {
    // Plugin host integration: notify the IntelliJ plugin (if active) so
    // it can open the file natively in the IDE editor. If the plugin
    // handles file opens, skip the dashboard's DiffPanel — the user wanted
    // IDE-native, not in-dashboard.
    try { window.dispatchEvent(new CustomEvent('kiroclaw-file-open', { detail: { path: filePath } })) } catch { /* ignore */ }
    if ((window as unknown as { __kiroclawPluginHandlesFiles?: boolean }).__kiroclawPluginHandlesFiles) return
    try {
      const [{ text, ok }] = await Promise.all([
        queryClient.fetchQuery({
          queryKey: ['file-read', filePath],
          queryFn: async () => {
            const url = fileReadUrl(filePath)
            const res = await fetch(url)
            const text = res.ok
              ? await res.text()
              : res.status === 404 ? '_File not found on disk. It may have been moved or deleted._'
              : '_Unable to read file._'
            return { text, ok: res.ok }
          },
          staleTime: 10_000,
        }),
        queryClient.prefetchQuery({
          queryKey: ['file-diff', filePath],
          queryFn: () => api.fileDiff(filePath),
        }),
      ])
      panel.openPanel(filePath, text, activeSlotRef.current ?? null)
      diffPanel.closeDiff()
      if (ok) touchedFiles.addFile(filePath, 'history')
    } catch {
      panel.openPanel(filePath, '_Error reading file_', activeSlotRef.current ?? null)
    }
  }, [queryClient, panel, diffPanel, touchedFiles])

  // Open the Monaco diff panel from a file-change chip click. Closes the
  // markdown viewer and the activity panel so panels stay mutually exclusive.
  const handleOpenDiff = useCallback((filePath: string, modified: string, original: string) => {
    // If the IntelliJ plugin's file bridge is active, dispatch the event
    // with before/after content so the plugin can show a native IntelliJ
    // diff viewer (with syntax highlighting). Skip the dashboard's
    // own DiffPanel in that case — the plugin sets the flag on page load.
    try {
      window.dispatchEvent(new CustomEvent('kiroclaw-file-open', {
        detail: { path: filePath, before: original, after: modified },
      }))
    } catch { /* ignore */ }
    if ((window as unknown as { __kiroclawPluginHandlesFiles?: boolean }).__kiroclawPluginHandlesFiles) return
    panel.closePanel()
    if (activityOpen) dispatch(toggleActivity())
    diffPanel.openDiff(filePath, modified, original)
  }, [panel, activityOpen, dispatch, diffPanel])

  // Auto-surface files modified by the agent (carried in m.meta.file_changes)
  // into the activity Files tab so the user sees a unified list. Skip files
  // referenced by messages older than the last 'tool' watermark — once the
  // user clears suggested files, those entries stay gone unless the agent
  // touches them again in a newer turn.
  useEffect(() => {
    for (const m of messages) {
      const ts = typeof m.ts === 'string' ? Date.parse(m.ts) : (m.ts as unknown as number) || 0
      if (!touchedFiles.shouldScanAdd(ts)) continue
      const fc = (m.meta as Record<string, unknown> | undefined)?.file_changes as Array<{ path: string }> | undefined
      if (fc) for (const f of fc) touchedFiles.addFile(f.path, 'tool')
    }
  }, [messages.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleFork = useCallback(async (visibleIndex: number) => {
    if (!activeSlot) return
    const prompt = inputRef.current.trim()
    try {
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, prompt: prompt || undefined })).unwrap()
      if (result.ok) {
        if (prompt) setInput('')
        await dispatch(switchSlot(result.key))
        if (prompt) {
          // Fire-and-forget: the forked slot's kiro-cli session spins up on
          // first prompt. If the backend isn't ready yet, sendChat returns
          // ok:false — restore the prompt to the input box AND surface an
          // alert so the user sees *why* the prompt reappeared (raised in
          // review). Silent restore was confusing UX.
          const onSendFailure = (detail: string) => {
            if (!inputRef.current.trim()) setInput(prompt)
            alert(`Fork created, but sending the prompt failed (${detail}). Please retry.`)
          }
          api.sendChat(prompt, result.key).then(async r => {
            if (!r.ok) {
              const body = await r.text().catch(() => '')
              onSendFailure(body || `HTTP ${r.status}`)
            }
          }).catch(e => {
            onSendFailure(e instanceof Error ? e.message : String(e))
          })
        }
      } else {
        alert('Fork failed: ' + (result.error || 'unknown error'))
      }
    } catch (e) {
      if (prompt && !inputRef.current.trim()) setInput(prompt)
      alert('Fork failed: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [activeSlot, dispatch])

  const handlePlanFromHere = useCallback(async (visibleIndex: number) => {
    if (!activeSlot) return
    try {
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, mode: 'orchestrator' })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
        if (mode !== 'orchestrator') navigate('/orchestrated')
      } else {
        alert('Plan from here failed: ' + (result.error || 'unknown error'))
      }
    } catch (e) {
      alert('Plan from here failed: ' + (e instanceof Error ? e.message : String(e)))
    }
  }, [activeSlot, dispatch, mode, navigate])

  const handleFileSave = useCallback(async (filePath: string, content: string) => {
    const res = await fetch('/api/file-write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    })
    if (!res.ok) throw new Error(`Save failed: ${res.status}`)
  }, [])

  const takeScreenshot = useCallback(async () => {
    // Capture the slot at click-time. If the user switches away before the
    // screenshot promise resolves, we must land the file in the slot the user
    // was looking at when they clicked — not whatever slot is now active.
    const requestSlot = activeSlotRef.current
    setUploading(true)
    try {
      const { path } = await api.screenshot()
      if (path) {
        if (activeSlotRef.current === requestSlot) {
          setPendingFiles(prev => [...prev, path])
        } else if (requestSlot) {
          // Slot changed during the await — divert the file into the request
          // slot's persisted draft so it's waiting when the user goes back.
          const cur = fileDrafts.current[requestSlot] ?? []
          setFileDraft(fileDrafts.current, requestSlot, [...cur, path])
          saveDrafts()
        }
      }
    } catch { /* user cancelled */ }
    setUploading(false)
  }, [saveDrafts])

  /** Screen capture entry: cross-platform snip+crop when supported, else native macOS screenshot. */
  const handleCapture = useCallback(async () => {
    if (!screenSnipSupported) { takeScreenshot(); return }
    const canvas = await captureScreen()
    if (canvas) setSnipFrame(canvas)
  }, [takeScreenshot])

  /** Upload files via browser File API (cross-platform) */
  const uploadFiles = useCallback(async (files: File[]) => {
    if (!files.length) return
    // Same slot-capture pattern as takeScreenshot — see note there.
    const requestSlot = activeSlotRef.current
    setUploadError('')
    setUploadNotice('')
    if (files.length > 20) { setUploadError('Too many files (max 20)'); return }
    const big = files.find(f => f.size > 50 * 1024 * 1024)
    if (big) { setUploadError(`File too large: ${big.name} (max 50 MB)`); return }
    setUploading(true)
    try {
      const res = await api.uploadFiles(files)
      if (res.error) {
        setUploadError('Upload failed: ' + res.error)
      } else if (res.paths?.length) {
        if (activeSlotRef.current === requestSlot) {
          setPendingFiles(prev => [...prev, ...res.paths])
        } else if (requestSlot) {
          const cur = fileDrafts.current[requestSlot] ?? []
          setFileDraft(fileDrafts.current, requestSlot, [...cur, ...res.paths])
          saveDrafts()
        }
      }
      if (!res.error && res.resized?.length) {
        const n = res.resized.length
        const first = res.resized[0]
        setUploadNotice(
          n === 1
            ? `Resized ${first.name} (${first.fromW}×${first.fromH} → ${first.toW}×${first.toH}) to fit model limits.`
            : `Resized ${n} images to fit model limits.`,
        )
      }
    } catch { setUploadError('Upload failed — check file type and size (max 50 MB)') }
    setUploading(false)
  }, [saveDrafts])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setDragOver(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length) {
      uploadFiles(files)
    }
  }, [uploadFiles])

  // Scroll to bottom helper — delegates to the virtualizer (single controller).
  const scrollBottom = useCallback((instant: boolean = false) => {
    vScrollToBottomRef.current(instant ? 'auto' : 'smooth')
  }, [])

  // Navigate to a (possibly off-window) display index: mount it first via the
  // virtualizer so the DOM-based scroll can find it, then scroll next frame.
  const navToDisplayIndex = useCallback((
    idx: number,
    opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number },
  ) => {
    // Signal WidgetFrames that a jump is starting so the span of widgets
    // mountIndex is about to union doesn't all build their iframes in one
    // frame (see PROGRAMMATIC_BUILD_DELAY_MS in WidgetFrame).
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const jumpedFar = mountIndexRef.current(idx)
    // A FAR jump replaces the window, so the rows between the old viewport and
    // the target are NOT mounted — a smooth glide would scrub the scroller
    // through blank spacer (the "occasional flicker" on the ↑/jump pills when
    // the target is past a long turn). Teleport instantly instead: the target
    // block is already mounted so it shows immediately, and overflow-anchor
    // keeps it stable as its rows measure. NEAR jumps keep their smooth glide
    // (mountIndex unioned the whole path, so there's nothing blank to scrub).
    const behavior: ScrollBehavior = jumpedFar ? 'auto' : (opts?.behavior ?? 'smooth')
    requestAnimationFrame(() => scrollToDisplayIndex(idx, { ...opts, behavior }))
  }, [scrollToDisplayIndex])

  // "Scroll to previous user message" pill — tracks topmost visible item
  const topmostIdxRef = useRef(0)
  const [hasUserMsgAbove, setHasUserMsgAbove] = useState(false)
  const displayItemsRef = useRef<DisplayItem[]>([])
  // Update topmost index from scroll position (replaces Virtuoso rangeChanged)
  const updateTopmostIdx = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // First item whose bottom is still below the scroller's top edge = the
    // topmost item not yet fully scrolled past the fold. Measure with
    // getBoundingClientRect (viewport-relative) so the origin matches the
    // scroller regardless of which ancestor is the items' offsetParent —
    // consistent with useScrollManager, which also deliberately avoids offsetTop.
    const items = el.querySelectorAll('[data-display-index]')
    const cTop = el.getBoundingClientRect().top
    for (const item of items) {
      const htmlItem = item as HTMLElement
      if (htmlItem.getBoundingClientRect().bottom > cTop) {
        const idx = parseInt(htmlItem.getAttribute('data-display-index') || '0', 10)
        topmostIdxRef.current = idx
        setHasUserMsgAbove(findPrevUserMsgDisplayIdx(displayItemsRef.current, idx) >= 0)
        break
      }
    }
  }, [scrollerRef])
  // rAF-throttle the per-scroll topmost recompute: updateTopmostIdx does a
  // querySelectorAll + getBoundingClientRect loop (a forced layout read), and a
  // fling fires scroll dozens of times/sec. Coalesce to at most once per frame,
  // mirroring the virtualizer's own scroll-listener throttle so this handler
  // doesn't reintroduce the scroll-time main-thread cost this CR removes.
  const topmostRafRef = useRef(false)
  const onScrollTopmost = useCallback(() => {
    if (topmostRafRef.current) return
    topmostRafRef.current = true
    requestAnimationFrame(() => {
      topmostRafRef.current = false
      updateTopmostIdx()
    })
  }, [updateTopmostIdx])
  const scrollToPrevUserMessage = useCallback(() => {
    const target = findPrevUserMsgDisplayIdx(displayItemsRef.current, topmostIdxRef.current)
    if (target < 0) return
    // Signal WidgetFrames that a programmatic jump is starting so any widget
    // the smooth scroll sweeps PAST defers building its (expensive) Tailwind
    // iframe until the glide settles (see PROGRAMMATIC_BUILD_DELAY_MS in
    // WidgetFrame). Without this, the native smooth scroll crosses the span
    // fast enough to mount+build several widget iframes synchronously mid-glide
    // — the ↑-button jank (a 100ms+ 'message' handler stall). navToDisplayIndex
    // already emits this for its mountIndex path; the smooth path had dropped it.
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    // Human-like smooth scroll (no wide window pre-mount) — see
    // scrollToIndexSmooth. Avoids leaving a broad span of animated widgets
    // mounted+oscillating after the jump.
    scrollToIndexSmoothRef.current(target, { align: 'start', offset: -72 })
  }, [])

  // Sticky-bottom scroll state is owned by the virtualizer (`virt.isAtBottom`,
  // wired below). No local mirror — a single source of truth avoids the
  // dual-controller drift that caused the follow/yank regressions.

  // New content while following is handled inside the virtualizer (RO re-pin
  // for in-place growth + append layout-effect pin for new items), so ChatPage
  // no longer runs its own message-length scroll effect.
  useEffect(() => { dispatch(fetchHistory(false)) }, [dispatch])
  // Persist active slot to localStorage for refresh recovery (per-mode)
  const slotStorageKey = `mc-active-slot-${mode || 'chat'}`
  const slotStorageKeyRef = useRef(slotStorageKey); slotStorageKeyRef.current = slotStorageKey
  useEffect(() => {
    if (activeSlot && filteredSlots.some(s => s.key === activeSlot)) {
      localStorage.setItem(slotStorageKey, activeSlot)
    }
  }, [activeSlot, slotStorageKey, filteredSlots])
  useEffect(() => () => { if (activeSlotRef.current && filteredSlotsRef.current.find(s => s.key === activeSlotRef.current)) localStorage.setItem(slotStorageKeyRef.current, activeSlotRef.current) }, [])
  // Handle ?sid= (or legacy ?slot=) query parameter — activate the given session
  // Capture initial ?sid= at mount time before any effect can overwrite it
  const initialSidRef = useRef(searchParams.get('sid') || searchParams.get('slot'))
  const initialMsgRef = useRef(searchParams.get('msg'))
  const initialNewRef = useRef(searchParams.get('new') === '1')
  const pendingSidRef = useRef(!!initialSidRef.current)
  const [sidError, setSidError] = useState('')
  const [highlightTs, setHighlightTs] = useState<string | null>(null)
  // Embed ?new=1: create a new chat slot and navigate to it
  const embedNewSlotMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ mode })).unwrap(),
    onSuccess: (slot: any) => {
      if (slot?.key) navigate(`/embed/chat/${slot.key}`, { replace: true })
    },
  })
  useEffect(() => {
    if (!initialNewRef.current || !embedMode) return
    initialNewRef.current = false
    embedNewSlotMutation.mutate()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // On mount, URL ?sid= drives which session is active (URL wins over localStorage)
  useEffect(() => {
    if (embedded && !embedMode) return
    const urlSlot = initialSidRef.current
    if (!urlSlot || filteredSlots.length === 0) return
    if (filteredSlots.some(s => s.key === urlSlot)) {
      initialSidRef.current = null
      dispatch(switchSlot(urlSlot))
    }
    // Don't error immediately — slot may arrive via SSE shortly
  }, [filteredSlots, dispatch])
  // React to ?sid= changes AFTER mount — required for plugin tab switching
  // where the URL is updated via react-router navigate() (soft nav). The
  // mount-only initialSidRef approach above misses these updates because
  // the component doesn't remount across soft navs. Without this effect
  // the "activeSlot → URL" sync below would rewrite the URL back to the
  // current activeSlot instead of switching to the slot the URL is asking
  // for.
  //
  // GATED to embed mode only: in the main UI, URL changes are always
  // derived from activeSlot (our own writes), so reacting to them here
  // creates a feedback loop with the activeSlot→URL sync effect — they
  // race on the same render and ping-pong activeSlot back to its prior
  // value.
  useEffect(() => {
    if (!embedMode) return
    const urlSid = searchParams.get('sid') || searchParams.get('slot')
    if (!urlSid || urlSid === activeSlot) return
    if (filteredSlots.some(s => s.key === urlSid)) {
      pendingSidRef.current = true
      dispatch(switchSlot(urlSid))
    }
  }, [searchParams, filteredSlots, activeSlot, dispatch, embedMode])
  // Timeout: if slot never appears after 5s, show error
  useEffect(() => {
    const urlSlot = initialSidRef.current
    if (!urlSlot) return
    const timer = setTimeout(() => {
      if (initialSidRef.current) {
        initialSidRef.current = null
        pendingSidRef.current = false
        setSidError(`Session "${urlSlot}" not found`)
      }
    }, 5000)
    return () => clearTimeout(timer)
  }, [])
  // Sync activeSlot → ?sid= in URL (persistent deep-link)
  // Skip entirely when embedded — URL belongs to the host app
  const basePath = embedMode === 'chat' || embedMode === 'sessions' ? '/embed/chat' : mode === 'orchestrator' ? '/orchestrated' : '/chat'
  const location = useLocation()
  const searchParamsRef = useRef(searchParams)
  searchParamsRef.current = searchParams
  useEffect(() => {
    if (embedded && !embedMode) return
    // In sessions embed mode, the URL is `/embed/sessions` regardless of
    // activeSlot. Navigation away from sessions is driven by the explicit
    // onSelectSlot callback in ChatSidebar — never auto-navigate from here,
    // since activeSlot may change due to background state (initial load,
    // localStorage hydration, WS updates) which would unwantedly bounce
    // the user back into chat view.
    if (embedMode === 'sessions') return
    const sp = searchParamsRef.current
    if (!activeSlot) {
      if (sp.has('sid') && !initialSidRef.current && !pendingSidRef.current) {
        navigate(basePath, { replace: true })
      }
      return
    }
    pendingSidRef.current = false
    const current = sp.get('sid')
    const slot = filteredSlots.find(s => s.key === activeSlot)
    const slug = slot?.title && slot.title !== slot.key ? toSlug(slot.title) : ''
    const expectedPath = `${basePath}${slug ? '/' + slug : ''}`
    if (current === activeSlot && location.pathname === expectedPath) return
    const next = new URLSearchParams(sp)
    next.set('sid', activeSlot)
    next.delete('slot')
    next.delete('prefill')
    next.delete('autoSend')
    next.delete('newSession')
    next.delete('msg')
    navigate(`${basePath}${slug ? '/' + slug : ''}?${next}`, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, filteredSlots, navigate, basePath, location.pathname, embedded])
  // Re-fetch slot messages on mount (handles nav away + back).
  // Skip when newSession=1 — createSlot in send() will set the active slot;
  // dispatching switchSlot here would race and overwrite it.
  useEffect(() => { if (activeSlot && !newSessionRef.current && filteredSlotsRef.current.find(s => s.key === activeSlot)) dispatch(switchSlot(activeSlot)) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // Clear activeSlot when it belongs to a different mode (page switch)
  useEffect(() => {
    if (activeSlot && slots.length > 0 && !filteredSlots.find(s => s.key === activeSlot)) {
      dispatch(setActiveSlot(null))
    }
  }, [activeSlot, slots.length, filteredSlots, dispatch])
  // Auto-select slot after refresh — restore from localStorage or pick first
  // If no slots exist at all, auto-create one so the user lands in a ready chat
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  const autoCreatedRef = useRef(false)
  useEffect(() => {
    if (activeSlot) return
    // Don't auto-select/auto-create while the challenge-redirect token effect
    // is still creating + slack-linking its session; otherwise we'd switch to
    // a different slot and orphan the linked one (breaking Slack mirroring).
    if (tokenConsumingRef.current) return
    if (searchParams.get('slot') || searchParams.get('sid') || initialSidRef.current) return
    if (filteredSlots.length > 0) {
      const saved = localStorage.getItem(slotStorageKey)
      const target = saved && filteredSlots.find(s => s.key === saved) ? saved : filteredSlots[0].key
      dispatch(switchSlot(target))
    } else if (connected && slotsLoaded && !autoCreatedRef.current) {
      // Connected, slots fetched, and truly empty — auto-create one
      autoCreatedRef.current = true
      dispatch(createSlot({ agent: defaultAgent || undefined, mode }))
    }
  }, [activeSlot, filteredSlots, searchParams, dispatch, slotStorageKey, connected, slotsLoaded, defaultAgent, mode])

  // Slot switch: the virtualizer (keyed on sessionId = activeSlot) force-pins
  // to the true bottom itself in a layout effect. Here we just re-arm the
  // local at-bottom ref used by the gating effects below.
  const prevSlotRef = useRef<string | null>(null)
  useEffect(() => {
    if (activeSlot !== prevSlotRef.current) {
      prevSlotRef.current = activeSlot
      isAtBottomRef.current = true
    }
  }, [activeSlot])

  // Auto-scroll during streaming — only when pinned to bottom
  const lastMsg = messages[messages.length - 1]
  const isStreaming = lastMsg?.role === 'streaming'
  // Follow-up options derived from the last assistant message in the current chat.
  // Swapping chats (activeSlot change) → messages change → memo recomputes fresh.
  const { followUpOptions, followUpIsPlan } = useMemo(() => {
    if (isStreaming) return { followUpOptions: [] as string[], followUpIsPlan: false }
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.role === 'user') return { followUpOptions: [] as string[], followUpIsPlan: false }
      if (m.role === 'assistant' && m.content) {
        const { options, isPlan } = parseOptions(m.content)
        return { followUpOptions: options, followUpIsPlan: isPlan }
      }
    }
    return { followUpOptions: [] as string[], followUpIsPlan: false }
  }, [messages, isStreaming])
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  const followUpOptionsKey = followUpOptions.join('\x00')
  useEffect(() => { setFollowUpPicked(new Set()) }, [followUpOptionsKey, activeSlot])
  const { data: dashCfg } = useQuery<{ quick_send?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
  const planTaskId = useMemo(() => {
    for (const m of messages) {
      const match = m.content?.match(/<!-- plan_task_id:(\S+) -->/)
      if (match) return match[1]
    }
    return ''
  }, [messages])

  // Scroll to show Footer when agent starts running (loading indicator appears)
  const prevRunningRef = useRef(false)
  useEffect(() => {
    if (slotRunning && !prevRunningRef.current && isAtBottomRef.current) {
      setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    }
    prevRunningRef.current = slotRunning
  }, [slotRunning, scrollBottom])

  // Sync slotRunning from WS slot updates.
  useEffect(() => {
    if (!activeSlot) return
    const s = slots.find(s => s.key === activeSlot)
    if (!s) return
    dispatch(setSlotRunning(s.running))
    dispatch(setSlotStopping(s.stopping ?? false))
  }, [slots, activeSlot, dispatch])

  const handleResumeSession = useCallback(async (key: string, title: string) => {
    try {
      await dispatch(resumeFromHistory({ key, title })).unwrap()
      if (activeSlot && activeSlot !== key) {
        delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]; prevSlot.current = null; saveDrafts()
        dispatch(deleteSlot(activeSlot)).unwrap().catch(() => {})
      }
    } catch { /* resume failed — keep current slot */ }
  }, [activeSlot, dispatch, saveDrafts])
  // Raw send — sends pre-built text directly to the server
  const modeRef = useRef(mode)
  modeRef.current = mode
  const planActionMutationRef = useRef(planActionMutation)
  planActionMutationRef.current = planActionMutation

  const send = useCallback(async (optionText?: string, targetSlot?: string) => {
    const raw = (optionText || inputRef.current).trim()
    if (!raw && !pendingFilesRef.current.length) return

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    const slashResult = await interceptSlashCommand(raw, activeSlot, dispatch)
    if (slashResult.intercepted) {
      if (!optionText) { setInput(''); setPasteBlocks([]) }
      return
    }

    // Knowledge fetch: intercept @knowledge prefix, show picker instead of sending
    const kq = extractKnowledgeQuery(raw)
    if (kq && !optionText) {
      knowledgeFetchRef.current.searchKnowledge(kq)
      setInput('')
      return
    }

    const { txt, displayTxt, filePaths } = prepareSendPayload(raw, pendingFilesRef.current)
    // Expand paste tokens for the LLM; UI-facing displayTxt keeps the tokens
    // intact so the user bubble can render them as clickable chips.
    const activePastes = pasteBlocksRef.current
    let llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    const browsing = browseModeRef.current
    // Prepend knowledge context if pending
    let knowledgeBlock: import('./chat/useKnowledgeFetch').KnowledgeBlock | null = null
    if (knowledgeFetchRef.current.pendingKnowledge) {
      knowledgeBlock = knowledgeFetchRef.current.pendingKnowledge
      llmTxt = expandKnowledgeBlock(knowledgeBlock) + '\n' + llmTxt
    }
    knowledgeFetchRef.current.clearPending()
    const bubblePastes = pruneBlocksUtil(displayTxt, activePastes)
    if (bubblePastes.length) saveStoredPaste(llmTxt, displayTxt, bubblePastes, filePaths)

    setPrefillHint(false)
    if (!optionText) { setInput(''); setPendingFiles([]); setPasteBlocks([]); if (activeSlot) { delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]; saveDrafts() } }
    let slot = targetSlot ?? activeSlot
    // Only a normal (non-targeted) send consumes the one-shot "new session"
    // intent. A targeted send — e.g. submitting document comments to the
    // document's origin slot — must leave it intact for the user's next send.
    let forceNew = false
    if (!targetSlot) {
      forceNew = newSessionRef.current
      newSessionRef.current = false
    }
    if (!slot || forceNew) {
      sendingRef.current = true;
      const result = await dispatch(createSlot({ agent: pendingAgentRef.current || defaultAgent || undefined, model: pendingModelRef.current || undefined, mode: modeRef.current })).unwrap();
      slot = result.key;
      if (pendingProjectRef.current) { await api.chatSlotProject(result.key, pendingProjectRef.current).catch(e => console.error('chatSlotProject failed', e)) }
    }
    setPendingAgent(''); setPendingModel(''); setPendingProject('')
    // Build meta for persistence (knowledge, files, pastes)
    const meta: Record<string, unknown> = {}
    if (filePaths.length) meta.files = filePaths
    if (bubblePastes.length) meta.pastes = bubblePastes
    if (knowledgeBlock) meta.knowledge = { items: knowledgeBlock.items.length, tokens: knowledgeBlock.totalTokens, titles: knowledgeBlock.items.map(i => i.title), content: knowledgeBlock.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }
    const metaPayload = Object.keys(meta).length ? meta : undefined
    // Skip optimistic user bubble when the slot is already running — the
    // backend will send a "queued" role message instead, avoiding a duplicate.
    if (!store.getState().chat.slotRunning || forceNew) {
      dispatch(appendMessage({ role: 'user', content: displayTxt, cls: '', ts: new Date().toISOString(), meta: metaPayload }))
    }
    window.dispatchEvent(new Event('voice-stop'))
    sendingRef.current = false
    isAtBottomRef.current = true
    setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    dispatch(setSlotRunning(true))
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 10_000)
    try {
      const r = await api.sendChat(llmTxt, slot ?? undefined, colorThemeRef.current, controller.signal, metaPayload, browsing)
      clearTimeout(timeout)
      const body = await r.json().catch(() => ({}))
      if (!body.queued && !body.ok) {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: body.error || 'Send failed', cls: '' }))
      }
    } catch (e: unknown) {
      clearTimeout(timeout)
      if (e instanceof DOMException && e.name === 'AbortError') {
        // Timeout — message was received, WS will deliver response
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: 'Connection error', cls: '' }))
        // Restore draft so the user doesn't lose their message (Mesh-1468).
        // Also restore the paste blocks backing any tokens in `txt`, otherwise
        // the restored text shows a dead `[ Paste #N · M lines ]` literal.
        // Persist for `slot` unconditionally (recoverable on disk), but only
        // touch the live input/blocks when `slot` is the one on screen. Compare
        // against activeSlotRef.current, NOT the closure's `activeSlot`: a
        // new-session/forceNew send creates a fresh slot and switches the UI to
        // it, so the closure value is stale — using it would leave the user's
        // just-typed message empty on the very session they're now viewing.
        // The ref reflects what's actually on screen, so it restores the text
        // visibly for a new-session failure while still not splicing a targeted
        // send's text into an unrelated slot the user is looking at.
        if (slot) {
          setDraft(drafts.current, slot, txt)
          setPasteDraft(pasteDrafts.current, slot, activePastes)
          saveDrafts()
          if (slot === activeSlotRef.current) { setInput(txt); setPasteBlocks(activePastes) }
        }
      }
    }
  }, [activeSlot, dispatch])

  // Submit inline document comments to the session the file was opened from,
  // not the currently-active one. If the user switched sessions while the
  // panel was open, switch back to the origin session so the prompt + reply
  // land where the document belongs. switchSlot.pending sets activeSlot
  // synchronously, but send()'s closure activeSlot is stale until re-render,
  // so the origin slot is passed to send() explicitly.
  const submitComments = useCallback((message: string) => {
    const target = panel.slot
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    send(message, target ?? undefined)
  }, [panel.slot, activeSlot, dispatch, send])

  // Auto-send when navigated with ?autoSend=1 or ?token= with prompt
  useEffect(() => { if (connected && autoSendRef.current) { const txt = autoSendRef.current; autoSendRef.current = null; send(txt) } }, [send, connected]) // eslint-disable-line react-hooks/exhaustive-deps

  // Widget interactivity: when a mcwidget iframe fires an action, auto-submit as user message
  useEffect(() => {
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail?.text
      if (text && !sendingRef.current) send(text)
    }
    window.addEventListener('mc-widget-send', handler)
    return () => window.removeEventListener('mc-widget-send', handler)
  }, [send])

  const approve = useCallback(async (action: string) => { if (activeSlot) await api.approveChatSlot(activeSlot, action) }, [activeSlot])
  const toApiDecision = (action: string): 'approve' | 'reject' =>
    action === 'approved' || action === 'trust' ? 'approve' : 'reject'
  const dismissApproval = useCallback((aid: string, decision?: string) => {
    dispatch(resolveByApprovalId({ id: aid, decision }))
    const n = store.getState().notifications.items.find(x => x.approval_id === aid)
    if (n) dispatch(removeNotificationByTs(n.ts))
  }, [dispatch])
  const switchAgent = useCallback(async (agentName: string) => {
    if (!activeSlot) {
      setPendingAgent(agentName)
      const mc = installedAgents.find(a => a.name === agentName)
      const templateName = provider.resolveAgentTemplate(mc || { name: agentName })
      queryClient.fetchQuery({ queryKey: ['resolved-model', templateName, provider.id], queryFn: () => provider.resolveModel(templateName) })
        .then(m => setPendingModel(m)).catch(() => setPendingModel(''))
      return
    }
    await api.chatSlotAgent(activeSlot, agentName)
    setAgentDropdown(false)
  }, [activeSlot, installedAgents, provider])
  const switchModel = useCallback(async (modelName: string) => {
    const val = modelName === 'auto' ? '' : modelName
    if (!activeSlot) { setPendingModel(val); return }
    await api.chatSlotModel(activeSlot, val)
    setModelDropdown(false)
  }, [activeSlot])
  const setProject = useCallback(async (path: string) => {
    if (!activeSlot) { setPendingProject(path); return }
    try { await api.chatSlotProject(activeSlot, path) } catch (e) { console.error('setProject failed', e) }
  }, [activeSlot])

  const currentSlot = slots.find(s => s.key === activeSlot)
  const title = currentSlot?.title && currentSlot.title !== currentSlot.key ? currentSlot.title : activeSlot || ''
  const displayMode = approvalMode === 'yolo' ? 'yolo' : currentSlot?.trust ? 'trust' : currentSlot?.trust_reads ? 'trust_reads' : 'normal'
  // Resolve model for existing slots that don't have one stored
  const _slotMc = installedAgents.find(a => a.name === currentSlot?.agent)
  const _slotTemplateName = (currentSlot && !currentSlot.model && installedAgents.length > 0) ? provider.resolveAgentTemplate(_slotMc || { name: currentSlot.agent || 'default' }) : ''
  const { data: _slotResolvedModel } = useQuery({
    queryKey: ['resolved-model', _slotTemplateName, provider.id],
    queryFn: () => provider.resolveModel(_slotTemplateName),
    enabled: !!_slotTemplateName,
  })
  useEffect(() => { setResolvedModel(_slotResolvedModel || '') }, [_slotResolvedModel])
  const [navPanelOpen, setNavPanelOpen] = useState(() => loadChatConfig().navPanelOpen)
  const [sidebarPinned, setSidebarPinned] = useState(() => localStorage.getItem('mc-sidebar-pinned') === 'true')
  const isMobile = useIsMobile()
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('mc-sidebar-width') || '', 10)
    return !isNaN(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX ? v : 260
  })
  const [sidebarDragging, setSidebarDragging] = useState(false)
  const [editingTitle, setEditingTitle] = useState(false)
  const [generatingTitleSlots, setGeneratingTitleSlots] = useState<Set<string>>(new Set())
  const [titleDraft, setTitleDraft] = useState('')
  const lastTextIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return i
    }
    return -1
  }, [messages])
  const [regenerating, setRegenerating] = useState(false)
  useEffect(() => { setRegenerating(false) }, [activeSlot])
  // Clear typing dots as soon as streaming starts
  useEffect(() => {
    if (regenerating && isStreaming) setRegenerating(false)
  }, [regenerating, isStreaming])
  // Safety timeout
  useEffect(() => {
    if (!regenerating) return
    const t = setTimeout(() => { setRegenerating(false) }, 30_000)
    return () => clearTimeout(t)
  }, [regenerating])
  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning) return
    const uIdx = messages.slice(0, lastTextIdx).map(mm => mm.role).lastIndexOf('user')
    if (uIdx < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(uIdx + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((e: unknown) => {
      console.warn('regenerate failed', e)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, regenerating, slotRunning, messages, lastTextIdx, dispatch])

  const [flyingQuote, setFlyingQuote] = useState<{ text: string; from: DOMRect } | null>(null)
  const inputAreaRef = useRef<HTMLDivElement>(null)

  const handleQuote = useCallback((text: string, rect: DOMRect) => {
    const quoted = text.split('\n').map(line => `> ${line}`).join('\n')
    setInput(prev => {
      // Append new quote after existing content (supports multiple quotes)
      if (!prev.trim()) return `${quoted}\n\n`
      return `${prev.trimEnd()}\n\n${quoted}\n\n`
    })
    // Trigger flying animation
    setFlyingQuote({ text, from: rect })
    requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus())
  }, [])

  const handleEditResend = useCallback((index: number, ts: string, newContent: string) => {
    if (!activeSlot || slotRunning) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(index))
    dispatch(appendMessage({ role: 'user', content: newContent, cls: '', ts: new Date().toISOString() }))
    setRegenerating(true)
    // Use /rewind (fork-and-swap) — discards the orphan kiro-cli session so
    // truncated forward turns can't resurface on resume. Mirrors kiro-cli's
    // native /rewind slash command, but swaps the session under the same
    // slot identity so the UI stays in place (no new tab, no title change).
    rewindWithRollback(activeSlot, ts, newContent, () => {
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, slotRunning, messages, dispatch])

  const search = useMessageSearch(messages, activeSlot)

  const searchCtxValue = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const renderUserContentCb = useCallback(
    (c: string, mt: Record<string, unknown> | undefined) => renderUserContent(c, mt, handleFileOpen),
    [handleFileOpen]
  )

  const cancelTitleRef = useRef(false)
  const composingRef = useRef(false)
  useEffect(() => {
    const togglePin = () => {
      if (!chatConfig.sidebarCanHide || filteredSlotsRef.current.length === 0) return
      setSidebarPinned(p => {
        const next = !p
        localStorage.setItem('mc-sidebar-pinned', String(next))
        return next
      })
    }
    window.addEventListener('toggle-pin-chat-sidebar', togglePin)
    return () => window.removeEventListener('toggle-pin-chat-sidebar', togglePin)
  }, [chatConfig.sidebarCanHide])

  const lastRole = messages[messages.length - 1]?.role ?? ''
  // Precompute: index of last finalized assistant message (tools after this are "trailing")
  const toggleAct = useCallback(() => { panel.closePanel(); diffPanel.closeDiff(); dispatch(toggleActivity()) }, [dispatch, panel, diffPanel])
  const displayItems = useMemo<DisplayItem[]>(() => {
    // Phase 1: build raw items (singles + groups)
    const raw: TurnItem[] = []
    let group: ChatMessage[] = [], groupStart = 0
    for (let i = 0; i < messages.length; i++) {
      // Permission messages handled by pinned ApprovalBar — skip entirely
      if (messages[i].role === 'permission') continue
      // Subagent completions are internal — LLM sees them but user doesn't need to
      if (messages[i].role === 'subagent') continue
      if (GROUPABLE.has(messages[i].role)) {
        if (!group.length) groupStart = i
        group.push(messages[i])
      } else {
        if (group.length) { raw.push({ kind: 'group', msgs: group, startIdx: groupStart }); group = [] }
        raw.push({ kind: 'single', msg: messages[i], idx: i })
      }
    }
    if (group.length) raw.push({ kind: 'group', msgs: group, startIdx: groupStart })

    // Phase 2: group into turns (user message → next user message)
    const turns: DisplayItem[] = []
    let turnItems: TurnItem[] = []
    const hasWorkingSteps = (items: TurnItem[]) =>
      items.some(t =>
        (t.kind === 'single' && (t.msg.role === 'tool' || t.msg.role === 'assistant' || t.msg.role === 'streaming')) ||
        t.kind === 'group'
      )
    const flushTurn = (items: TurnItem[], complete: boolean) => {
      if (hasWorkingSteps(items) && items.length > 2) {
        turns.push({ kind: 'turn', items, complete })
      } else {
        turns.push(...items)
      }
    }
    for (const item of raw) {
      if (item.kind === 'single' && item.msg.role === 'user') {
        if (turnItems.length > 0) { flushTurn(turnItems, true); turnItems = [] }
        turns.push(item)
      } else {
        turnItems.push(item)
      }
    }
    if (turnItems.length > 0) flushTurn(turnItems, !slotRunning)
    return turns
  }, [messages, slotRunning])

  // Keep the ref in sync so handleRangeChanged / scrollToPrevUserMessage
  // read the latest displayItems. useEffect rather than render-body
  // mutation keeps us in line with React's rules of render (no side
  // effects) — the one-tick lag is irrelevant because callbacks fire
  // after commit, by which point the ref has caught up.
  useEffect(() => { displayItemsRef.current = displayItems }, [displayItems])

  // Virtualized display — only mounts items in the viewport window. The
  // virtualizer shares `scrollerRef` with useScrollManager so the legacy
  // scroll APIs (scrollToDisplayIndex, scrollToBottom) operate on the
  // same DOM element. Its own follow-output handles streaming auto-pin
  // and append-pin, so the legacy useStreamingScroll/useFollowOutput
  // calls below are no-ops in this configuration but are kept invoked
  // for hook-call stability.
  const virtualKey = useCallback((it: DisplayItem, _i: number) => {
    if (it.kind === 'turn') {
      const first = it.items[0]
      if (!first) return `turn-empty-${_i}`
      return first.kind === 'single'
        ? `turn-${first.msg.ts || first.idx}`
        : `turn-g-${first.startIdx}`
    }
    return it.kind === 'single' ? `s-${it.msg.ts || it.idx}` : `g-${it.startIdx}`
  }, [])

  // (Sticky widget detection removed — widgets now unmount with the
  // window like any other item. See useVirtualChat call below for the
  // memory-vs-flicker trade-off rationale.)

  const virt = useVirtualChat<DisplayItem>({
    items: displayItems,
    getKey: virtualKey,
    sessionId: activeSlot ?? '__no_slot__',
    estimatedHeight: 100,
    // Overscan tradeoff (experimental):
    //   smaller (3)   → least memory, frequent widget remounts on small scrolls
    //   medium  (12)  → screenful of buffer, ~290MB baseline / 450MB while scrolling
    //   larger  (25)  → fewer remounts but inflated RAM from warm iframe pool
    // Currently testing 6 — middle ground between memory and remount frequency.
    overscan: 6,
    // No isSticky: widget messages unmount along with everything else
    // when they leave the viewport window. Trade-off: scrolling back to
    // an old widget causes its iframe to reload (1-2 frames of flicker).
    // Memory benefit: only widgets in the active window are kept alive,
    // ~290MB baseline instead of 500MB+ with all-widgets-sticky.
    externalScrollerRef: scrollerRef,
  })

  // Single scroll controller wiring: expose the virtualizer's follow API to
  // the early effects/handlers (declared above) via refs, and derive the
  // at-bottom state for the jump-to-bottom pill. The virtualizer owns slot
  // entry, streaming follow, and append-pin; ChatPage only triggers explicit
  // jumps (send, jump-to-latest pill) through these.
  const isAtBottom = virt.isAtBottom
  // Mirror the virtualizer's follow API into the refs the early effects/handlers
  // (declared above) read. Done in a layout effect rather than the render body
  // so a concurrent render React throws away can't write stale callbacks into
  // the refs. Layout effects run before passive effects, so the gating effect
  // that reads isAtBottomRef.current still sees this commit's value.
  useLayoutEffect(() => {
    isAtBottomRef.current = isAtBottom
    vScrollToBottomRef.current = virt.scrollToBottom
    mountIndexRef.current = virt.mountIndex
    scrollToIndexSmoothRef.current = virt.scrollToIndexSmooth
  })

  // Legacy aliases so the JSX below keeps reading the same names.
  const visibleDisplayItems = virt.virtualItems
  // No "load more" pagination indicator with virtualization — the
  // windowing engine swaps mounted/placeholder automatically.

  // Reset scroll-navigation state on slot switch.
  useEffect(() => {
    setHasUserMsgAbove(false)
    topmostIdxRef.current = 0
  }, [activeSlot])

  const queuedMessages = useMemo(() => messages.filter(m => m.role === 'queued'), [messages])

  const handleCancelQueued = useCallback((queueId: string) => {
    if (!activeSlot) return
    const msg = messagesRef.current.find(m => m.role === 'queued' && (m.meta?.queueId as string) === queueId)
    if (msg?.content) setInput(msg.content)
    // Optimistically remove the card; WS event is a no-op if already gone
    dispatch(cancelQueuedMessage({ slot: activeSlot, queue_id: queueId }))
    api.cancelQueuedMessage(activeSlot, queueId).catch(() => {})
  }, [activeSlot, dispatch])

  const handleInterruptQueued = useCallback((queueId: string) => {
    if (!activeSlot) return
    api.interruptSlot(activeSlot, queueId).catch(() => {})
  }, [activeSlot])

  // Search: map message index → displayItems index for scroll-to-match
  const messageToDisplayIdx = useMemo(() => {
    const map = new Map<number, number>()
    displayItems.forEach((item, di) => {
      if (item.kind === 'turn') {
        for (const ti of item.items) {
          if (ti.kind === 'single') map.set(ti.idx, di)
          else if (ti.kind === 'group') ti.msgs.forEach((_, mi) => map.set(ti.startIdx + mi, di))
        }
      } else if (item.kind === 'single') map.set(item.idx, di)
      else if (item.kind === 'group') item.msgs.forEach((_, mi) => map.set(item.startIdx + mi, di))
    })
    return map
  }, [displayItems])

  const chatNav = useChatNavigation(messages, messageToDisplayIdx)

  const scrollToNavSection = useCallback((displayIdx: number) => {
    navToDisplayIndex(displayIdx, { behavior: 'smooth', align: 'start', offset: -72 })
  }, [navToDisplayIndex])

  useEffect(() => {
    if (search.currentMessageIdx < 0) return
    const di = messageToDisplayIdx.get(search.currentMessageIdx)
    if (di !== undefined) {
      navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
    }
  }, [search.currentMessageIdx, search.currentIdx, messageToDisplayIdx, navToDisplayIndex])

  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const msgIdx = messages.findIndex(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToDisplayIndex])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear initialMsgRef after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    if (!targetTs || messages.length === 0) return
    const msgIdx = messages.findIndex(m => m.ts === targetTs)
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    initialMsgRef.current = null
    setTimeout(() => {
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setHighlightTs(targetTs)
      setTimeout(() => setHighlightTs(null), 3000)
    }, 500)
  }, [messages, messageToDisplayIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  // Precomputed O(n) map from message index → visible (user/assistant) index,
  // used by the fork button. Replaces per-row O(i) filter that made the
  // renderer O(n²) overall.
  const visibleIndexMap = useMemo(() => {
    const map = new Map<number, number>()
    let count = 0
    for (let idx = 0; idx < messages.length; idx++) {
      const r = messages[idx].role
      if (r === 'user' || r === 'assistant') {
        map.set(idx, count)
        count++
      }
    }
    return map
  }, [messages])

  const activeSlotTitle = filteredSlots.find(s => s.key === activeSlot)?.title

  const renderMessage = useCallback((i: number, m: ChatMessage) => {
    const key = m.ts ? `${m.role}-${m.ts}` : `${m.role}-${i}`
    if (m.role === 'thinking') return null
    if (m.role === 'tool') {
      // Skip ✅/🚫 completion messages — completion shown via CircleCheckBig icon
      if (!m.content.startsWith('🔧')) return null
      // Animate tools in the trailing group (after last assistant/streaming text)
      const isInTrailingGroup = slotState === 'tool_running' && i > lastTextIdx
      return <ToolCallLine key={key} message={m} running={isInTrailingGroup} />
    }
    if (m.role === 'file') {
      try {
        const f = JSON.parse(m.content)
        return <FileCard key={key} file={f} />
      } catch { /* fall through to default */ }
    }
    if (m.role === 'queued') return null
    if (m.kind === 'stop_event' || m.meta?.kind === 'stop_event') return <StopEventCard key={m.meta?.id as string ?? key} message={m} />
    if (m.role === 'error') return <div key={key} className="bg-danger-subtle text-danger text-[13px] px-3 py-2 rounded-md border border-danger/15 self-center animate-scale-in">{m.content}</div>
    if (m.role === 'permission') return null
    if (m.role === 'mcp_oauth') {
      const banner = renderMcpOAuthMessage(m)
      return banner ? <div key={key}>{banner}</div> : null
    }
    const isUser = m.role === 'user'
    const isStreaming = m.role === 'streaming'
    const isInject = m.role === 'inject'
    // Pass a stable handleFork (useCallback) + primitive index so memo()
    // on AssistantMessage can short-circuit when only unrelated state changes
    // (raised in review). visibleIndexMap is O(1) per row.
    const canFork = !isStreaming && !isInject && !slotHasMore
    const forkIndex = canFork ? visibleIndexMap.get(i) : undefined
    const msgTime = m.ts ? new Date(m.ts).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''
    return (
      <MessageSearchScope key={key} messageIdx={i}>
      <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''} ${m.ts && m.ts === highlightTs ? 'animate-msg-highlight rounded-lg' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden ${isUser ? 'items-end' : ''}`}>
          {isUser ? (
            <UserMessage
              content={m.content}
              meta={m.meta}
              timestamp={chatConfig.showTimestamps ? msgTime : undefined}
              renderContent={renderUserContentCb}
              canEdit={!slotRunning && !regenerating && !!activeSlot}
              messageIndex={i}
              messageTs={m.ts || ''}
              onEditResend={handleEditResend}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
            />
          ) : isInject ? (
            (() => {
              const cronLabel = (m.meta?.cronLabel as string) || ''
              // Strip wrapper tags — LLM needs them for context but user sees clean content
              const cleanContent = cronLabel
                ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
                : m.content
              return <>
                {cronLabel && <span className="text-muted text-[11px] font-medium px-1 mb-0.5"><Clock className="lucide-inline" /> {cronLabel}</span>}
                <div className="msg-content px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap rounded-lg bg-warning-subtle text-fg border border-warning/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}><MarkdownRenderer content={cleanContent} /></div>
                {chatConfig.showTimestamps && msgTime && <span className="text-muted text-[12px] font-mono px-1">{msgTime}</span>}
              </>
            })()
          ) : (
            <div className="flex flex-col gap-0">
              <AssistantMessage content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdx} onFileOpen={handleFileOpen} onQuote={handleQuote} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} showFooter={(() => {
                // Show footer on the last assistant message of each completed turn
                if (isStreaming) return false
                // Find next message after this one that's assistant, user, or streaming
                for (let j = i + 1; j < messages.length; j++) {
                  if (messages[j].role === 'user') return true // end of turn — show footer
                  if (messages[j].role === 'assistant' || messages[j].role === 'streaming') return false // not last assistant in turn
                }
                // End of messages — show footer only if agent is done
                return !slotRunning
              })()} onSpeak={() => { const playing = store.getState().chat.voicePlaying; if (playing) { window.dispatchEvent(new Event('voice-stop')); dispatch(setVoiceAudio(null)); return }; dispatch(setVoiceAudio(null)); api.voiceSynthesize(activeSlot || '', m.content).catch(() => {}) }} onRegenerate={i === lastTextIdx && !slotRunning && !regenerating && activeSlot ? handleRegenerate : undefined} variants={m.variants} variantIdx={m.variant_idx} onSwitchVariant={i === lastTextIdx && m.variants && m.variants.length > 1 && activeSlot ? (idx: number) => { api.switchVariant(activeSlot, idx).catch((e: unknown) => console.warn('switch-variant failed', e)) } : undefined} onFork={handleFork} onPlanFromHere={handlePlanFromHere} forkIndex={forkIndex} onApplyPlan={async (steps: any[]) => {
                try {
                  const r = await api.planFromChat(steps, planTaskId)
                  if (r.ok) { navigate('/projects?applied=' + (r.task_id || planTaskId)); return true }
                } catch { /* API error */ }
                alert('Failed to apply plan')
                return false
              }} />
            </div>
          )}
        </div>
      </div>
      </MessageSearchScope>
    )
  }, [messages, visibleIndexMap, pendingApproval, slotRunning, slotState, lastTextIdx, approve, send, handleFileOpen, handleFork, handleQuote, chatConfig, avatar, botName, activeSlot, dismissApproval, regenerating, handleRegenerate, handleEditResend, slotHasMore, renderUserContentCb, highlightTs, activeSlotTitle, mode])

  const [mobileSessions, setMobileSessions] = useState(false)
  // Close mobile sessions panel when a session is selected
  useEffect(() => { if (isMobile) setMobileSessions(false) }, [activeSlot]) // eslint-disable-line react-hooks/exhaustive-deps
  // Reset mobile sessions state when leaving mobile viewport
  useEffect(() => { if (!isMobile) setMobileSessions(false) }, [isMobile])
  // Swipe from left edge to open sidebar, swipe left on backdrop to close
  const chatContainerRef = useRef<HTMLDivElement>(null)
  const openSidebar = useCallback(() => setMobileSessions(true), [])
  const closeSidebar = useCallback(() => setMobileSessions(false), [])
  useSwipeEdge(chatContainerRef, { enabled: isMobile && !mobileSessions, edge: 'left', edgeZone: 0.35, onSwipe: openSidebar })
  useSwipeEdge(chatContainerRef, { enabled: isMobile && mobileSessions, edge: 'right', threshold: 50, edgeZone: 9999, onSwipe: closeSidebar })
  const sidebarOpen = isMobile ? mobileSessions : (!chatConfig.sidebarCanHide || sidebarPinned || filteredSlots.length === 0)
  useEffect(() => {
    if (chatConfig.sidebarCanHide && filteredSlots.length === 0 && !sidebarPinned) {
      setSidebarPinned(true)
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }
  }, [chatConfig.sidebarCanHide, filteredSlots.length]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div ref={chatContainerRef} className="flex flex-1 min-h-0 h-full overflow-hidden relative">
      <AnimatePresence>
        {isMobile && mobileSessions && (
          <motion.div
            key="sessions-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-sm"
            onClick={() => setMobileSessions(false)}
          />
        )}
      </AnimatePresence>
      {embedMode === 'chat' ? null : embedMode === 'sessions' ? (
        <div className="flex-1 min-w-0 h-full overflow-hidden [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!border-0 [&_.sidebar-inner]:!rounded-none [&_.sidebar-inner]:!shrink [&_.sidebar-inner]:!bg-bg [&_.sidebar-resize-handle]:!hidden">
          <ChatSidebar
            slots={filteredSlots}
            activeSlot={null}
            unreadSlots={surfaceUnreadSlots}
            history={history}
            historyHasMore={historyHasMore}
            defaultAgent={defaultAgent}
            installedAgents={installedAgents}
            mode={mode}
            onWidthChange={setSidebarWidth}
            onDragChange={setSidebarDragging}
            onSelectSlot={(key) => navigate(`/embed/chat/${key}`)}
          />
        </div>
      ) : (
      <OverlayDrawer open={sidebarOpen} width={sidebarWidth} dragging={sidebarDragging} className={isMobile ? 'mobile-sessions-overlay fixed top-[52px] bottom-0 left-0 z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg max-w-[calc(100vw-2.5rem)] [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0' : ''}>
        <ChatSidebar
          slots={filteredSlots}
          activeSlot={activeSlot}
          unreadSlots={surfaceUnreadSlots}
          history={history}
          historyHasMore={historyHasMore}
          defaultAgent={defaultAgent}
          installedAgents={installedAgents}
          mode={mode}
          onWidthChange={setSidebarWidth}
          onDragChange={setSidebarDragging}
        />
      </OverlayDrawer>
      )}

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div className={`relative flex flex-col bg-bg min-w-0 min-h-0 h-full overflow-hidden ${panel.isOpen || activityOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: '-0.5rem' } : {}), '--mc-content-width': CONTENT_WIDTH[chatConfig.contentWidth].messages, '--mc-input-width': CONTENT_WIDTH[chatConfig.contentWidth].input } as React.CSSProperties}>
        {snipFrame && (
          <SnipOverlay
            frame={snipFrame}
            onComplete={f => { uploadFiles([f]); setSnipFrame(null) }}
            onCancel={() => setSnipFrame(null)}
            onError={setUploadError}
          />
        )}
        {uploadError && (
          <div className="mx-4 mt-2 mb-0 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center gap-3 animate-rise">
            <span className="text-sm text-danger flex-1">{uploadError}</span>
            <button onClick={() => setUploadError('')} className="text-danger/60 hover:text-danger text-lg leading-none">&times;</button>
          </div>
        )}
        {uploadNotice && (
          <div className="mx-4 mt-2 mb-0 bg-info/10 border border-info/20 rounded-lg p-3 flex items-center gap-3 animate-rise">
            <span className="text-sm text-info flex-1">{uploadNotice}</span>
            <button onClick={() => setUploadNotice('')} className="text-info/60 hover:text-info text-lg leading-none">&times;</button>
          </div>
        )}
        {sidError && (
          <div className="mx-4 mt-2 mb-0 bg-warn/10 border border-warn/20 rounded-lg p-3 flex items-center gap-3 animate-rise">
            <span className="text-sm text-warn flex-1">{sidError}</span>
            <button onClick={() => setSidError('')} className="text-warn/60 hover:text-warn text-lg leading-none">&times;</button>
          </div>
        )}
        {isMobile && !sidebarOpen && !(activeSlot && (messages.length > 0 || slotRunning)) && (
          <div className="fixed top-[52px] left-2 z-10">
            <button className="p-2 rounded-lg text-muted hover:text-text bg-bg-elevated border border-border shadow-sm cursor-pointer" onClick={() => setMobileSessions(true)} aria-label="Toggle sessions">
              {mode === 'orchestrator' ? <MessageSquareDot size={18} /> : <MessageSquare size={18} />}
            </button>
          </div>
        )}
        {!activeSlot ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-4 px-8">
            <EmptyState icon={<MessageSquare className="lucide-inline" />} title="What can I do for you?" subtitle="Start a new chat to begin" />
            <Btn primary onClick={() => dispatch(createSlot({ agent: pendingAgent || defaultAgent || undefined, model: pendingModel || undefined, mode }))}>Start a new chat</Btn>
          </div>
        ) : (
          <SearchHighlightContext.Provider value={searchCtxValue}>
          <div className="relative flex flex-col flex-1 min-h-0">
            {search.isOpen && <SearchBar term={search.term} setTerm={search.setTerm} matches={search.matches} currentIdx={search.currentIdx} next={search.next} prev={search.prev} close={search.close} caseSensitive={search.caseSensitive} toggleCaseSensitive={search.toggleCaseSensitive} />}
            <AnimatePresence>
              {navPanelOpen && <ChatNavPanel links={chatNav.links} sections={chatNav.sections} onScrollToSection={scrollToNavSection} onClose={() => setNavPanelOpen(false)} searchOpen={search.isOpen} resolving={chatNav.resolving} />}
            </AnimatePresence>
            {/* Claude-style title row — absolute overlay, solid top fading to transparent.
                Inset on the right by the 6px scrollbar width (see ::-webkit-scrollbar
                in index.css) so the overlay never paints over the scroller's scrollbar
                track — otherwise the thumb is hidden/un-grabbable when scrolled to top. */}
            <div className="absolute top-0 left-0 right-1.5 z-10 pointer-events-none" style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
              <div className="px-5 pt-3 pb-2 flex items-center gap-2 bg-bg pointer-events-none">
                {embedMode !== 'chat' && isMobile && (
                  <button className="p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto" onClick={() => setMobileSessions(p => !p)} aria-label="Toggle sessions">
                    {mode === 'orchestrator' ? <MessageSquareDot size={16} /> : <MessageSquare size={16} />}
                  </button>
                )}
                <div className="group/header flex items-stretch gap-0.5 pointer-events-auto">
                <div className="rounded-l-md rounded-r-[2px] px-1.5 py-0.5 group-hover/header:bg-bg-hover transition-colors">
                <ChatHeaderMenu
                  activeSlot={activeSlot}
                  currentSlot={currentSlot}
                  slackChannels={slackChannels}
                  onSlackLink={handleSlackLink}
                  slotKey={activeSlot ?? undefined}
                  colorIndex={slots.find(s => s.key === activeSlot)?.color_index}
                  agent={currentSlot?.agent}
                  onReveal={activeSlot ? () => { if (!sidebarPinned) setSidebarPinned(true); window.dispatchEvent(new CustomEvent('reveal-slot', { detail: activeSlot })) } : undefined}
                  mode={mode}
                />
                </div>
              {editingTitle ? (
                <Input className="text-sm font-semibold font-mono bg-transparent border border-accent rounded px-1 py-0 text-text-strong outline-none min-w-[120px]" autoFocus value={titleDraft} onChange={e => setTitleDraft(e.target.value)} onBlur={() => { if (!cancelTitleRef.current && titleDraft.trim() && activeSlot && titleDraft !== title) { dispatch(sseSlotTitle({ key: activeSlot, title: titleDraft.trim() })); api.renameSlot(activeSlot, titleDraft.trim()).catch(() => {}) } cancelTitleRef.current = false; setEditingTitle(false) }} onCompositionStart={() => { composingRef.current = true }} onCompositionEnd={() => { composingRef.current = true; setTimeout(() => { composingRef.current = false }, 50) }} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing && !composingRef.current) (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') { cancelTitleRef.current = true; setEditingTitle(false) } }} />
              ) : (
                <div className="cursor-text flex items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover transition-colors">
                  <Clickable className="flex items-center gap-1" onClick={() => { if (activeSlot && generatingTitleSlots.has(activeSlot)) return; setEditingTitle(true); setTitleDraft(title) }}>
                    {currentSlot?.memory_mode === 'incognito' && <span title="Incognito — memory writes disabled"><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                    {currentSlot?.memory_mode === 'temporary' && <span title="Temporary — no memory reads or writes"><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                    <TypewriterText text={title} className="text-sm font-semibold text-muted font-mono truncate max-w-[40vw]" />
                    <Pen size={13} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-60 transition-opacity" />
                  </Clickable>
                  {activeSlot && (generatingTitleSlots.has(activeSlot) ? <Loader size={16} className="shrink-0 text-accent animate-spin" /> : <Btn aria-label="Regenerate title with LLM" className="shrink-0 text-muted opacity-0 group-hover/header:opacity-40 hover:!opacity-100 hover:text-accent transition-all cursor-pointer bg-transparent border-none p-0" title="Regenerate title with LLM" onClick={e => { e.stopPropagation(); if (!activeSlot || generatingTitleSlots.has(activeSlot)) return; const slot = activeSlot; setGeneratingTitleSlots(prev => new Set(prev).add(slot)); api.generateTitle(slot).then(r => { /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */ if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title })) }).catch(e => console.warn('Failed to generate title:', e)).finally(() => setGeneratingTitleSlots(prev => { const next = new Set(prev); next.delete(slot); return next })) }}><Sparkles size={16} /></Btn>)}
                </div>
              )}
                </div>
              {mode === 'orchestrator' && <span className="pointer-events-auto"><InfoTip text="Autopilot plans before executing. Each stage needs your approval (or select 'Go All' to run autonomously). Sub-agents are delegated automatically. Plan lessons persist across sessions." /></span>}
              <Clickable className="ml-auto opacity-40 hover:opacity-100 transition-opacity cursor-pointer pointer-events-auto" onClick={() => setNavPanelOpen(p => !p)} title="Chat navigation" aria-label="Toggle chat navigation">
                <ListTree size={14} />
              </Clickable>
              {embedMode !== 'chat' && !activityOpen && <Clickable className="opacity-40 hover:opacity-100 transition-opacity cursor-pointer pointer-events-auto" onClick={toggleAct} aria-label="Toggle activity panel">
                <SessionStatus />
              </Clickable>}
              </div>
              <div className="h-6 bg-gradient-to-b from-bg to-transparent" />
            </div>
            {!isAtBottom && hasUserMsgAbove && (
              <div className="absolute top-16 inset-x-0 z-10 pointer-events-none flex justify-center">
                <button
                  type="button"
                  className="w-8 h-8 rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 text-text/85 hover:text-text hover:scale-[1.06] active:scale-95 active:duration-75"
                  style={{
                    background: 'linear-gradient(145deg, rgba(255,255,255,0.24), rgba(255,255,255,0.06)), rgba(0,0,0,0.06)',
                    backdropFilter: 'blur(24px) saturate(180%)',
                    WebkitBackdropFilter: 'blur(24px) saturate(180%)',
                    boxShadow: [
                      'inset 0 1px 1.5px rgba(255,255,255,0.45)',
                      'inset 0 -1px 1.5px rgba(0,0,0,0.18)',
                      'inset 1px 0 1px rgba(255,255,255,0.12)',
                      'inset -1px 0 1px rgba(0,0,0,0.08)',
                      '0 8px 24px -6px rgba(0,0,0,0.28)',
                      '0 2px 6px rgba(0,0,0,0.12)',
                    ].join(', '),
                  }}
                  onClick={scrollToPrevUserMessage}
                  aria-label="Scroll to previous user message"
                  title="Scroll to previous user message"
                ><ArrowUp size={14} strokeWidth={2.5} /></button>
              </div>
            )}
            {slotLoading && (
              <div className="absolute inset-0 flex items-center justify-center z-20 pointer-events-none">
                <Loader size={20} className="animate-spin text-muted" />
              </div>
            )}
            {isWelcomeState ? (
              <motion.div
                key="welcome-hero"
                layout
                className="flex-1 flex flex-col items-center justify-center gap-6 px-8 min-h-0 overflow-y-auto"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              >
                <WelcomeView
                  mode={mode}
                  setInput={setInput}
                  memoryMode={currentSlot?.memory_mode ?? 'persistent'}
                  cleanMode={currentSlot?.clean_mode}
                  onSwitchMode={async (newMode) => {
                    if (!activeSlot) return
                    // Create-first-then-delete: deleting the active slot first
                    // would make deleteSlot jump focus to a sibling. Creating
                    // first keeps the new slot active, so the delete skips the
                    // sibling navigation. Carry agent/project/folder/color so
                    // the recreated slot keeps its identity and placement.
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      memory_mode: newMode,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                  onToggleClean={async (clean) => {
                    if (!activeSlot) return
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      clean_mode: clean,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                />
              </motion.div>
            ) : (
            <div
              ref={scrollerRef}
              style={{
                flex: 1,
                paddingBottom: 8,
                overflowY: 'auto',
                // Reserve a stable scrollbar gutter so the 6px scrollbar always
                // occupies the same right-edge column the title overlay is inset
                // from (see the right-1.5 inset above) — keeps the thumb visible
                // and grabbable at the top instead of hidden behind the header.
                scrollbarGutter: 'stable',
                // Native scroll anchoring: when items above the viewport
                // resize (e.g. widget iframes loading async), the browser
                // adjusts scrollTop to keep the user's content stable.
                // This is more precise than item-level anchoring because
                // it works at the DOM-element granularity.
                overflowAnchor: 'auto',
              } as React.CSSProperties}
              aria-label="Chat messages"
              aria-live="polite"
              onScroll={onScrollTopmost}
            >
              {/* Header spacer */}
              <div className="h-16" />
              {/* Top sentinel: drives upward window expansion via virtualizer's IO. */}
              <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* Top spacer — reserves the height of all items above the mounted
                  window so the scrollbar stays accurate while only the window
                  renders real DOM (keeps fast scroll cheap — O(window) nodes).
                  overflow-anchor:none so the browser anchors on real content,
                  not on this spacer (which resizes as the window moves). */}
              <div aria-hidden style={{ height: virt.offsetBefore, overflowAnchor: 'none' }} />
              {/* Message items — only the mounted window renders; everything
                  else is represented by the top/bottom spacers. */}
              {visibleDisplayItems.map((vi) => {
                if (!vi.mounted) return null
                const item = vi.data
                const displayIdx = vi.index
                if (item.kind === 'turn') {
                  const renderTurnItem = (it: TurnItem, _j: number) => {
                    // Skip hidden tool messages (✅/🚫 completions) to avoid empty py-1 wrappers
                    if (it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')) return null
                    return <div key={it.kind === 'single' ? (it.msg.ts || it.idx) : `g-${it.startIdx}`} className={`px-5 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                      {it.kind === 'group' ? (() => {
                        const unresolvedPerms = it.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                        // Skip group entirely if it only contains unresolved permissions (handled by ApprovalBar)
                        if (it.msgs.every(m => m.role === 'permission')) return null
                        return (
                        <CollapsibleToolGroup
                          count={it.msgs.filter(m => m.role !== 'permission').length}
                          hasPermission={false}
                          isRunning={false}
                          permissionMeta={unresolvedPerms.at(-1)?.meta as Record<string, unknown> | undefined}
                          pendingPermCount={unresolvedPerms.length}
                          onApprove={(() => {
                            const aid = unresolvedPerms.at(-1)?.meta?.approval_id as string | undefined
                            if (!aid) return approve
                            return async (action: string) => { await api.resolveApproval(aid, toApiDecision(action)); dismissApproval(aid) }
                          })()}
                          onViewActivity={toggleAct}
                          activityOpen={activityOpen}
                        >{it.msgs.map((m, j) => <div key={m.ts || j}>{renderMessage(it.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
                      })() : renderMessage(it.idx, it.msg)}
                    </div>
                  }
                  return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx}><TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} /></div>
                }
                return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-5 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>{item.kind === 'group' ? (() => {
                const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                if (item.msgs.every(m => m.role === 'permission')) return null
                return (
                <CollapsibleToolGroup
                  count={item.msgs.filter(m => m.role !== 'permission').length}
                  hasPermission={false}
                  isRunning={slotRunning && displayIdx === displayItems.length - 1}
                  permissionMeta={unresolvedGroupPerms.at(-1)?.meta as Record<string, unknown> | undefined}
                  pendingPermCount={unresolvedGroupPerms.length}
                  onApprove={(() => {
                    const aid = unresolvedGroupPerms.at(-1)?.meta?.approval_id as string | undefined
                    if (!aid) return approve
                    return async (action: string) => {
                      await api.resolveApproval(aid, toApiDecision(action))
                      dismissApproval(aid)
                    }
                  })()}
                  onViewActivity={toggleAct}
                  activityOpen={activityOpen}
                >{item.msgs.map((m, j) => <div key={m.ts || j}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
              })() : renderMessage(item.idx, item.msg)}</div>
              })}
              {/* Bottom spacer — reserves the height of all items below the
                  mounted window. overflow-anchor:none (see top spacer). */}
              <div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />
              {/* Bottom sentinel: drives downward window expansion when in jump mode. */}
              <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* Footer */}
              <ChatFooter running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} regenerating={regenerating} stopState={currentSlot?.stop_state} />
              <div style={{height: '2vh'}} />
            </div>
            )}
            <div className="h-6 bg-gradient-to-t from-bg to-transparent pointer-events-none -mt-6 relative z-[1]" />
            <div className="relative">
              {!isAtBottom && messages.length > 0 && (
                <div className="absolute -top-10 inset-x-0 z-10 pointer-events-none flex justify-center">
                  <button
                    className="w-8 h-8 rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 text-text/85 hover:text-text hover:scale-[1.06] active:scale-95 active:duration-75"
                    style={{
                      background: 'linear-gradient(145deg, rgba(255,255,255,0.24), rgba(255,255,255,0.06)), rgba(0,0,0,0.06)',
                      backdropFilter: 'blur(24px) saturate(180%)',
                      WebkitBackdropFilter: 'blur(24px) saturate(180%)',
                      boxShadow: [
                        'inset 0 1px 1.5px rgba(255,255,255,0.45)',  // top highlight
                        'inset 0 -1px 1.5px rgba(0,0,0,0.18)',        // bottom shadow
                        'inset 1px 0 1px rgba(255,255,255,0.12)',     // left edge
                        'inset -1px 0 1px rgba(0,0,0,0.08)',          // right edge
                        '0 8px 24px -6px rgba(0,0,0,0.28)',           // ambient lift
                        '0 2px 6px rgba(0,0,0,0.12)',                 // close shadow
                      ].join(', '),
                    }}
                    onClick={() => { isAtBottomRef.current = true; scrollBottom(true) }}
                    aria-label="Scroll to bottom"
                  ><ArrowDown size={14} strokeWidth={2.5} /></button>
                </div>
              )}
              {!activityOpen && <SubagentProgressBar slot={activeSlot} />}
              <QueueStack messages={queuedMessages} onCancel={handleCancelQueued} onInterrupt={handleInterruptQueued} />
              {flyingQuote && <FlyingQuote text={flyingQuote.text} from={flyingQuote.from} targetRef={inputAreaRef} onComplete={() => setFlyingQuote(null)} />}
              <div ref={inputAreaRef} className="relative z-10">
              {showHistorySuggestions && (
                <div className="absolute left-0 right-0 bottom-full mb-1 mx-auto w-full max-w-[760px] border border-border rounded-lg bg-card overflow-hidden animate-scale-in z-50 shadow-lg flex flex-col max-h-[min(300px,40vh)]">
                  <div className="px-3.5 py-2.5 border-b border-border shrink-0">
                    <span className="text-[12px] font-semibold text-muted tracking-[.02em]">Continue a previous chat?</span>
                  </div>
                  <div className="overflow-y-auto flex-1 min-h-0" role="listbox" aria-label="Previous chats">
                    {historySuggestions.map((s) => (
                      <div
                        key={s.key}
                        role="option"
                        tabIndex={0}
                        aria-selected={false}
                        className="w-full text-left px-3.5 py-2.5 flex items-center gap-3 cursor-pointer transition-all border-b border-border last:border-0 hover:bg-bg-hover"
                        onMouseDown={(e) => { e.preventDefault(); handleResumeSession(s.key, s.title || s.key) }}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleResumeSession(s.key, s.title || s.key) }}
                      >
                        <div className="flex-1 min-w-0">
                          <div className="font-mono text-[13px] text-text truncate">{s.title || s.key}</div>
                          {s.created && <div className="text-[11px] text-muted font-mono mt-0.5">{new Date(s.created).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}</div>}
                        </div>
                        <Undo2 size={14} className="text-accent shrink-0" />
                      </div>
                    ))}
                  </div>
                  <div className="px-3.5 py-2 border-t border-border flex justify-end shrink-0">
                    <span className="text-[11px] text-muted-strong">Esc to dismiss</span>
                  </div>
                </div>
              )}
              {knowledgeFetch.results.length > 0 || knowledgeFetch.loading ? (
                <KnowledgePicker
                  results={knowledgeFetch.results}
                  query={knowledgeFetch.query}
                  loading={knowledgeFetch.loading}
                  onInject={(selected) => {
                    knowledgeFetch.inject(selected)
                  }}
                  onSkip={() => knowledgeFetch.clearResults()}
                />
              ) : null}
              {pendingQuestion && pendingQuestion.slot === activeSlot && (
                <div className="px-5 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <QuestionCard
                    questions={pendingQuestion.questions}
                    onSubmit={(answers) => {
                      const text = Object.values(answers).join('\n')
                      if (text.trim()) {
                        send(text)
                      }
                      dispatch(clearQuestionCard())
                    }}
                  />
                </div>
              )}
              <ChatInput
              value={input}
              onChange={setInput}
              onSend={() => send()}
              onFollowUpSend={(text?: string) => send(text)}
              disabled={
                /* Streaming, compaction (Mesh-1345), and stopping (Mesh-2004) all
                   keep the input interactive: api_chat queues on slot.running and
                   stop preserves the queue (Mesh-1889), so typing + Enter queues a
                   follow-up during the stop window instead of being silently blocked. */
                false
              }
              autoFocusKey={activeSlot}
              prefillHint={prefillHint}
              onDismissHint={() => setPrefillHint(false)}
              onScreenshot={handleCapture}
              onUploadFiles={uploadFiles}
              uploading={uploading}
              pendingFiles={pendingFiles}
              onRemoveFile={p => setPendingFiles(prev => prev.filter(x => x !== p))}
              onFileSelect={path => setPendingFiles(prev => prev.includes(path) ? prev : [...prev, path])}
              onFileOpen={handleFileOpen}
              project={currentSlot?.project || ''}
              isMac={isMac}
              onDrop={handleDrop}
              dragOver={dragOver}
              onDragOver={e => { e.preventDefault(); e.stopPropagation(); setDragOver(true) }}
              onDragLeave={e => { if (e.currentTarget === e.target) setDragOver(false) }}
              voiceRecording={voice.recording}
              voiceTranscribing={voice.transcribing}
              onVoiceToggle={voiceInputSupported ? toggleVoice : undefined}
              agentName={currentSlot?.agent || 'default'}
              agentSource={installedAgents.find(a => a.name === (currentSlot?.agent || 'default'))?.source}
              modelName={currentSlot?.model || resolvedModel || 'auto'}
              onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); setAgentDropdown(!agentDropdown) } : undefined}
              onModelClick={(rect) => { setModelBtnRect(rect); setModelDropdown(!modelDropdown) }}
              onProjectClick={(rect) => {
                setProjectBtnRect(rect)
                setProjectPickerOpen(o => !o)
              }}
              contextPct={contextPct}
              contextUsedTokens={contextTokens?.used}
              contextWindowTokens={contextTokens?.window}
              isRunning={slotRunning}
              onStop={() => {
                const slot = activeSlot
                if (!slot) return
                const isSoftPending = currentSlot?.stop_state === 'soft_pending'
                // Per-slot view over the map, satisfying SoftStopRef so the
                // arming window is measured against THIS slot's soft press.
                const map = softStopAtMapRef.current
                const slotRef = {
                  get current() { return map.get(slot) ?? 0 },
                  set current(v: number) { map.set(slot, v) },
                }
                const action = handleStopPress(
                  isSoftPending,
                  Date.now(),
                  slotRef,
                  () => dispatch(requestStop({ slotId: slot, force: false })),
                  () => dispatch(requestStop({ slotId: slot, force: true })),
                )
                // 'ignore' = accidental rapid double-tap during the arming window
                if (action !== 'ignore') dispatch(clearPendingPermissions())
              }}
              isQueued={slotStopping}
              stopState={currentSlot?.stop_state}
              approvalMode={displayMode}
              onApprovalClick={(rect) => { setApprovalBtnRect(rect); setApprovalDropdown(!approvalDropdown) }}
              providerId={provider.id}
              reasoningEffort={currentSlot?.reasoning_effort || ''}
              onReasoningEffortClick={provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel) ? (rect) => { setReasoningEffortBtnRect(rect); setReasoningEffortDropdown(!reasoningEffortDropdown) } : undefined}
              autoNudgeActive={!!autoNudgeLoop?.active}
              autoNudgeCycleCount={autoNudgeLoop?.cycle_count || 0}
              onAutoNudgeClick={(rect) => { setAutoNudgeBtnRect(rect); setAutoNudgeOpen(!autoNudgeOpen) }}
              browseMode={browseMode}
              onBrowseToggle={toggleBrowseMode}
              memoryMode={currentSlot?.memory_mode ?? 'persistent'}
              cleanMode={currentSlot?.clean_mode}
              sentMessages={sentMessages}
              sendOnEnter={isMobile ? 'ctrl-enter' : chatConfig.sendOnEnter}
              followUpOptions={followUpOptions}
              followUpPicked={followUpPicked}
              quickSend={dashCfg?.quick_send}
              followUpLayout={chatConfig.followUpLayout}
              onFollowUpSelect={(o: string, e: React.MouseEvent) => {
                // Plan options (e.g. Stage-N-APPROVE) dispatch directly — no input fill.
                if (followUpIsPlan && modeRef.current === 'orchestrator' && activeSlot) {
                  if (planActionMutationRef.current.isPending) return
                  planActionMutationRef.current.mutate({ slot: activeSlot, action: o })
                  return
                }
                // One-click: enabled + no shift + not busy + not already in multi-select
                if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, slotRunning, followUpPicked.size, send)) return
                // Regular options: toggle. Click unpicked → append + mark; click
                // picked → try to remove text + unmark (if the user edited the
                // text so it no longer matches, leave text alone — the chip
                // still un-highlights for consistency).
                if (followUpPicked.has(o)) {
                  setInput(prev => {
                    // Order matters: try leading ", o" first so "opt, opt" + remove
                    // last "opt" doesn't match "opt, " and splice the wrong one.
                    const leading = ', ' + o
                    let idx = prev.indexOf(leading)
                    if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + leading.length)
                    const trailing = o + ', '
                    idx = prev.indexOf(trailing)
                    if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + trailing.length)
                    if (prev === o) return ''
                    return prev  // user edited — leave text, still unmark below
                  })
                  setFollowUpPicked(prev => { const next = new Set(prev); next.delete(o); return next })
                } else {
                  setInput(prev => prev.trim() ? prev.trimEnd() + ', ' + o : o)
                  setFollowUpPicked(prev => new Set(prev).add(o))
                }
              }}
              pasteBlocks={pasteBlocks}
              onPasteBlocksChange={setPasteBlocks}
              knowledgeChip={knowledgeFetch.pendingKnowledge ? <div className="flex items-start gap-1"><KnowledgeBubbleChip knowledge={{ items: knowledgeFetch.pendingKnowledge.items.length, tokens: knowledgeFetch.pendingKnowledge.totalTokens, titles: knowledgeFetch.pendingKnowledge.items.map(i => i.title), content: knowledgeFetch.pendingKnowledge.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }} /><button type="button" onClick={() => knowledgeFetch.clearPending()} className="shrink-0 mt-0.5 p-0.5 text-muted hover:text-danger bg-transparent border-none cursor-pointer rounded hover:bg-danger/10 transition-colors" aria-label="Remove knowledge context" title="Remove knowledge context">&times;</button></div> : undefined}

            />
            </div>
            {/* Agent dropdown portal — triggered from input bar */}
            {agentDropdown && agentBtnRect && createPortal(
              <div ref={agentDropdownRef} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up" style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}>
                <div className="px-1.5 pt-1.5 pb-1">
                  <Input ref={agentInputRef} type="text" placeholder="Type to filter…" value={agentFilter} onChange={e => setAgentFilter(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setAgentDropdown(false); if (e.key === 'Enter' && filteredAgents.length === 1) { switchAgent(filteredAgents[0].name); setAgentDropdown(false) } }} className="w-full px-2 py-1 text-[13px] font-mono" />
                </div>
                <div className="overflow-y-auto max-h-[280px]">
                <AgentDropdownList agents={filteredAgents} activeAgent={currentSlot?.agent || 'default'} defaultAgent={defaultAgent} onSelect={name => { switchAgent(name); setAgentDropdown(false) }} />
                </div>
              </div>,
              document.body
            )}
            {/* Model dropdown portal — triggered from input bar */}
            {modelDropdown && modelBtnRect && createPortal((() => {
              const hasEffort = !!(activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel))
              const popWidth = hasEffort ? 460 : 348
              return (
              <div ref={modelDropdownRef} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl flex flex-row p-1 gap-1 animate-slide-up" style={(() => { const left = Math.max(8, Math.min(modelBtnRect.left, window.innerWidth - popWidth)); return { bottom: window.innerHeight - modelBtnRect.top + 4, left } })()}>
                <div className="flex flex-col gap-0.5 flex-1 min-w-[252px]">
                  <div className="px-1.5 pt-1.5 pb-1">
                    <Input ref={modelInputRef} type="text" placeholder="Type to filter…" value={modelFilter} onChange={e => setModelFilter(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setModelDropdown(false); if (e.key === 'Enter' && filteredModels.length === 1) { switchModel(filteredModels[0].name); setModelDropdown(false) } }} className="w-full px-2 py-1 text-[13px] font-mono" />
                  </div>
                  <div className="overflow-y-auto max-h-[280px]">
                    <ModelDropdownList models={filteredModels} activeModel={currentSlot?.model || resolvedModel || 'auto'} onSelect={name => { switchModel(name); setModelDropdown(false) }} />
                  </div>
                </div>
                {hasEffort && activeSlot && (
                  <div className="flex flex-col gap-0.5 border-l border-border pl-1 w-[180px] shrink-0">
                    <div className="px-3 pt-2.5 pb-1 text-[11px] font-medium text-muted uppercase tracking-[.04em]">Reasoning effort</div>
                    <div className="overflow-y-auto max-h-[280px]">
                      <ReasoningEffortDropdown slot={activeSlot} currentEffort={currentSlot?.reasoning_effort || ''} onClose={() => setModelDropdown(false)} embedded />
                    </div>
                  </div>
                )}
              </div>
              )
            })(),
              document.body
            )}
            {/* Project picker — triggered from input bar */}
            <ProjectPicker
              open={projectPickerOpen}
              onOpenChange={setProjectPickerOpen}
              anchorRect={projectBtnRect}
              onSelect={path => { setProject(path); setProjectPickerOpen(false) }}
            />
            {/* Auto-nudge popover — triggered from input bar */}
            {autoNudgeOpen && autoNudgeBtnRect && activeSlot && createPortal(
              <AutoNudgePopover
                slotKey={activeSlot}
                anchorRect={autoNudgeBtnRect}
                loop={autoNudgeLoop}
                onClose={() => setAutoNudgeOpen(false)}
                onChange={setAutoNudgeLoop}
              />,
              document.body
            )}
            {/* Approval mode dropdown portal — triggered from input bar */}
            {approvalDropdown && approvalBtnRect && createPortal(
              <div ref={approvalDropdownRef} className="fixed z-[9999] animate-slide-up flex items-end gap-2" style={(() => { const left = Math.max(8, Math.min(approvalBtnRect.left, window.innerWidth - 520)); return { bottom: window.innerHeight - approvalBtnRect.top + 4, left: isMobile ? 8 : left, ...(isMobile ? { flexDirection: 'column-reverse' as const, alignItems: 'flex-start', right: 8, maxWidth: 'calc(100vw - 16px)' } : {}) } })()}>
                <div className="rounded-lg bg-bg-elevated border border-border py-1 w-[280px] shrink-0">
                  {APPROVAL_SEGMENTS.map(s => (
                    <Btn
                      key={s.key}
                      title={s.tooltip}
                      onClick={() => {
                        const m = s.key
                        if (m === 'yolo') {
                          if (displayMode === 'yolo') return
                          if (localStorage.getItem('mc-yolo-ack')) { dispatch(changeApprovalMode({ mode: m, slot: activeSlot || '' })); setApprovalDropdown(false) }
                          else setYoloConfirm(c => c + 1)
                          return
                        }
                        setYoloConfirm(0)
                        dispatch(changeApprovalMode({ mode: m, slot: activeSlot || '' })); setApprovalDropdown(false)
                      }}
                      className={`flex items-center gap-2 w-full px-3 py-2 text-[13px] font-medium cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover ${
                        s.key === displayMode ? 'text-accent' : 'text-text'
                      }`}
                    >
                      <span className="shrink-0">{s.icon}</span>
                      <span className="flex flex-col min-w-0 flex-1">
                        <span>{s.label}</span>
                        <span className="text-[11px] font-normal text-muted leading-snug">{s.desc}</span>
                      </span>
                      {s.key === displayMode && <Check size={12} className="shrink-0 text-accent" />}
                    </Btn>
                  ))}
                </div>
                {yoloConfirm > 0 && (
                  <motion.div key={yoloConfirm} animate={{ x: [0, -3, 3, -2, 2, 0] }} transition={{ duration: 0.3 }} className="px-3 py-2 rounded-lg bg-bg-elevated border border-border text-[12px] w-[260px]">
                    <p className="font-medium text-text">YOLO mode is an app-wide setting</p>
                    <p className="text-muted mt-0.5">All tools will get auto-approved across all sessions.</p>
                    <div className="flex items-center gap-2 mt-1.5">
                      <Btn className="px-2.5 py-1 rounded-md bg-card border border-border text-danger font-medium hover:bg-bg-hover cursor-pointer" onClick={() => {
                        dispatch(changeApprovalMode({ mode: 'yolo', slot: activeSlot || '' })); setYoloConfirm(0); setApprovalDropdown(false)
                      }}>Enable</Btn>
                      <Btn className="px-2.5 py-1 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={e => { e.stopPropagation(); setYoloConfirm(0) }}>Cancel</Btn>
                      <label className="flex items-center gap-1 text-[11px] text-muted cursor-pointer ml-auto">
                        <Input type="checkbox" className="rounded" onChange={e => { if ((e.target as HTMLInputElement).checked) localStorage.setItem('mc-yolo-ack', '1'); else localStorage.removeItem('mc-yolo-ack') }} />
                        Don't show again
                      </label>
                    </div>
                  </motion.div>
                )}
              </div>,
              document.body
            )}
            {/* Reasoning effort dropdown portal */}
            {reasoningEffortDropdown && reasoningEffortBtnRect && activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel) && createPortal(
              <div ref={reasoningEffortDropdownRef} className="fixed z-[9999] animate-slide-up" style={(() => { const left = Math.max(8, Math.min(reasoningEffortBtnRect.left, window.innerWidth - 220)); return { bottom: window.innerHeight - reasoningEffortBtnRect.top + 4, left: isMobile ? 8 : left, ...(isMobile ? { right: 8, maxWidth: 'calc(100vw - 16px)' } : {}) } })()}>
                <ReasoningEffortDropdown slot={activeSlot} currentEffort={currentSlot?.reasoning_effort || ''} onClose={() => setReasoningEffortDropdown(false)} />
              </div>,
              document.body
            )}
            </div>
          </div>
          </SearchHighlightContext.Provider>
        )}
      </div>
      )}
      <AnimatePresence>
        {diffPanel.isOpen && !panel.isOpen && (
          <DetailPanel
            key="diff-panel"
            title={
              <span className="flex items-center gap-2">
                <button className="truncate hover:text-accent cursor-pointer transition-colors" onClick={() => { diffPanel.closeDiff(); handleFileOpen(diffPanel.filePath) }}>
                  {diffPanel.filePath.split('/').pop() || 'Diff'}
                </button>
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0">CHANGE</span>
                {(() => { const { added, removed } = countLines(diffPanel.original, diffPanel.modified); return (added > 0 || removed > 0) ? <span className="text-[10px] font-mono shrink-0">{added > 0 && <span className="text-ok">+{added}</span>}{removed > 0 && <span className="text-danger ml-0.5">-{removed}</span>}</span> : null })()}
              </span>
            }
            onClose={() => { diffPanel.closeDiff(); dispatch(openActivityToTab('files')) }}
            initialWidth={600}
            storageKey="mc-panel-width"
            noPadding
            headerClassName="diff-panel-header"
            headerActions={
              <>
                <button onClick={() => setDiffLineNumbers(v => !v)} className={`p-1 rounded cursor-pointer transition-colors ${diffLineNumbers ? 'text-accent' : 'text-muted hover:text-text'}`} title={diffLineNumbers ? 'Hide line numbers' : 'Show line numbers'} aria-label={diffLineNumbers ? 'Hide line numbers' : 'Show line numbers'}><Hash size={14} /></button>
              </>
            }
          >
            <DiffPanel filePath={diffPanel.filePath} original={diffPanel.original} modified={diffPanel.modified} lineNumbers={diffLineNumbers} />
          </DetailPanel>
        )}
        {panel.isOpen && !diffPanel.isOpen && (
          <MarkdownPanel key="md-panel" filePath={panel.filePath} content={panel.content} onContentChange={panel.setContent} onSave={handleFileSave} onClose={panel.closePanel} liveWatch onSubmitComments={submitComments} />
        )}
        {activityOpen && !panel.isOpen && !diffPanel.isOpen && (
          <DetailPanel key="activity-panel" title="Activity" onClose={toggleAct} initialWidth={420} storageKey="mc-activity-width">
            <ActivityViewer subagents={subagents} toolLog={toolLog} open={true} onToggle={toggleAct} slot={activeSlot || ''} files={touchedFiles.files} onFileOpen={handleFileOpen} onFileRemove={touchedFiles.removeFile} onFilesClear={touchedFiles.clearBySource} projectDir={currentSlot?.project || undefined} />
          </DetailPanel>
        )}
      </AnimatePresence>
    </div>
  )
}

