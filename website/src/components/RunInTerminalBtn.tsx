import { useState, useCallback, useRef, useEffect } from 'react'
import { SquareTerminal, Check, AlertCircle, ShieldAlert } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../store'
import { openCliPanel } from '../store/terminalSlice'
import { sendToTerminal, getActiveTerminalWs, onTerminalReady } from '../utils/terminalRegistry'
import { checkSensitiveCommand } from '../utils/sensitiveCommand'

export const SHELL_LANGS = new Set(['bash', 'sh', 'shell', 'zsh', 'console', 'terminal', 'fish'])

function stripPromptChars(code: string): string {
  return code.replace(/^[\$>]\s+/gm, '')
}

export default function RunInTerminalBtn({ code }: { code: string }) {
  const dispatch = useAppDispatch()
  const terminalOpen = useAppSelector(s => s.terminal.open)
  const [status, setStatus] = useState<'idle' | 'sent' | 'error' | 'warn'>('idle')
  const [warnReason, setWarnReason] = useState('')
  const warnTimerRef = useRef<ReturnType<typeof setTimeout>>()

  const flashTimerRef = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => () => { clearTimeout(warnTimerRef.current); clearTimeout(flashTimerRef.current) }, [])

  const unsubRef = useRef<(() => void) | null>(null)
  const readyTimeoutRef = useRef<ReturnType<typeof setTimeout>>()
  const pendingCodeRef = useRef<string>('')
  useEffect(() => () => { unsubRef.current?.(); clearTimeout(readyTimeoutRef.current) }, [])

  const execute = useCallback((cleaned: string) => {
    pendingCodeRef.current = cleaned
    if (!getActiveTerminalWs()) {
      if (!terminalOpen) dispatch(openCliPanel())
      unsubRef.current?.()
      clearTimeout(readyTimeoutRef.current)
      readyTimeoutRef.current = setTimeout(() => { unsubRef.current?.(); flash('error') }, 5000)
      unsubRef.current = onTerminalReady(() => {
        clearTimeout(readyTimeoutRef.current)
        sendToTerminal(pendingCodeRef.current) ? flash('sent') : flash('error')
      })
      return
    }

    if (sendToTerminal(cleaned)) {
      flash('sent')
    } else {
      flash('error')
    }
  }, [terminalOpen, dispatch])

  const run = useCallback(() => {
    const cleaned = stripPromptChars(code)
    if (!cleaned) return

    const match = checkSensitiveCommand(code)
    if (match) {
      setWarnReason(match.reason)
      setStatus('warn')
      clearTimeout(warnTimerRef.current)
      warnTimerRef.current = setTimeout(() => setStatus('idle'), 8000)
      return
    }

    execute(cleaned)
  }, [code, execute])

  const confirmRun = useCallback(() => {
    clearTimeout(warnTimerRef.current)
    const cleaned = stripPromptChars(code)
    if (!cleaned) return
    setStatus('idle')
    execute(cleaned)
  }, [code, execute])

  const cancelWarn = useCallback(() => {
    clearTimeout(warnTimerRef.current)
    setStatus('idle')
  }, [])

  function flash(s: 'sent' | 'error') {
    setStatus(s)
    flashTimerRef.current = setTimeout(() => setStatus('idle'), s === 'sent' ? 1200 : 2000)
  }

  if (status === 'warn') {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-[11px] text-warn truncate max-w-[180px]" title={warnReason}>
          <ShieldAlert size={11} className="inline mr-0.5" />{warnReason}
        </span>
        <button
          className="px-1.5 py-0.5 rounded text-[11px] bg-warn/20 text-warn hover:bg-warn/30 cursor-pointer"
          onClick={confirmRun}
          aria-label="Confirm run sensitive command"
        >
          Run anyway
        </button>
        <button
          className="px-1.5 py-0.5 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          onClick={cancelWarn}
          aria-label="Cancel"
        >
          Cancel
        </button>
      </span>
    )
  }

  if (status === 'sent') {
    return (
      <span className="p-1 rounded text-accent" title="Sent to terminal" aria-label="Sent to terminal">
        <Check size={13} />
      </span>
    )
  }

  if (status === 'error') {
    return (
      <span className="p-1 rounded text-danger" title="No active terminal session" aria-label="No active terminal">
        <AlertCircle size={13} />
      </span>
    )
  }

  return (
    <button
      className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
      onClick={run}
      title="Run in terminal"
      aria-label="Run in terminal"
    >
      <SquareTerminal size={13} />
    </button>
  )
}
