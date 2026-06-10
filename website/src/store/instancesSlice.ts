/**
 * instancesSlice — shared client state for the multi-instance header switcher.
 *
 * `warm` holds the in-memory loopback port + minted token for each connected
 * instance whose iframe is kept mounted (hide-not-unmount). It is NEVER
 * persisted and never logged — it lives only for the dashboard session, exactly
 * like the previous page-local state. The header tab strip writes it (on
 * connect) and the viewport reads it (to build iframe src + render).
 *
 * `activeId` is the instance currently filling the page body, or `null` for the
 * native dashboard (the "Local" tab). `mru` is recency order (front = most
 * recent) for K-cap eviction. `unread` is the validated postMessage relay count.
 */
import { createSlice, type PayloadAction } from '@reduxjs/toolkit'

export interface WarmConn {
  port: number
  token: string
}

interface InstancesState {
  warm: Record<string, WarmConn>
  activeId: string | null
  mru: string[]
  unread: Record<string, number>
}

const initialState: InstancesState = {
  warm: {},
  activeId: null,
  mru: [],
  unread: {},
}

const instancesSlice = createSlice({
  name: 'instances',
  initialState,
  reducers: {
    setWarm(state, action: PayloadAction<{ id: string; conn: WarmConn }>) {
      state.warm[action.payload.id] = action.payload.conn
      state.mru = [action.payload.id, ...state.mru.filter(x => x !== action.payload.id)]
    },
    setActiveId(state, action: PayloadAction<string | null>) {
      state.activeId = action.payload
      if (action.payload) {
        state.mru = [action.payload, ...state.mru.filter(x => x !== action.payload)]
        // Selecting an instance clears its unread badge.
        if (state.unread[action.payload]) state.unread[action.payload] = 0
      }
    },
    /** Pure client-state teardown for one connection (no API call). */
    removeWarm(state, action: PayloadAction<string>) {
      const id = action.payload
      delete state.warm[id]
      delete state.unread[id]
      state.mru = state.mru.filter(x => x !== id)
      if (state.activeId === id) state.activeId = null
    },
    setUnread(state, action: PayloadAction<{ id: string; count: number }>) {
      state.unread[action.payload.id] = action.payload.count
    },
    clearInstances() {
      return initialState
    },
  },
})

export const { setWarm, setActiveId, removeWarm, setUnread, clearInstances } = instancesSlice.actions
export default instancesSlice.reducer
