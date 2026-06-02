import type { ChatMessage } from '../../types'

export const SOFT_STOP_DEBOUNCE_MS = 150

export type TurnItem =
  | { kind: 'single'; msg: ChatMessage; idx: number }
  | { kind: 'group'; msgs: ChatMessage[]; startIdx: number }

export type DisplayItem =
  | TurnItem
  | { kind: 'turn'; items: TurnItem[]; complete: boolean }
