import { useState, useMemo, useEffect, memo, useRef } from 'react'
import { Copy, Check, Volume2, Code, ClipboardList, CheckCircle, RefreshCw, ChevronLeft, ChevronRight, GitFork, Link2 } from 'lucide-react'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import SelectionToolbar, { useSelectionActions } from '../../components/SelectionToolbar'
import { useSearchHighlight, useCurrentOcc } from '../../hooks/SearchHighlightContext'
import { applySearchHighlights } from '../../utils/domHighlight'
import FileChangeChips, { type FileChangeEntry } from '../../components/FileChangeChips'
import type { FileChipStyle } from './ChatSettings'
import { loadChatConfig } from './ChatSettings'
import { useSmoothStream } from '../../hooks/useSmoothStream'

const OPTION_RE = /\[OPTION:\s*(.+?)\]\s*$/
const OPTIONS_RE = /\[OPTIONS:\s*(.+?)\]\s*$/
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

export function parseOptions(content: string): { text: string; options: string[]; multi: boolean; isPlan: boolean } {
  const mSingle = content.match(OPTION_RE)
  if (mSingle) {
    const sep = mSingle[1].includes('|') ? '|' : ','
    const opts = mSingle[1].split(sep).map(o => o.trim()).filter(Boolean)
    const isPlan = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
    return { text: content.slice(0, mSingle.index).trimEnd(), options: opts, multi: false, isPlan }
  }
  const m = content.match(OPTIONS_RE)
  if (!m) return { text: content, options: [], multi: true, isPlan: false }
  const sep = m[1].includes('|') ? '|' : ','
  const isPlanMulti = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
  return { text: content.slice(0, m.index).trimEnd(), options: m[1].split(sep).map(o => o.trim()).filter(Boolean), multi: true, isPlan: isPlanMulti }
}

const AssistantMessage = memo(function AssistantMessage({ content, isStreaming, onFileOpen, planTaskId, onApplyPlan, slotRunning, onSpeak, timestamp, showFooter = true, onRegenerate, variants, variantIdx, onSwitchVariant, isRegenerating, onFork, onPlanFromHere, forkIndex, onQuote, messageTs, slotKey, slotTitle, mode, fileChanges, onOpenDiff, fileChipStyle }: { content: string; isStreaming: boolean; onFileOpen?: (path: string) => void; planTaskId?: string; onApplyPlan?: (steps: any[]) => Promise<boolean>; slotRunning?: boolean; onSpeak?: () => void; timestamp?: string; showFooter?: boolean; onRegenerate?: () => void; variants?: { content: string; ts?: string }[]; variantIdx?: number; onSwitchVariant?: (index: number) => void; isRegenerating?: boolean; onFork?: (index: number) => void | Promise<void>; onPlanFromHere?: (index: number) => void | Promise<void>; forkIndex?: number; onQuote?: (text: string, rect: DOMRect) => void; messageTs?: string; slotKey?: string; slotTitle?: string; mode?: string; fileChanges?: FileChangeEntry[]; onOpenDiff?: (path: string, modified: string, original: string) => void; fileChipStyle?: FileChipStyle }) {
  const [applied, setApplied] = useState(false)
  const [copied, setCopied] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const [forking, setForking] = useState(false)
  const [rawMode, setRawMode] = useState(false)
  const [localIdx, setLocalIdx] = useState<number | null>(null)
  useEffect(() => { setLocalIdx(null) }, [content, variants?.length])

  const hasVariants = variants && variants.length > 1
  const activeIdx = onSwitchVariant ? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1) : (localIdx ?? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1))
  const effectiveContent = hasVariants && localIdx !== null && !onSwitchVariant ? (variants[localIdx]?.content ?? content) : content
  useEffect(() => { if (applied) setApplied(false) }, [effectiveContent])
  const { text } = parseOptions(effectiveContent)
  const [smooth] = useState(() => loadChatConfig().streamMode !== 'immediate')
  const speed = 4 // force high speed smooth streaming to avoid lagging behind raw model output
  const smoothedText = useSmoothStream(text, isStreaming, smooth, speed)

  const planSteps = useMemo(() => {
    if (isStreaming || !planTaskId || !effectiveContent) return null
    const jsonMatch = effectiveContent.match(/```json\s*\n([\s\S]*?)\n```/)
    if (!jsonMatch) return null
    try {
      const parsed = JSON.parse(jsonMatch[1])
      if (!Array.isArray(parsed) || !parsed.length) return null
      const valid = parsed.every((s: any) =>
        typeof s?.title === 'string' && s.title.trim() &&
        (!s.depends_on || (Array.isArray(s.depends_on) && s.depends_on.every((d: any) => typeof d === 'number')))
      )
      return valid ? parsed : null
    } catch {}
    return null
  }, [effectiveContent, isStreaming, planTaskId])

  const contentRef = useRef<HTMLDivElement>(null)
  const selectionActions = useSelectionActions(onQuote)

  const { term, caseSensitive } = useSearchHighlight()
  const currentOcc = useCurrentOcc()

  useEffect(() => {
    const el = contentRef.current
    if (!el) return

    const run = () => applySearchHighlights(el, term, caseSensitive, currentOcc)
    run()

    // Code blocks use dangerouslySetInnerHTML — hljs runs in a child
    // useEffect and sets innerHTML asynchronously after this effect.
    // A MutationObserver catches those deferred DOM updates and re-runs
    // the TreeWalker so code block content gets highlighted too.
    //
    // The observer also fires when our own applySearchHighlights mutates
    // the DOM (inserting <mark> elements). To prevent an infinite loop:
    // 1. Disconnect the observer before running the TreeWalker
    // 2. Re-observe after the TreeWalker finishes
    // 3. Batch rapid mutations via requestAnimationFrame + a scheduled flag
    //
    // Performance: the observer fires on any subtree mutation (React
    // re-renders, hljs updates, our own marks). Each firing runs one
    // TreeWalker pass which is sub-millisecond even for long messages,
    // so the extra runs are negligible.
    if (!term) return
    let disposed = false
    let scheduled = false
    const observer = new MutationObserver(() => {
      if (scheduled) return
      scheduled = true
      requestAnimationFrame(() => {
        scheduled = false
        if (disposed) return
        observer.disconnect()
        run()
        observer.observe(el, { childList: true, subtree: true, characterData: true })
      })
    })
    observer.observe(el, { childList: true, subtree: true, characterData: true })
    return () => { disposed = true; observer.disconnect() }
  }, [term, caseSensitive, currentOcc, effectiveContent, rawMode])

  return <div data-role="assistant" className="group/msg">
    <div ref={contentRef} className="msg-content group/bubble relative text-sm leading-relaxed text-text overflow-hidden" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
      <MarkdownRenderer content={smoothedText} streaming={isStreaming} onFileOpen={onFileOpen} rawMode={rawMode} messageTs={messageTs} glow={isStreaming} smooth={smooth} />
      {!isStreaming && selectionActions.length > 0 && <SelectionToolbar containerRef={contentRef} actions={selectionActions} />}
    </div>
    {fileChanges && fileChanges.length > 0 && !isStreaming && (
      <FileChangeChips fileChanges={fileChanges} onOpenDiff={onOpenDiff} style={fileChipStyle} />
    )}
    {!isStreaming && showFooter && (
      <div className="flex items-center gap-1 mt-0.5 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300">
        {timestamp && <span className="text-muted text-[12px] font-mono mr-1.5">{timestamp}</span>}
        <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Copy" aria-label={copied ? 'Copied!' : 'Copy'} onClick={() => { copyToClipboard(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {}) }}>{copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}</button>
        {messageTs && slotKey && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Copy link to message" aria-label="Copy link to message" onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(() => { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }).catch(() => {}) }}>{linkCopied ? <Check size={14} className="text-ok" /> : <Link2 size={14} />}</button>}
        {onFork && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={forking} title="Fork conversation from here" aria-label="Fork conversation from here" onClick={async () => { setForking(true); try { await onFork(forkIndex) } finally { setForking(false) } }}><GitFork size={14} /></button>}
        {onPlanFromHere && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={forking} title="Plan from here" aria-label="Plan from here" onClick={async () => { setForking(true); try { await onPlanFromHere(forkIndex) } finally { setForking(false) } }}><ClipboardList size={14} /></button>}
        {text.length >= 50 && onSpeak && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Speak" aria-label="Speak message" onClick={onSpeak}><Volume2 size={14} /></button>}
        {text.length > 20 && <button className={`p-0.5 rounded transition-colors flex items-center gap-0.5 text-[11px] ${rawMode ? 'text-text' : 'text-muted hover:text-text'}`} title={rawMode ? 'Rendered view' : 'Raw markdown'} aria-label={rawMode ? 'Switch to rendered view' : 'Switch to raw markdown view'} onClick={() => setRawMode(!rawMode)}><Code size={14} />{rawMode ? 'rendered' : 'raw'}</button>}
        {onRegenerate && !slotRunning && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Regenerate" aria-label="Regenerate response" onClick={onRegenerate}><RefreshCw size={14} /></button>}
        {hasVariants && (() => {
          const curIdx = activeIdx
          const switchFn = onSwitchVariant || ((i: number) => setLocalIdx(i))
          return (
            <div className="flex items-center gap-0.5 ml-1 text-[11px] text-muted">
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title="Previous version" aria-label="Previous version" disabled={curIdx <= 0 || !!slotRunning} onClick={() => switchFn(curIdx - 1)}><ChevronLeft size={14} /></button>
              <span className="font-mono">{curIdx + 1}/{variants!.length}</span>
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title="Next version" aria-label="Next version" disabled={curIdx >= variants!.length - 1 || !!slotRunning} onClick={() => switchFn(curIdx + 1)}><ChevronRight size={14} /></button>
            </div>
          )
        })()}
      </div>
    )}
    {planSteps && onApplyPlan && !applied && !isRegenerating && (
      <button className="mt-1 px-3 py-1.5 rounded-md text-[13px] font-medium border border-accent text-accent bg-transparent cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => { const ok = await onApplyPlan(planSteps); if (ok) setApplied(true) }}>
        <ClipboardList className="lucide-inline" /> Use as Plan ({planSteps.length} steps)
      </button>
    )}
    {applied && <div className="mt-1 text-[13px] text-ok"><CheckCircle className="lucide-inline" /> Applied to Tasks</div>}
  </div>
})

export default AssistantMessage
