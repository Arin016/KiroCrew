/**
 * Generic cross-window coordination engine for "pop out" features.
 *
 * A "pop out" opens some entity (a chat session, an artifact, …) in a
 * dedicated same-origin browser window. That window is its own JS context — it
 * opens its own `/api/ws` socket and Redux store, so it renders live and
 * independently; the gateway stays the single source of truth. This module
 * carries only the lightweight *coordination* the backend can't: which entities
 * are currently popped out, so the main dashboard can show an indicator, focus
 * an existing popout instead of spawning a duplicate, and bring one back.
 *
 * A single BroadcastChannel (one per feature — see `channelName`) is shared by
 * every same-origin tab/window. Popout windows announce their presence and
 * answer heartbeat pings; the main dashboard maintains an `id -> lastSeen` map
 * and prunes windows that stop responding (closed / crashed). Everything is
 * in-memory + channel messages — no persistence, so a stale entry can never
 * outlive a heartbeat interval.
 *
 * `createPopoutController` returns an independent instance (its own channel +
 * state), so multiple features (chat, artifacts) coordinate on separate
 * channels with zero cross-talk. The pure helpers (message reducer, prune) are
 * exported so the coordination logic is unit-testable without a live
 * BroadcastChannel.
 */

/** Heartbeat cadence (ms) for the main window's liveness ping. */
export const HEARTBEAT_MS = 5_000
/** A popout unseen for longer than this is considered gone and pruned. */
export const STALE_MS = 12_000

export type PopoutMsg =
  | { t: 'open'; id: string }
  | { t: 'close'; id: string }
  | { t: 'ping' }
  | { t: 'pong'; id: string }
  | { t: 'focus'; id: string }
  | { t: 'bring-back'; id: string }

/** entity id -> epoch ms the window was last seen alive. */
export type PopoutMap = Record<string, number>

/** Fold a channel message into the popped-out map (pure). */
export function applyMessage(map: PopoutMap, msg: PopoutMsg, now: number): PopoutMap {
  switch (msg.t) {
    case 'open':
    case 'pong':
      return { ...map, [msg.id]: now }
    case 'close': {
      if (!(msg.id in map)) return map
      const next = { ...map }
      delete next[msg.id]
      return next
    }
    default:
      return map
  }
}

/** Drop windows unseen for longer than `staleMs` (pure; identity-stable when unchanged). */
export function pruneStale(map: PopoutMap, now: number, staleMs: number = STALE_MS): PopoutMap {
  const next: PopoutMap = {}
  let changed = false
  for (const [id, seen] of Object.entries(map)) {
    if (now - seen <= staleMs) next[id] = seen
    else changed = true
  }
  return changed ? next : map
}

type Listener = () => void

/** Options that specialize a controller for one feature (chat, artifacts, …). */
export interface PopoutControllerOptions {
  /** Unique BroadcastChannel name for this feature (isolates cross-talk). */
  channelName: string
  /** Short label for guarded console diagnostics (e.g. 'chatPopout'). */
  logLabel: string
  /** Build the popout window URL for an entity id. */
  buildUrl: (id: string, title?: string) => string
  /** Stable, filesystem-safe `window.open` name for an entity id (enables dedupe). */
  windowName: (id: string) => string
  /**
   * Main-dashboard URL for an entity — the `returnSelfToMain` fallback target
   * when `window.close()` is refused (deep-linked / restored popouts have no
   * script opener, so close is a spec-level no-op there).
   */
  mainViewUrl: (id: string | null) => string
}

/** The main-window + popout-window API for one feature's popouts. */
export interface PopoutController {
  /** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
  subscribe(listener: Listener): () => void
  /** Current set of popped-out ids (stable identity until membership changes). */
  getSnapshot(): ReadonlySet<string>
  /** Open (or focus, if already open) an entity in its own browser window. */
  openPopout(id: string, title?: string): void
  /** Focus the popout window for an entity (direct handle, else ask it to focus itself). */
  focusPopout(id: string): void
  /** Close an entity's popout window and drop it from the map (caller re-views it in main). */
  bringBack(id: string): void
  /** True when THIS window is the live popout for `id`. */
  isSelfPopout(id: string): boolean
  /** From inside a popout: focus the opener and close; navigate to the main view when close is refused. */
  returnSelfToMain(): void
  /** Register THIS window as the live popout for `id` (responder role). Returns cleanup. */
  registerPopout(id: string): () => void
  /** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
  __setNavigateForTests(fn: (url: string) => void): void
  /** Test-only: reset all instance state between cases. */
  __resetForTests(): void
}

/**
 * Create an independent popout controller bound to one BroadcastChannel. All
 * state lives in this closure, so separate features never share a map, heartbeat,
 * or window-handle registry.
 */
export function createPopoutController(opts: PopoutControllerOptions): PopoutController {
  const { channelName, logLabel, buildUrl, windowName, mainViewUrl } = opts

  let channel: BroadcastChannel | null = null
  let map: PopoutMap = {}
  let snapshot: ReadonlySet<string> = new Set<string>()
  const listeners = new Set<Listener>()
  let heartbeat: ReturnType<typeof setInterval> | null = null
  let mainSubscribers = 0
  /** Non-null when THIS window is itself a popout (drives the responder role). */
  let selfId: string | null = null
  /** Handles for popouts THIS window opened — lets us focus/close them directly. */
  const handles = new Map<string, Window | null>()

  /**
   * Guarded console output. The window-control paths (open / focus / close) can
   * be silently vetoed by the browser (popup blocker, no user activation); these
   * keep every veto diagnosable instead of swallowed by an empty catch.
   */
  function logDebug(msg: string, err?: unknown): void {
    // eslint-disable-next-line no-console
    console.debug(`[${logLabel}] ${msg}`, err ?? '')
  }
  function logWarn(msg: string): void {
    // eslint-disable-next-line no-console
    console.warn(`[${logLabel}] ${msg}`)
  }

  function recomputeSnapshot(): void {
    const keys = Object.keys(map)
    if (keys.length === snapshot.size && keys.every(k => snapshot.has(k))) return
    snapshot = new Set(keys)
    listeners.forEach(l => l())
  }

  function ensureChannel(): BroadcastChannel | null {
    if (channel || typeof BroadcastChannel === 'undefined') return channel
    channel = new BroadcastChannel(channelName)
    channel.onmessage = (e: MessageEvent<PopoutMsg>) => handleMessage(e.data)
    return channel
  }

  function post(msg: PopoutMsg): void {
    ensureChannel()?.postMessage(msg)
  }

  function handleMessage(msg: PopoutMsg): void {
    // Popout responder role: answer liveness pings and honor control messages
    // addressed to this window's entity.
    if (selfId) {
      if (msg.t === 'ping') { post({ t: 'pong', id: selfId }); return }
      if (msg.t === 'focus' && msg.id === selfId) {
        // A channel-routed focus has no user activation, so browsers may veto it
        // (common after the opener refreshed and lost the direct handle).
        try { window.focus() } catch (e) { logDebug('self focus vetoed', e) }
        return
      }
      if (msg.t === 'bring-back' && msg.id === selfId) { returnSelfToMain(); return }
    }
    const now = Date.now()
    const next = pruneStale(applyMessage(map, msg, now), now)
    if (next !== map) { map = next; recomputeSnapshot() }
  }

  function heartbeatTick(): void {
    const now = Date.now()
    const pruned = pruneStale(map, now)
    if (pruned !== map) { map = pruned; recomputeSnapshot() }
    post({ t: 'ping' })
  }

  /**
   * Pause the interval while the tab is hidden, and on return to visible re-ping
   * BEFORE the next prune could run. Two reasons (both from review): a perpetual
   * 5s timer in every dashboard tab is wasted work for an opt-in feature, and a
   * backgrounded tab's throttled timers (~1/min) exceed STALE_MS, so without the
   * refresh a still-live popout would be pruned → its indicator flickers off/on.
   */
  function handleVisibilityChange(): void {
    if (document.hidden) {
      if (heartbeat) { clearInterval(heartbeat); heartbeat = null }
      return
    }
    if (mainSubscribers === 0 || heartbeat) return
    // Re-confirm liveness immediately: forgive the hidden gap (entries would
    // otherwise read as stale) and ping so live popouts re-add within a frame.
    const now = Date.now()
    let refreshed = false
    const next: PopoutMap = {}
    for (const [id, seen] of Object.entries(map)) {
      next[id] = Math.max(seen, now - HEARTBEAT_MS)
      refreshed = refreshed || next[id] !== seen
    }
    if (refreshed) { map = next; recomputeSnapshot() }
    post({ t: 'ping' })
    heartbeat = setInterval(heartbeatTick, HEARTBEAT_MS)
  }

  function startHeartbeat(): void {
    if (heartbeat || typeof BroadcastChannel === 'undefined') return
    document.addEventListener('visibilitychange', handleVisibilityChange)
    if (document.hidden) return // quiescent until visible; listener will start it
    post({ t: 'ping' })
    heartbeat = setInterval(heartbeatTick, HEARTBEAT_MS)
  }

  function stopHeartbeat(): void {
    document.removeEventListener('visibilitychange', handleVisibilityChange)
    if (heartbeat) { clearInterval(heartbeat); heartbeat = null }
  }

  function subscribe(listener: Listener): () => void {
    ensureChannel()
    listeners.add(listener)
    mainSubscribers += 1
    if (mainSubscribers === 1) startHeartbeat()
    return () => {
      listeners.delete(listener)
      mainSubscribers = Math.max(0, mainSubscribers - 1)
      if (mainSubscribers === 0) stopHeartbeat()
    }
  }

  function getSnapshot(): ReadonlySet<string> {
    return snapshot
  }

  function openPopout(id: string, title?: string): void {
    if (typeof window === 'undefined') return
    const existing = handles.get(id)
    if (existing && !existing.closed) {
      try { existing.focus() } catch (e) { logDebug(`focus of existing popout ${id} vetoed`, e) }
      return
    }
    const sc = window.screen
    const w = Math.min(880, Math.round((sc?.availWidth ?? 1280) * 0.55))
    const h = Math.min(900, Math.round((sc?.availHeight ?? 900) * 0.85))
    const availLeft = (sc as unknown as { availLeft?: number })?.availLeft ?? 0
    const availTop = (sc as unknown as { availTop?: number })?.availTop ?? 0
    const left = Math.round(availLeft + ((sc?.availWidth ?? 1280) - w) / 2)
    const top = Math.round(availTop + Math.max(0, ((sc?.availHeight ?? 900) - h) / 2))
    const features = `popup=yes,width=${w},height=${h},left=${left},top=${top},resizable=yes,scrollbars=yes`
    const win = window.open(buildUrl(id, title), windowName(id), features)
    if (!win) {
      // Popup blocker (or policy) vetoed the window. Don't optimistically mark
      // open — and don't fail silently: tell the user why nothing happened and
      // leave an operator-diagnosable trail.
      logWarn(`window.open blocked for ${id} — pop-up blocker or browser policy`)
      try {
        window.alert('Your browser blocked the pop-out window. Allow pop-ups for this site and try again.')
      } catch { /* alert unavailable (e.g. sandboxed frame) — the warn above still records it */ }
      return
    }
    handles.set(id, win)
    try { win.focus() } catch (e) { logDebug(`focus of new popout ${id} vetoed`, e) }
    // Optimistically mark open; the window's own 'open' announce refreshes lastSeen.
    map = { ...map, [id]: Date.now() }
    recomputeSnapshot()
  }

  function focusPopout(id: string): void {
    const win = handles.get(id)
    if (win && !win.closed) {
      try { win.focus(); return } catch (e) { logDebug(`direct focus of ${id} vetoed — falling back to channel`, e) }
    }
    post({ t: 'focus', id }) // handle lost (main refreshed) — the window focuses itself
  }

  function bringBack(id: string): void {
    const win = handles.get(id)
    if (win && !win.closed) {
      try { win.close() } catch (e) { logDebug(`direct close of ${id} vetoed — falling back to channel`, e) }
    }
    post({ t: 'bring-back', id })
    handles.delete(id)
    if (id in map) {
      const next = { ...map }
      delete next[id]
      map = next
      recomputeSnapshot()
    }
  }

  /**
   * True when THIS window is the live popout for `id`. Surfaces rendered
   * inside a popout need this: a popout never holds its own id in the
   * coordination map (BroadcastChannel doesn't self-deliver), so
   * `isPoppedOut(ownId)` reads false in the popout window.
   */
  function isSelfPopout(id: string): boolean {
    return selfId !== null && selfId === id
  }

  /**
   * Navigation indirection: jsdom can't redefine `window.location`, so tests
   * swap this via `__setNavigateForTests` to assert the deep-link fallback.
   */
  let navigate = (url: string): void => window.location.assign(url)

  /**
   * Return THIS popout window's entity to the main dashboard: focus the opener
   * and close. `window.close()` is a spec-level no-op for windows the script
   * didn't open — a deep-linked / restored / refreshed popout has no opener — so
   * when the close doesn't take, navigate this window to the main view
   * instead. The control must always visibly do something.
   */
  function returnSelfToMain(): void {
    const id = selfId
    try { window.opener?.focus() } catch (e) { logDebug('opener focus vetoed', e) }
    try { window.close() } catch (e) { logDebug('self close vetoed', e) }
    // If we're still alive, the close was refused (no script opener). Fall back
    // to becoming the main view for this entity.
    if (!window.closed) {
      if (id) post({ t: 'close', id }) // tell other windows this popout is gone
      navigate(mainViewUrl(id))
    }
  }

  function registerPopout(id: string): () => void {
    ensureChannel()
    selfId = id
    post({ t: 'open', id })
    const announceClose = () => post({ t: 'close', id })
    window.addEventListener('beforeunload', announceClose)
    window.addEventListener('pagehide', announceClose)
    return () => {
      window.removeEventListener('beforeunload', announceClose)
      window.removeEventListener('pagehide', announceClose)
      announceClose()
      if (selfId === id) selfId = null
    }
  }

  function __setNavigateForTests(fn: (url: string) => void): void {
    navigate = fn
  }

  function __resetForTests(): void {
    channel?.close()
    channel = null
    map = {}
    snapshot = new Set<string>()
    listeners.clear()
    stopHeartbeat()
    mainSubscribers = 0
    selfId = null
    handles.clear()
    navigate = (url: string) => window.location.assign(url)
  }

  return {
    subscribe, getSnapshot, openPopout, focusPopout, bringBack,
    isSelfPopout, returnSelfToMain, registerPopout,
    __setNavigateForTests, __resetForTests,
  }
}
