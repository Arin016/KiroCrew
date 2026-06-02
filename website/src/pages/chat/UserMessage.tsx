import { memo, useState, useRef, useEffect, useCallback } from 'react'
import { Pencil, Send, Copy, Check, Link2 } from 'lucide-react'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import { useSearchHighlight, useCurrentOcc } from '../../hooks/SearchHighlightContext'
import { applySearchHighlights } from '../../utils/domHighlight'
import { type PasteBlock, expandAll as expandPasteTokens } from '../../utils/pasteTokens'

interface UserMessageProps {
  content: string
  meta?: Record<string, unknown>
  timestamp?: string
  renderContent: (content: string, meta: Record<string, unknown> | undefined) => React.ReactNode
  canEdit?: boolean
  messageIndex?: number
  messageTs?: string
  onEditResend?: (index: number, ts: string, newContent: string) => void
  slotKey?: string
  slotTitle?: string
  mode?: string
}

const UserMessage = memo(function UserMessage({ content, meta, timestamp, renderContent, canEdit, messageIndex, messageTs, onEditResend, slotKey, slotTitle, mode }: UserMessageProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(content)
  const [copied, setCopied] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // Track the copy-reset timer so it can be cleared on unmount.  Without this,
  // the 1.5 s setTimeout below survives test teardown and fires after jsdom
  // has been disposed, throwing "ReferenceError: window is not defined" from
  // React's `getCurrentEventPriority` and failing the build under vitest 3.x.
  const copyResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current)
  }, [])

  useEffect(() => {
    if (editing && taRef.current) {
      const ta = taRef.current
      ta.focus()
      ta.selectionStart = ta.selectionEnd = ta.value.length
      ta.style.height = 'auto'
      ta.style.height = ta.scrollHeight + 'px'
    }
  }, [editing])

  const startEdit = useCallback(() => {
    // Expand any collapsed paste tokens into their original content so the
    // user can actually edit the pasted text. Once edited, the message is
    // resent as plain expanded text — no chip reconstruction.
    const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
    const initial = pastes.length ? expandPasteTokens(content, pastes) : content
    setDraft(initial)
    setEditing(true)
  }, [content, meta])
  const cancel = useCallback(() => setEditing(false), [])
  const submit = useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed) { setEditing(false); return }
    onEditResend?.(messageIndex ?? 0, messageTs ?? '', trimmed)
    setEditing(false)
  }, [draft, onEditResend, messageIndex, messageTs])

  const userRef = useRef<HTMLDivElement>(null)
  const { term, caseSensitive } = useSearchHighlight()
  const currentOcc = useCurrentOcc()

  useEffect(() => {
    if (userRef.current) applySearchHighlights(userRef.current, term, caseSensitive, currentOcc)
  }, [term, caseSensitive, currentOcc, content])

  /** Native select+copy from a sent bubble gives the literal chip label
   *  ("Paste #1 · 5 lines") — worthless on the other end. Intercept the
   *  copy event, clone the selected DOM, swap each `[data-paste-seq]` chip
   *  for its expanded content, and write that to the clipboard instead. */
  const handleCopy = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
    if (!pastes.length) return
    const sel = window.getSelection()
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return
    const range = sel.getRangeAt(0)
    if (!userRef.current?.contains(range.commonAncestorContainer)) return
    const frag = range.cloneContents()
    const chips = frag.querySelectorAll('[data-paste-seq]')
    if (!chips.length) return
    const bySeq = new Map(pastes.map(p => [p.seq, p]))
    chips.forEach(chip => {
      const seq = Number(chip.getAttribute('data-paste-seq'))
      const block = bySeq.get(seq)
      if (block) chip.replaceWith(document.createTextNode(block.content))
    })
    const tmp = document.createElement('div')
    tmp.appendChild(frag)
    const text = tmp.textContent ?? ''
    if (!text) return
    e.clipboardData.setData('text/plain', text)
    e.preventDefault()
  }, [meta])

  if (editing) {
    return (
      <div className="flex flex-col items-end gap-1 max-w-[550px] min-w-[200px]">
        <div className="w-full rounded-lg border-2 border-accent/60 bg-card overflow-hidden">
          <textarea
            ref={taRef}
            className="w-full px-3.5 py-2.5 text-sm leading-relaxed bg-transparent text-card-fg resize-none overflow-hidden focus:outline-none"
            style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
            value={draft}
            onChange={e => { setDraft(e.target.value); e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px' }}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } if (e.key === 'Escape') cancel() }}
          />
          <div className="flex justify-end gap-1.5 px-2 pb-2">
            <button onClick={cancel} className="px-2.5 py-1 text-[13px] text-muted hover:text-text rounded border border-border hover:bg-hover transition-colors" title="Cancel (Esc)">
              Cancel
            </button>
            <button onClick={submit} className="flex items-center gap-1 px-2.5 py-1 text-[13px] bg-accent text-white rounded hover:bg-accent/80 transition-colors" title="Send (Enter)">
              <Send size={10} /> Send
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="group/msg flex flex-col items-end">
      <div ref={userRef} onCopy={handleCopy} className="msg-content px-4 py-1.5 text-sm leading-relaxed rounded-xl bg-card text-card-fg overflow-hidden min-w-0 max-w-[550px]" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
        {renderContent(content, meta)}
      </div>
      <div className="flex items-center gap-1.5 px-1 mt-1 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300">
        <button
          onClick={() => {
            const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
            const toCopy = pastes.length ? expandPasteTokens(content, pastes) : content
            copyToClipboard(toCopy).then(() => {
              setCopied(true)
              if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current)
              copyResetTimerRef.current = setTimeout(() => {
                copyResetTimerRef.current = null
                setCopied(false)
              }, 1500)
            }).catch(() => {})
          }}
          className="text-muted hover:text-text p-0.5 rounded transition-colors"
          title="Copy"
          aria-label="Copy"
        >
          {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
        </button>
        {messageTs && slotKey && (
          <button
            onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(() => { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }).catch(() => {}) }}
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title="Copy link to message"
            aria-label="Copy link to message"
          >
            {linkCopied ? <Check size={14} className="text-ok" /> : <Link2 size={14} />}
          </button>
        )}
        {canEdit && onEditResend && (
          <button
            onClick={startEdit}
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title="Edit & Resend"
            aria-label="Edit & Resend"
          >
            <Pencil size={14} />
          </button>
        )}
        {timestamp && <span className="text-muted text-[12px] font-mono">{timestamp}</span>}
      </div>
    </div>
  )
})

export default UserMessage
