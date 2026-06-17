import { createSlice, createAsyncThunk, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { addSlotOptimistic, removeSlotOptimistic, markSlotRead, fetchSlots, slotSurfaceKey } from './dashboardSlice'
import { resolveDefaultColor } from '../utils/sessionColors'
import type { RootState } from './index'
import type { ChatMessage, SessionInfo, SubagentActivity, ToolActivity } from '../types'
import { SOFT_STOP_DEBOUNCE_MS } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'

const SKIP_ROLES = new Set(['chunk', 'done'])
const filterMessages = (msgs: ChatMessage[]) => msgs.filter(m => !SKIP_ROLES.has(m.role))

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

export interface SideMessage {
  role: 'user' | 'assistant'
  content: string
  ts: string
  run_id?: string
  is_error?: boolean
}

export interface SideState {
  messages: SideMessage[]
  lastRunId?: string
  pending?: boolean
  streaming?: boolean
  openedAtTurnCount: number
  createdAt: string
}

interface ChatState {
  activeSlot: string | null
  messages: ChatMessage[]
  slotRunning: boolean
  slotStopping: boolean
  slotState: SlotState
  slotStatusDetail: Record<string, { kind: string; text: string; ts: number; toolName?: string }>
  slotHasMore: boolean
  slotOldestIndex: number
  loadingOlder: boolean
  lastChunkSeq: number | undefined
  _wsChunkedDuringFetch: boolean
  history: SessionInfo[]
  historyHasMore: boolean
  historyOffset: number
  pendingInput: string | null
  slotContextPct: Record<string, number>
  // Real token counts behind the context ring (from the adapter usage_update),
  // keyed by slot. Used for the ring tooltip so "44%" shows its absolute
  // "used / window" tokens and can't be misread (e.g. 44% of 200k, not 1M).
  slotContextTokens: Record<string, { used: number; window: number }>
  voicePlaying: boolean
  voiceAudio: string | null  // base64 stitched MP3 for replay
  subagents: Record<string, SubagentActivity>
  toolLog: ToolActivity[]
  activityOpen: boolean
  activityTab: 'subagents' | 'logs' | 'files' | 'side' | 'nav'
  /** Tool call to highlight & auto-expand inline. Set by openActivityToTool;
   *  consumed (cleared) once the matching ToolCallLine has expanded itself. */
  focusToolCallId: string | null
  slotActivity: Record<string, { toolLog: ToolActivity[]; subagents: Record<string, SubagentActivity>; activityTab?: 'subagents' | 'logs' | 'files' | 'side' | 'nav' }>
  slotSide: Record<string, SideState>
  slotSideClosed: Record<string, boolean>
  slotMessages: Record<string, ChatMessage[]>
  slotLoading: boolean
  slotHistory: string[]
  stopPressedAt: Record<string, number | null>
  pendingQuestion: { slot: string; questions: Array<{ question: string; header?: string; options: Array<{ label: string; description?: string }>; multiSelect?: boolean }> } | null
  // Slot with a locally-started turn awaiting server confirmation. While set,
  // the slots-sync ignores a server running=false for it (the snapshot may
  // predate the send). Cleared on server confirmation or turn end.
  pendingTurnSlot: string | null
}

const initialState: ChatState = {
  activeSlot: null,
  messages: [],
  slotRunning: false,
  slotStopping: false,
  slotState: 'idle',
  slotStatusDetail: {},
  slotHasMore: false,
  slotOldestIndex: 0,
  loadingOlder: false,
  lastChunkSeq: undefined,
  _wsChunkedDuringFetch: false,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
  pendingInput: null,
  slotContextPct: {},
  slotContextTokens: {},
  voicePlaying: false,
  voiceAudio: null,
  subagents: {},
  toolLog: [],
  activityOpen: false,
  activityTab: 'files' as const,
  focusToolCallId: null,
  slotActivity: {},
  slotMessages: {},
  slotLoading: false,
  slotSide: {},
  slotSideClosed: {},
  slotHistory: [],
  pendingQuestion: null,
  stopPressedAt: {},
  pendingTurnSlot: null,
}

function pushHistory(history: string[], key: string): string[] {
  const deduped = history.filter(k => k !== key)
  deduped.push(key)
  return deduped.length > 50 ? deduped.slice(-50) : deduped
}

export const fetchHistory = createAsyncThunk(
  'chat/fetchHistory',
  async (append: boolean, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    const offset = append ? state.historyOffset : 0
    const d = await api.sessions(30, offset)
    return { sessions: (d.sessions || d) as SessionInfo[], hasMore: d.has_more || false, offset, append }
  },
)

async function fetchSlotDetail(key: string) {
  // No limit → backend returns all chained history (across gateway restarts).
  const d = await api.chatSlotDetail(key)
  return { key, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as any[]).map((q: any) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString() }) }
}

export const switchSlot = createAsyncThunk(
  'chat/switchSlot',
  async (key: string, { dispatch }) => {
    dispatch(markSlotRead(key))
    return fetchSlotDetail(key)
  },
)

/** Re-fetch messages for a slot without changing activeSlot. Only applies if still active. */
/** Re-insert client-only reasoning (`thinking`) messages into a server-refreshed
 *  message list. The backend never persists reasoning, so a refresh (e.g. the
 *  one fired on chat_done) would otherwise drop the thinking block the instant a
 *  turn finishes. Each preserved block is anchored to the assistant message that
 *  immediately followed it in the old list (matched by finalized content) and
 *  re-inserted just before it. At most one reasoning block per assistant. Any
 *  block whose anchor isn't found is appended so it is never silently lost.
 *  Returns `incoming` unchanged (reference-equal) when there is nothing to
 *  preserve. */
function mergePreservedThinking<M extends { role: string; content: string; cls?: string }>(
  existing: M[],
  incoming: M[],
): M[] {
  const preserved: Array<{ msg: M; anchor: string | null }> = []
  for (let i = 0; i < existing.length; i++) {
    const m = existing[i]
    if (m.role !== 'thinking' || !m.content) continue
    let anchor: string | null = null
    for (let j = i + 1; j < existing.length; j++) {
      const r = existing[j].role
      if (r === 'assistant' || r === 'streaming') { anchor = existing[j].content.trimEnd(); break }
      if (r === 'user') break
    }
    preserved.push({ msg: m, anchor })
  }
  if (!preserved.length) return incoming
  const used = new Set<number>()
  const result: M[] = []
  for (const item of incoming) {
    if (item.role === 'assistant') {
      const c = item.content.trimEnd()
      for (let p = 0; p < preserved.length; p++) {
        if (!used.has(p) && preserved[p].anchor === c) {
          result.push({ ...preserved[p].msg }); used.add(p); break
        }
      }
    }
    result.push(item)
  }
  for (let p = 0; p < preserved.length; p++) {
    if (!used.has(p)) result.push({ ...preserved[p].msg })
  }
  return result
}

export const refreshSlot = createAsyncThunk(
  'chat/refreshSlot',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot !== key) return null
    return fetchSlotDetail(key)
  },
)

export const createSlot = createAsyncThunk(
  'chat/createSlot',
  async (opts: { agent?: string; model?: string; mode?: string; memory_mode?: string; clean_mode?: boolean; folder_id?: string | null; color_index?: number | null; project?: string | null } | string | undefined, { dispatch, getState }) => {
    const agent = typeof opts === 'string' ? opts : opts?.agent
    const model = typeof opts === 'string' ? undefined : opts?.model
    const mode = typeof opts === 'string' ? undefined : opts?.mode
    const memory_mode = typeof opts === 'string' ? undefined : opts?.memory_mode
    const clean_mode = typeof opts === 'string' ? undefined : opts?.clean_mode
    const folderId = typeof opts === 'string' ? undefined : opts?.folder_id
    const explicitColor = typeof opts === 'string' ? undefined : opts?.color_index
    const project = typeof opts === 'string' ? undefined : opts?.project
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, undefined, clean_mode)
    const dashState = (getState() as RootState).dashboard
    // An explicit color (e.g. carried from a slot being recreated on a
    // mode switch) wins; otherwise fall back to the default-color policy.
    const ci = explicitColor != null ? explicitColor : resolveDefaultColor(dashState.sessionDefaultColor, dashState.slots.length)
    if (ci != null) {
      slot.color_index = ci
      api.setSlotColor(slot.key, ci).catch(() => {})
    }
    // Carry folder membership so a recreated slot stays in its folder
    // instead of popping out to the top level.
    if (folderId) {
      slot.folder_id = folderId
      api.setSlotFolder(slot.key, folderId).catch(() => {})
    }
    // Carry the project directory. The create endpoint ignores `project` and
    // defaults it to the workspace dir, so a recreated slot would otherwise
    // lose its project — re-apply it via the dedicated endpoint. (We do NOT
    // re-issue setSlotAgent here: that endpoint resets the project back to the
    // workspace default, which would clobber this carry. Agent rides the
    // create payload instead.)
    if (project) {
      slot.project = project
      api.chatSlotProject(slot.key, project).catch(() => {})
    }
    dispatch(addSlotOptimistic(slot))
    return slot
  },
)

export const deleteSlot = createAsyncThunk(
  'chat/deleteSlot',
  async (key: string, { dispatch, getState }) => {
    const root = getState() as RootState
    const deletedSlot = root.dashboard.slots.find(s => s.key === key)
    // Use the surface key (forward-compat alias for `mode`) so a future
    // backend that emits a distinct `slot.surface` keeps "switch to a peer
    // session" pinned to the same nav destination.
    const deletedSurface = deletedSlot ? slotSurfaceKey(deletedSlot) : ''
    // Navigate before removeSlotOptimistic to prevent useEffect race
    if (root.chat.activeSlot === key) {
      const sameSurface = new Set(root.dashboard.slots.filter(s => slotSurfaceKey(s) === deletedSurface).map(s => s.key))
      const prev = root.chat.slotHistory.filter(k => k !== key && sameSurface.has(k)).pop()
        || root.dashboard.slots.filter(s => s.key !== key && sameSurface.has(s.key)).map(s => s.key)[0]
      dispatch({ type: 'chat/setActiveSlot', payload: null })
      if (prev) {
        await dispatch(switchSlot(prev)).unwrap().catch(() => dispatch({ type: 'chat/clearSlotState' }))
      } else {
        dispatch({ type: 'chat/clearSlotState' })
      }
    }
    dispatch(removeSlotOptimistic(key))
    try {
      await api.deleteChatSlot(key)
    } catch {
      dispatch(fetchSlots())
      throw new Error('save failed')
    }
    return key
  },
)

export const resumeFromHistory = createAsyncThunk(
  'chat/resumeFromHistory',
  async ({ key, title }: { key: string; title: string }, { dispatch }) => {
    const d = await api.resumeChatSlot(key, title)
    if (d.ok) dispatch(addSlotOptimistic({ key: d.key, title: title || d.key, messages: 0, running: false, memory_mode: d.memory_mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }))
    return { ok: d.ok, key: d.key, messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
  },
)

export const forkSlot = createAsyncThunk(
  'chat/forkSlot',
  async (
    { slot, atIndex, prompt, mode }: { slot: string; atIndex?: number; prompt?: string; mode?: string },
    { dispatch },
  ) => {
    const d = await api.forkChatSlot(slot, atIndex, prompt, mode)
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: d.title || d.key, messages: d.messages || 0, running: false, folder_id: d.folder_id }))
    }
    return d
  },
)

export const deleteHistorySession = createAsyncThunk(
  'chat/deleteHistorySession',
  async (key: string) => { await api.deleteSession(key); return key },
)

export const loadOlderMessages = createAsyncThunk(
  'chat/loadOlder',
  async (_, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!state.activeSlot || !state.slotHasMore || state.loadingOlder) return null
    if (state.slotOldestIndex <= 0) return null
    const d = await api.chatSlotDetail(state.activeSlot, 100, state.slotOldestIndex)
    return { messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
  },
)

export const requestStop = createAsyncThunk(
  'chat/requestStop',
  async ({ slotId, force }: { slotId: string; force: boolean }, { getState, dispatch }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!force) {
      const lastPress = state.stopPressedAt[slotId] ?? 0
      if (Date.now() - lastPress < SOFT_STOP_DEBOUNCE_MS) return
    }
    dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: Date.now() }))
    try {
      if (force) {
        await api.stopChatSlotForce(slotId)
      } else {
        await api.stopChatSlot(slotId)
      }
    } catch {
      dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: 0 }))
    }
  },
)

/** Get subagents map for a slot (read-only lookup) */
function getSlotSubs(state: ChatState, slot: string) {
  return slot !== state.activeSlot ? state.slotActivity[slot]?.subagents : state.subagents
}

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    setActiveSlot(state, action: PayloadAction<string | null>) { state.activeSlot = action.payload; state.slotState = 'idle'; state.pendingTurnSlot = null },
    clearSlotState(state) { state.messages = []; state.toolLog = []; state.subagents = {}; state.activityTab = 'files'; state.slotRunning = false; state.slotStopping = false; state.slotState = 'idle'; state.slotHasMore = false; state.slotOldestIndex = 0; state.loadingOlder = false; state.lastChunkSeq = undefined; state._wsChunkedDuringFetch = false; state.slotStatusDetail = {}; state.voicePlaying = false; state.voiceAudio = null; state.pendingQuestion = null; state.pendingTurnSlot = null },
    setPendingInput(state, action: PayloadAction<string | null>) { state.pendingInput = action.payload },
    setQuestionCard(state, action: PayloadAction<ChatState['pendingQuestion']>) { state.pendingQuestion = action.payload },
    clearQuestionCard(state) { state.pendingQuestion = null },
    sseContextUsage(state, action: PayloadAction<{ slot: string; pct: number; used_tokens?: number; window_tokens?: number }>) {
      const { slot, pct, used_tokens, window_tokens } = action.payload
      state.slotContextPct[slot] = pct
      if (window_tokens && window_tokens > 0) {
        state.slotContextTokens[slot] = { used: used_tokens ?? 0, window: window_tokens }
      }
    },
    appendMessage(state, action: PayloadAction<ChatMessage>) { state.messages.push(action.payload) },
    updateStreamingMessage(state, action: PayloadAction<string>) {
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.content = action.payload }
      else { state.messages.push({ role: 'streaming', content: action.payload, cls: 'msg msg-a' }) }
    },
    finalizeAssistant(state, action: PayloadAction<string | { content: string; ts?: string }>) {
      const payload = typeof action.payload === 'string' ? { content: action.payload } : action.payload
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.role = 'assistant'; last.content = payload.content; if (payload.ts) last.ts = payload.ts }
      else { state.messages.push({ role: 'assistant', content: payload.content, cls: 'msg msg-a', ts: payload.ts }) }
    },
    removeThinking(state) { state.messages = state.messages.filter(m => m.role !== 'thinking') },
    removeByApprovalId(state, action: PayloadAction<string>) { state.messages = state.messages.filter(m => m.meta?.approval_id !== action.payload) },
    resolveByApprovalId(state, action: PayloadAction<{ id: string; decision?: string }>) {
      const decision = action.payload.decision || 'approved'
      const m = state.messages.find(m => m.meta?.approval_id === action.payload.id)
      if (m?.meta) m.meta.resolved = decision
      // If rejected, mark the matching toolLog entry so the pill can show a rejection icon
      const toolCallId = m?.meta?.tool_call_id as string | undefined
      if (decision === 'rejected' && toolCallId) {
        const log = state.toolLog
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && log[i].tool_call_id === toolCallId) {
            log[i].rejected = true; break
          }
        }
      }
    },
    /** Mark all unresolved permission messages as resolved (e.g. when stop is pressed). */
    clearPendingPermissions(state) {
      for (const m of state.messages) {
        if (m.role === 'permission' && !m.meta?.resolved) {
          if (m.meta) m.meta.resolved = 'rejected'
          else m.meta = { resolved: 'rejected' }
        }
      }
      // Mark all incomplete toolLog entries as rejected so pills show the right icon
      for (const e of state.toolLog) {
        if (e.type === 'tool' && e.output == null && !e.rejected) e.rejected = true
      }
    },
    setSlotRunning(state, action: PayloadAction<boolean>) {
      state.slotRunning = action.payload
      if (!action.payload) state.pendingTurnSlot = null
    },
    /** Optimistically start a turn for `slot` after a local send. Marks it
     *  pending so the slots-sync won't clobber running=true before the server
     *  catches up. Only the active slot drives the visible footer. */
    startLocalTurn(state, action: PayloadAction<string>) {
      const slot = action.payload
      state.pendingTurnSlot = slot
      if (slot === state.activeSlot) state.slotRunning = true
    },
    /** Reconcile the active slot's running state from a WS slots broadcast.
     *  running=true is always trusted (also catches Slack/cron-initiated turns);
     *  running=false is ignored while a local turn is pending confirmation, since
     *  the snapshot may predate the send. Turn end is owned by _done/refreshSlot. */
    syncSlotRunningFromServer(state, action: PayloadAction<{ slot: string; running: boolean; stopping: boolean }>) {
      const { slot, running, stopping } = action.payload
      if (slot !== state.activeSlot) return
      if (running) {
        state.slotRunning = true
        state.slotStopping = stopping
        state.pendingTurnSlot = null
      } else if (state.pendingTurnSlot !== slot) {
        state.slotRunning = false
        state.slotStopping = stopping
      }
      // Pending turn: ignore both fields so a leftover stopping=true from a
      // prior turn can't falsely show a "stopping" state on the new turn.
    },
    setSlotStopping(state, action: PayloadAction<boolean>) { state.slotStopping = action.payload },
    setStopPressedAt(state, action: PayloadAction<{ slotId: string; ts: number }>) { state.stopPressedAt[action.payload.slotId] = action.payload.ts },
    setSlotState(state, action: PayloadAction<SlotState>) { state.slotState = action.payload },
    setSlotStatusDetail(state, action: PayloadAction<{ slot: string; kind: string; text: string; ts: number; toolName?: string }>) {
      const { slot, ...detail } = action.payload
      state.slotStatusDetail[slot] = detail
    },
    clearMessages(state) { state.messages = []; state.slotHasMore = false; state.slotOldestIndex = 0; state.voiceAudio = null; state.voicePlaying = false },
    truncateAfterIndex(state, action: PayloadAction<number>) { state.messages = state.messages.slice(0, action.payload) },
    replaceMessages(state, action: PayloadAction<ChatMessage[]>) { state.messages = action.payload },
    setVoicePlaying(state, action: PayloadAction<boolean>) { state.voicePlaying = action.payload },
    setVoiceAudio(state, action: PayloadAction<string | null>) { state.voiceAudio = action.payload },
    toggleActivity(state) { state.activityOpen = !state.activityOpen; if (!state.activityOpen) state.focusToolCallId = null },
    openActivityToTab(state, action: PayloadAction<'subagents' | 'logs' | 'files' | 'side' | 'nav'>) { state.activityOpen = true; state.activityTab = action.payload; state.focusToolCallId = null },
    /** Tools tab is deprecated — tool details now expand inline in the chat. This action
     *  signals the matching ToolCallLine pill to auto-expand and scroll into view. */
    openActivityToTool(state, action: PayloadAction<string>) { state.focusToolCallId = action.payload },
    /** Clear after the matching pill has consumed the focus signal, so the same trigger
     *  doesn't re-fire on subsequent re-renders. */
    clearFocusToolCallId(state) { state.focusToolCallId = null },
    sseSubagentPending(state, action: PayloadAction<{ slot: string; id: string; task: string; approval_id: string }>) {
      const entry: SubagentActivity = {
        id: action.payload.id, task: action.payload.task, agent: '',
        status: 'pending', streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
        approval_id: action.payload.approval_id,
      }
      if (action.payload.slot !== state.activeSlot) {
        const c = state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }
        c.subagents[action.payload.id] = entry
        return
      }
      state.subagents[action.payload.id] = entry
    },
    markSubagentApproving(state, action: PayloadAction<{ id: string; approving: boolean }>) {
      const a = state.subagents[action.payload.id]
      if (a) { a.approving = action.payload.approving; return }
      for (const sa of Object.values(state.slotActivity)) {
        const b = sa.subagents[action.payload.id]
        if (b) { b.approving = action.payload.approving; return }
      }
    },
    sseSubagentSpawn(state, action: PayloadAction<{ slot: string; id: string; task: string; agent: string }>) {
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[action.payload.id]
      if (existing?.status === 'pending') {
        existing.status = 'running'
        existing.agent = action.payload.agent || existing.agent || 'kiroclaw'
        return
      }
      subs[action.payload.id] = {
        id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kiroclaw',
        status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
      }
    },
    sseSubagentChunk(state, action: PayloadAction<{ slot: string; id: string; text: string }>) {
      const a = getSlotSubs(state, action.payload.slot)?.[action.payload.id]
      if (a) {
        a.streaming += action.payload.text
        if (a.streaming.length > 50_000) {
          a.streaming = '…(truncated)\n' + a.streaming.slice(-40_000)
        }
      }
    },
    sseSubagentTool(state, action: PayloadAction<{ slot: string; id: string; tool: string }>) {
      const a = getSlotSubs(state, action.payload.slot)?.[action.payload.id]
      if (a) { a.lastTool = action.payload.tool; a.status = 'tool' }
    },
    sseSubagentDone(state, action: PayloadAction<{ slot: string; id: string; elapsed: number; error?: string; task?: string; agent?: string; result?: string }>) {
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const a = subs[action.payload.id]
      if (a) {
        a.status = action.payload.error ? 'error' : 'done'
        a.elapsed = action.payload.elapsed
        a.error = action.payload.error
        a.streaming = ''
      }
      else { subs[action.payload.id] = { id: action.payload.id, task: action.payload.task || '', agent: action.payload.agent || 'kiroclaw', status: action.payload.error ? 'error' : 'done', streaming: '', lastTool: '', startedAt: Date.now() - action.payload.elapsed * 1000, elapsed: action.payload.elapsed, error: action.payload.error } }
    },
    sseSideResult(state, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean }>) {
      const { slot, run_id, role, content, ts, is_error, final } = action.payload
      const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
      // Intentional re-open (new user frame) clears the closed sentinel
      if (role === 'user' && state.slotSideClosed[slot]) {
        delete state.slotSideClosed[slot]
      }
      // Block late assistant chunks after sideClose
      if (!state.slotSide[slot] && state.slotSideClosed[slot]) return
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[slot] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: tsIso }
      }
      const side: SideState = state.slotSide[slot]
      if (role === 'user') {
        // Reconcile with optimistic bubble appended in sideOptimisticAppend.
        const lastUser = side.messages[side.messages.length - 1]
        if (lastUser?.role === 'user' && lastUser.content === content && !lastUser.run_id) {
          lastUser.run_id = run_id
          lastUser.ts = tsIso
        } else {
          side.messages.push({ role: 'user', content, ts: tsIso, run_id })
        }
        side.lastRunId = run_id
        side.pending = true
        side.streaming = true
        return
      }
      side.pending = false
      side.streaming = !final
      if (is_error) {
        side.messages.push({ role: 'assistant', content, ts: tsIso, run_id, is_error: true })
        side.lastRunId = run_id
        return
      }
      const last = side.messages[side.messages.length - 1]
      if (last?.role === 'assistant' && last.run_id === run_id && !last.is_error) {
        if (content === last.content) return
        last.content = content.startsWith(last.content) ? content : last.content + content
        last.ts = tsIso
        return
      }
      side.messages.push({ role: 'assistant', content, ts: tsIso, run_id })
      side.lastRunId = run_id
    },
    sideClose(state, action: PayloadAction<string>) {
      delete state.slotSide[action.payload]
      state.slotSideClosed[action.payload] = true
    },
    sideOptimisticAppend(state, action: PayloadAction<{ slot: string; message: SideMessage }>) {
      const { slot, message } = action.payload
      if (state.slotSideClosed[slot]) delete state.slotSideClosed[slot]
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[slot] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: message.ts }
      }
      const side = state.slotSide[slot]
      side.messages.push(message)
      side.pending = true
    },
    sideOptimisticRollback(state, action: PayloadAction<string>) {
      const side = state.slotSide[action.payload]
      if (!side) return
      const last = side.messages[side.messages.length - 1]
      if (last?.role === 'user') side.messages.pop()
      side.pending = false
    },
    sseSubagentSnapshot(state, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number }>) {
      const d = action.payload
      const subs = d.slot && d.slot !== state.activeSlot
        ? (state.slotActivity[d.slot] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[d.id]
      subs[d.id] = {
        id: d.id, task: d.task, agent: d.agent || 'kiroclaw',
        status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
        startedAt: d.started * 1000, elapsed: 0,
        approval_id: existing?.approval_id, approving: existing?.approving,
      }
    },
    sseChatMessageUpdate(state, action: PayloadAction<{ slot: string; tool_call_id?: string; ts?: string; content?: string; meta?: Record<string, unknown> }>) {
      const { slot, tool_call_id: tcid, ts, content, meta } = action.payload
      if (!slot) return

      if (tcid) {
        const updateByTcid = (msgs: ChatMessage[]) => {
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            const mMeta = m.meta as Record<string, unknown> | undefined
            if (m.role === 'tool' && mMeta?.tool_call_id === tcid) {
              if (content !== undefined) m.content = content
              if (meta) m.meta = { ...(mMeta || {}), ...meta }
              break
            }
          }
        }
        if (slot === state.activeSlot) updateByTcid(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) updateByTcid(cached)
      } else if (ts) {
        const apply = (msgs: ChatMessage[]) => {
          const idx = msgs.findIndex(m => m.ts === ts)
          if (idx < 0) return
          const target = msgs[idx]
          if (meta) target.meta = { ...(target.meta || {}), ...meta }
          if (content !== undefined) target.content = content
        }
        if (slot === state.activeSlot) apply(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) apply(cached)
      }
    },
    sseToolActivity(state, action: PayloadAction<{ slot: string; tool: string; kind: string; purpose: string; input_preview: string; auto?: boolean; tool_call_id?: string; is_update?: boolean }>) {
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      // claude-agent-acp emits an initial tool_call with empty rawInput followed
      // by tool_call_update notifications carrying the populated payload. The
      // backend sets is_update:true on the second-phase event so we merge into
      // the existing entry by tool_call_id. We gate strictly on is_update to
      // avoid silently merging a replayed initial event (e.g. WebSocket
      // reconnect) into an unrelated tool with a colliding id.
      const tcid = action.payload.tool_call_id
      if (tcid && action.payload.is_update) {
        const existing = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
        if (existing) {
          if (action.payload.tool) existing.text = action.payload.tool
          if (action.payload.purpose) existing.purpose = action.payload.purpose
          if (action.payload.input_preview) existing.input = action.payload.input_preview
          existing.ts = Date.now()
          return
        }
      }
      log.push({ type: 'tool', text: action.payload.tool, purpose: action.payload.purpose, input: action.payload.input_preview, ts: Date.now(), auto: action.payload.auto, tool_call_id: action.payload.tool_call_id })
      if (log.length > 100) log.splice(0, log.length - 100)
    },
    sseActivityEvent(state, action: PayloadAction<{ slot: string; kind: string; text: string; approval_id?: string; approval_type?: string }>) {
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      if (action.payload.kind === 'approval_resolved') {
        const id = action.payload.approval_id
        const entry = log.find(e => e.type === 'approval' && e.approval_id === id)
        if (entry) entry.type = 'approval_resolved'
        // Also mark the permission message as resolved so ApprovalBar hides it
        const msg = state.messages.findLast(m => m.role === 'permission' && (m.meta as Record<string,unknown>)?.approval_id === id)
        if (msg && !(msg.meta as Record<string,unknown>).resolved) (msg.meta as Record<string,unknown>).resolved = 'approved'
        return
      }
      const entry: ToolActivity = { type: action.payload.kind, text: action.payload.text, ts: Date.now() }
      if (action.payload.approval_id) entry.approval_id = action.payload.approval_id
      if (action.payload.approval_type) entry.approval_type = action.payload.approval_type
      log.push(entry)
    },
    sseToolResult(state, action: PayloadAction<{ slot: string; output: string; tool_call_id?: string }>) {
      const log = action.payload.slot !== state.activeSlot
        ? state.slotActivity[action.payload.slot]?.toolLog
        : state.toolLog
      if (!log) return
      const tid = action.payload.tool_call_id
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].type === 'tool' && (!tid || log[i].tool_call_id === tid || !log[i].tool_call_id)) {
          log[i].output = action.payload.output; break
        }
      }
    },
    /** Handle chat messages pushed via global SSE/WS (works after refresh). */
    /** Accumulate streamed model reasoning (`chat_thinking` WS event) into a
     *  single content-bearing `thinking`-role message for the current turn.
     *  Reasoning normally arrives before the visible answer, so the block sits
     *  above the streamed assistant text. Scans back to the turn boundary (the
     *  last user message) to keep one reasoning block per turn. */
    sseThinkingChunk(state, action: PayloadAction<{ slot: string; content: string }>) {
      const { slot, content } = action.payload
      if (slot !== state.activeSlot || !content) return
      for (let i = state.messages.length - 1; i >= 0; i--) {
        if (state.messages[i].role === 'thinking') { state.messages[i].content += content; return }
        if (state.messages[i].role === 'user') break
      }
      state.messages.push({ role: 'thinking', content, cls: '' })
    },
    sseChatMessage(state, action: PayloadAction<{ slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string }>) {
      const { slot, role, content, ts, seq, cls, meta, kind } = action.payload
      if (slot !== state.activeSlot) return
      // stop_event — replace in place by id, or insert new
      const effectiveKind = kind ?? (meta?.kind as string | undefined)
      if (effectiveKind === 'stop_event') {
        const id = (meta?.id as string) ?? ''
        const idx = id ? state.messages.findIndex(m => m.meta?.id === id) : -1
        const msg: ChatMessage = { role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' }
        if (idx >= 0) { state.messages[idx] = msg } else { state.messages.push(msg) }
        return
      }
      // WS segment — finalize streaming into assistant without resetting sequence or slot state
      if (role === '_segment') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            state.messages[i].role = 'assistant'
            state.messages[i].rawText = state.messages[i].content
            break
          }
        }
        return
      }
      // WS chunk — accumulate into streaming message, preserve rawText
      if (role === 'chunk') {
        state.slotState = 'streaming'
        state._wsChunkedDuringFetch = true
        // Drop only the empty "Thinking…" placeholder; keep content-bearing
        // reasoning blocks (from chat_thinking) so they persist as a collapsible
        // trace directly above the streamed answer.
        if (state.messages.some(m => m.role === 'thinking' && !m.content)) {
          state.messages = state.messages.filter(m => !(m.role === 'thinking' && !m.content))
        }
        // Accumulate reasoning text into activity timeline
        const last = state.toolLog[state.toolLog.length - 1]
        if (last?.type === 'reasoning') {
          last.text += content
        } else {
          state.toolLog.push({ type: 'reasoning', text: content, ts: Date.now() })
        }
        let streamIdx = -1
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') { streamIdx = i; break }
        }
        if (streamIdx >= 0) {
          const msg = state.messages[streamIdx]
          // Detect missed chunks via sequence gap
          if (seq !== undefined && state.lastChunkSeq !== undefined && seq > state.lastChunkSeq + 1) {
            msg.content += `\n[⚠ ${seq - state.lastChunkSeq - 1} chunk(s) missed]\n`
          }
          msg.content += content
          msg.rawText = msg.content
        } else {
          state.messages.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content })
        }
        if (seq !== undefined) state.lastChunkSeq = seq
        return
      }
      // WS done — finalize streaming into assistant, rawText preserved for reparse
      if (role === '_done') {
        state.slotState = 'idle'
        state.lastChunkSeq = undefined
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            const msg = state.messages[i]
            msg.role = 'assistant'
            msg.rawText = msg.content
            break
          }
        }
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        state.pendingTurnSlot = null
        return
      }
      // Compacting — block input, show footer indicator (no visible message)
      if (role === 'compacting') {
        if (action.payload.slot && action.payload.slot !== state.activeSlot) return
        state.slotState = 'compacting'
        state.slotRunning = true
        return
      }
      // Tool call — update state, insert before streaming message
      if (role === 'tool') {
        state.slotState = 'tool_running'
        // Insert tool before any trailing streaming message so
        // chat_segment can still find and finalize it with redacted text.
        let insertIdx = state.messages.length
        if (insertIdx > 0 && state.messages[insertIdx - 1]?.role === 'streaming') {
          insertIdx--
        }
        state.messages.splice(insertIdx, 0, { role, content, cls: cls || '', ts, meta })
        return
      }
      // Thinking — deduplicate, only keep one
      if (role === 'thinking') {
        if (state.messages.some(m => m.role === 'thinking')) return
        state.messages.push({ role: 'thinking', content: '', cls: '' })
        return
      }
      // Replace streaming placeholder with final assistant message
      if (role === 'assistant') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            state.messages[i].role = 'assistant'; state.messages[i].content = content; if (ts) state.messages[i].ts = ts
            return
          }
        }
      }
      // New user message = new turn — clear activity log
      if (role === 'user') {
        state.toolLog = []
        // Auto-resolve any stale permissions from previous turn so they don't block the new turn
        for (const m of state.messages) {
          if (m.role === 'permission' && !m.meta?.resolved) {
            if (m.meta) m.meta.resolved = 'rejected'
            else m.meta = { resolved: 'rejected' }
          }
        }
      }
      // Permission messages carry request_id/tool_input in cls (JSON) — lift into meta
      let effectiveMeta = meta
      if (role === 'permission' && !meta?.approval_id && cls) {
        try {
          const parsed = JSON.parse(cls)
          if (parsed.request_id) {
            effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
          }
        } catch { /* not JSON cls, ignore */ }
      }
      // If this permission's tool was already rejected/stopped, mark it resolved immediately
      if (role === 'permission') {
        const tcid = (effectiveMeta?.tool_call_id as string) || ''
        if (tcid) {
          const entry = state.toolLog.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
          if (entry?.rejected) effectiveMeta = { ...effectiveMeta, resolved: 'rejected' }
        }
      }
      state.messages.push({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind })
    },
    /** Patch an existing message identified by ts. Used by the `chat_message_update`
     * server event to flip an mcp_oauth banner from "needs auth" to "authenticated"
     * after kiro-cli emits server_initialized. Patches both the active messages
     * array and the slotMessages cache so a slot the user isn't currently
     * viewing still shows the correct banner state on switch-back. */
    sseChatMessagePatchByTs(state, action: PayloadAction<{ slot: string; ts: string; meta?: Record<string, unknown>; content?: string }>) {
      const { slot, ts, meta, content } = action.payload
      if (!slot || !ts) return
      const apply = (msgs: ChatMessage[]) => {
        const idx = msgs.findIndex(m => m.ts === ts)
        if (idx < 0) return
        const target = msgs[idx]
        if (meta) target.meta = { ...(target.meta || {}), ...meta }
        if (content !== undefined) target.content = content
      }
      if (slot === state.activeSlot) apply(state.messages)
      const cached = state.slotMessages[slot]
      if (cached) apply(cached)
    },
    /** Remove the first queued message matching content and append a user bubble at the end. */
    removeQueuedMessage(state, action: PayloadAction<{ slot: string; content: string; queue_id?: string }>) {
      if (action.payload.slot !== state.activeSlot) return
      const idx = action.payload.queue_id
        ? state.messages.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === action.payload.queue_id)
        : state.messages.findIndex(m => m.role === 'queued' && m.content === action.payload.content)
      if (idx >= 0) {
        const ts = state.messages[idx].ts
        state.messages.splice(idx, 1)
        state.messages.push({ role: 'user', content: action.payload.content, cls: 'msg msg-u', ts })
      }
    },
    /** Cancel a queued message: remove from messages. pendingInput is set locally by the initiating client. */
    cancelQueuedMessage(state, action: PayloadAction<{ slot: string; queue_id: string }>) {
      if (action.payload.slot !== state.activeSlot) return
      const idx = state.messages.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === action.payload.queue_id)
      if (idx >= 0) state.messages.splice(idx, 1)
    },
    /** Add a queued message (from backend queue_push WS event). */
    appendQueuedMessage: {
      reducer(state, action: PayloadAction<{ slot: string; content: string; ts: string; queueId: string }>) {
        if (action.payload.slot !== state.activeSlot) return
        state.messages.push({ role: 'queued', content: action.payload.content, cls: 'msg msg-queued', ts: action.payload.ts, meta: { queueId: action.payload.queueId } })
      },
      prepare(payload: { slot: string; content: string; ts: string; queue_id?: string }) {
        return { payload: { ...payload, queueId: payload.queue_id || crypto.randomUUID() } }
      },
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchHistory.fulfilled, (state, action) => {
        const { sessions, hasMore, offset, append } = action.payload
        state.history = append ? [...state.history, ...sessions] : sessions
        state.historyHasMore = hasMore
        state.historyOffset = offset + sessions.length
      })
      .addCase(switchSlot.pending, (state, action) => {
        // Save current slot's activity
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab }
        }
        // Cache current slot's messages before switching
        if (state.activeSlot && state.messages.length > 0) {
          state.slotMessages[state.activeSlot] = state.messages
        }
        // Always strip target from history: activeSlot ∉ slotHistory
        state.slotHistory = state.slotHistory.filter(k => k !== action.meta.arg)
        if (state.activeSlot && state.activeSlot !== action.meta.arg) {
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        // Restore target slot's activity (or empty)
        const cached = state.slotActivity[action.meta.arg]
        state.toolLog = cached?.toolLog ?? []
        state.subagents = cached?.subagents ?? {}
        // 'tools' tab was removed in May 2026 (inline expansion replaces it). Cached
        // pre-migration values fall back to 'files'.
        state.activityTab = (cached?.activityTab && cached.activityTab !== ('tools' as never)) ? cached.activityTab : 'files'
        // Set activeSlot immediately so WS events for the new slot are accepted.
        // Restore cached messages if available (instant switch), otherwise show loading.
        state.activeSlot = action.meta.arg
        const cachedMsgs = state.slotMessages[action.meta.arg]
        if (cachedMsgs) {
          state.messages = cachedMsgs
          state.slotLoading = false
        } else {
          state.messages = []
          state.slotLoading = true
        }
        state._wsChunkedDuringFetch = false
      })
      .addCase(switchSlot.fulfilled, (state, action) => {
        const { key, messages, running, hasMore, total, queue } = action.payload
        if (state.activeSlot !== key) return  // user switched away during fetch
        state.slotState = running ? 'streaming' : 'idle'
        // Mark stale permissions as resolved so ApprovalBar ignores them
        if (!running) {
          for (const m of messages) {
            if (m.role === 'permission' && !m.meta?.resolved) m.meta = { ...m.meta, resolved: 'stale' }
          }
        }
        // If WS already delivered newer streaming content, append it to fetched messages
        const lastLocal = state.messages[state.messages.length - 1]
        const preserved = mergePreservedPastes(state.messages, messages)
        if (
          state._wsChunkedDuringFetch
          && lastLocal?.role === 'streaming'
          && lastLocal.content.length > 0
        ) {
          // WS chunks arrived during fetch — use fetched history + local streaming
          state.messages = [...preserved.filter(m => m.role !== 'streaming'), lastLocal]
        } else {
          state.messages = preserved
        }
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        state.slotHasMore = hasMore
        state.slotOldestIndex = hasMore ? total - messages.length : 0
        // Hydrate queued messages from backend queue field
        // Clear any WS-delivered queued messages first to avoid duplicates
        // (a queue_push WS event may have arrived during the HTTP fetch)
        state.messages = state.messages.filter(m => m.role !== 'queued')
        for (const { content, queueId, ts } of queue) {
          state.messages.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
        }
        // Update cache and clear loading state
        state.slotMessages[key] = state.messages
        state.slotLoading = false
      })
      .addCase(switchSlot.rejected, (state, action) => {
        if (state.activeSlot !== action.meta.arg) return
        state.messages = []
        state.slotRunning = false
        state.slotStopping = false
        state.slotHasMore = false
        state.slotOldestIndex = 0
        state.slotLoading = false
      })
      .addCase(refreshSlot.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, running, hasMore, total } = action.payload
        if (state.activeSlot !== key) return  // user switched away
        // Merge permission messages: prefer state perms (have frontend resolved flags)
        // but include API perms for any we don't have locally (e.g. arrived while disconnected)
        const statePerms = new Map<string, typeof state.messages[0]>()
        for (const m of state.messages) {
          if (m.role === 'permission' && m.meta?.approval_id) statePerms.set(m.meta.approval_id as string, m)
        }
        const apiPerms = messages.filter(m => m.role === 'permission')
        for (const m of apiPerms) {
          const aid = m.meta?.approval_id as string | undefined
          if (aid && !statePerms.has(aid)) statePerms.set(aid, m)
        }
        const tsNum = (v: unknown): number => {
          const s = v == null ? '' : String(v)
          if (!s) return 0
          const n = Number(s)
          if (Number.isFinite(n)) return n  // numeric epoch
          const p = Date.parse(s)
          return Number.isFinite(p) ? p / 1000 : 0  // ISO → epoch seconds
        }
        const merged = [...messages.filter(m => m.role !== 'permission'), ...statePerms.values()]
        const mergedWithPastes = mergePreservedPastes(state.messages, merged)
        // Only sort if permissions were re-injected (they need positional merge).
        // Backend messages arrive in order; sorting with mixed ts formats reorders them.
        const sorted = statePerms.size > 0
          ? mergedWithPastes.sort((a, b) => tsNum(a.ts) - tsNum(b.ts))
          : mergedWithPastes
        // Reasoning is client-only (never persisted server-side); re-insert it so
        // a finished turn's thinking block survives this refresh.
        state.messages = mergePreservedThinking(state.messages, sorted)
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        state.slotHasMore = hasMore
        state.slotOldestIndex = hasMore ? total - messages.length : 0
      })
      .addCase(createSlot.fulfilled, (state, action) => {
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab }
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        state.activeSlot = action.payload.key
        state.messages = []
        state.toolLog = []
        state.subagents = {}
        state.activityTab = 'files'
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        state.slotHasMore = false
        state.slotOldestIndex = 0
      })
      .addCase(deleteSlot.fulfilled, (state, action) => {
        delete state.slotActivity[action.payload]
        delete state.slotMessages[action.payload]
        delete state.slotSide[action.payload]
        delete state.slotSideClosed[action.payload]
        state.slotHistory = state.slotHistory.filter(k => k !== action.payload)
        if (state.activeSlot === action.payload) {
          state.activeSlot = null
          state.messages = []
          state.toolLog = []
          state.subagents = {}
        }
      })
      .addCase(resumeFromHistory.fulfilled, (state, action) => {
        if (action.payload.ok) {
          state.slotHistory = state.slotHistory.filter(k => k !== action.payload.key)
          if (state.activeSlot) {
            state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab }
            if (state.activeSlot !== action.payload.key) {
              state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
            }
          }
          const cached = state.slotActivity[action.payload.key]
          state.toolLog = cached?.toolLog ?? []
          state.subagents = cached?.subagents ?? {}
          // 'tools' tab was removed (inline expansion replaces it). Cached pre-migration values fall back to 'files'.
          state.activityTab = (cached?.activityTab && cached.activityTab !== ('tools' as never)) ? cached.activityTab : 'files'
          state.activeSlot = action.payload.key
          state.messages = mergePreservedPastes(state.messages, action.payload.messages)
          state.slotState = 'idle'
          state.pendingTurnSlot = null
          state.slotHasMore = action.payload.hasMore
          state.slotOldestIndex = action.payload.hasMore ? action.payload.total - action.payload.messages.length : 0
        }
      })
      .addCase(deleteHistorySession.fulfilled, (state, action) => {
        state.history = state.history.filter(s => s.key !== action.payload)
      })
      .addCase(loadOlderMessages.pending, (state) => {
        state.loadingOlder = true
      })
      .addCase(loadOlderMessages.fulfilled, (state, action) => {
        state.loadingOlder = false
        if (action.payload) {
          // Merge paste state into the older messages first, then prepend so
          // historical pastes re-tokenize from localStorage instead of showing
          // as fully-expanded text.
          const merged = mergePreservedPastes(state.messages, action.payload.messages)
          state.messages = [...merged, ...state.messages]
          state.slotHasMore = action.payload.hasMore
          state.slotOldestIndex = action.payload.hasMore ? action.payload.total - state.messages.length : 0
        }
      })
      .addCase(loadOlderMessages.rejected, (state) => {
        state.loadingOlder = false
      })
  },
})

export const {
  setActiveSlot, clearSlotState, setPendingInput, setQuestionCard, clearQuestionCard, appendMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, startLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, truncateAfterIndex, replaceMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityToTool, clearFocusToolCallId, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentChunk, sseSubagentTool, sseSubagentDone,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseSideResult, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions
export default chatSlice.reducer
