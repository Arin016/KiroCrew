import { useSyncExternalStore } from 'react'

const registry = new Map<string, WebSocket>()
let activeId: string | null = null
let _enabled = false
const readyListeners = new Set<() => void>()
const enabledListeners = new Set<() => void>()

export function setTerminalEnabledFlag(v: boolean) {
  _enabled = v
  for (const cb of enabledListeners) cb()
}
export function isTerminalEnabled(): boolean { return _enabled }

function subscribeEnabled(cb: () => void): () => void {
  enabledListeners.add(cb)
  return () => { enabledListeners.delete(cb) }
}
function getEnabledSnapshot(): boolean { return _enabled }

export function useTerminalEnabled(): boolean {
  return useSyncExternalStore(subscribeEnabled, getEnabledSnapshot)
}

export function onTerminalReady(cb: () => void): () => void {
  readyListeners.add(cb)
  return () => { readyListeners.delete(cb) }
}

export function registerTerminalWs(sessionId: string, ws: WebSocket) {
  registry.set(sessionId, ws)
  for (const cb of readyListeners) cb()
  readyListeners.clear()
}

export function unregisterTerminalWs(sessionId: string) {
  registry.delete(sessionId)
}

export function setActiveTerminalSession(sessionId: string | null) {
  activeId = sessionId
}

export function getActiveTerminalWs(): WebSocket | null {
  if (!activeId) return null
  const ws = registry.get(activeId)
  if (ws && ws.readyState === WebSocket.OPEN) return ws
  return null
}

export function sendToTerminal(code: string): boolean {
  const ws = getActiveTerminalWs()
  if (!ws) return false
  try {
    ws.send(new TextEncoder().encode(code.trimEnd() + '\n'))
    return true
  } catch {
    return false
  }
}
