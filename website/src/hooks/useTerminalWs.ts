import { useEffect, useRef } from 'react'
import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'
import { registerTerminalWs, unregisterTerminalWs } from '../utils/terminalRegistry'

const MAX_RETRIES = 10
const BASE_DELAY_MS = 1000
const MAX_DELAY_MS = 30_000

/**
 * Hook that connects an xterm.js Terminal to a backend PTY via WebSocket.
 * Binary frames carry terminal I/O; JSON text frames carry control (resize).
 */
export function useTerminalWs(
  sessionId: string | null,
  terminal: Terminal | null,
  fitAddon: FitAddon | null,
) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>()
  const disposedRef = useRef(false)
  const retriesRef = useRef(0)
  // Store terminal/fitAddon in refs so connect() doesn't depend on them
  const termRef = useRef(terminal)
  const fitRef = useRef(fitAddon)
  termRef.current = terminal
  fitRef.current = fitAddon

  // Connect/reconnect when sessionId changes
  useEffect(() => {
    if (!sessionId || !termRef.current) return

    disposedRef.current = false
    retriesRef.current = 0

    function connect() {
      if (disposedRef.current) return
      if (retriesRef.current >= MAX_RETRIES) return

      // Close any existing connection first
      if (wsRef.current) {
        wsRef.current.onclose = null // prevent reconnect loop
        wsRef.current.close()
        wsRef.current = null
      }

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const url = `${proto}//${location.host}/api/ws/terminal/${sessionId}`
      const ws = new WebSocket(url)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.onopen = () => {
        retriesRef.current = 0
        registerTerminalWs(sessionId!, ws)
        const fit = fitRef.current
        const term = termRef.current
        if (fit && term) {
          fit.fit()
          ws.send(JSON.stringify({
            type: 'resize',
            cols: term.cols,
            rows: term.rows,
          }))
        }
      }

      ws.onmessage = (ev) => {
        if (ev.data instanceof ArrayBuffer) {
          termRef.current?.write(new Uint8Array(ev.data))
        }
      }

      ws.onclose = () => {
        unregisterTerminalWs(sessionId!)
        if (disposedRef.current) return
        const attempt = retriesRef.current++
        if (attempt >= MAX_RETRIES) return
        const delay = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
        const jitter = delay * 0.2 * Math.random()
        reconnectTimer.current = setTimeout(connect, delay + jitter)
      }

      ws.onerror = () => ws.close()
    }

    connect()

    return () => {
      disposedRef.current = true
      clearTimeout(reconnectTimer.current)
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [sessionId]) // only depends on sessionId — terminal/fit are in refs

  // Wire terminal input → WebSocket (once per terminal)
  useEffect(() => {
    if (!terminal) return
    const dispose = terminal.onData((data) => {
      const ws = wsRef.current
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })
    return () => dispose.dispose()
  }, [terminal])

  // Wire resize → WebSocket (once per terminal)
  useEffect(() => {
    if (!terminal) return
    const dispose = terminal.onResize(({ cols, rows }) => {
      const ws = wsRef.current
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })
    return () => dispose.dispose()
  }, [terminal])

  return wsRef
}
