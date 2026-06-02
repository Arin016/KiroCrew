import { openActivityToTab } from '../../store/chatSlice'
import { api } from '../../api/client'
import type { AppDispatch } from '../../store'

export type SlashInterceptResult = { intercepted: true } | { intercepted: false }

const SIDE_RE = /^\/side(?:\s+([\s\S]+))?$/

export async function interceptSlashCommand(
  raw: string,
  slot: string | null,
  dispatch: AppDispatch,
): Promise<SlashInterceptResult> {
  const trimmed = raw.trim()
  const match = trimmed.match(SIDE_RE)
  if (!match) {
    return { intercepted: false }
  }
  if (!slot) {
    console.warn('[/side] no active slot — intercepted but not dispatched')
    return { intercepted: true }
  }
  const message = match[1]?.trim() ?? ''
  try {
    await api.sideOpen(slot)
  } catch (e: unknown) {
    console.warn('[/side] sideOpen failed:', e)
    return { intercepted: true }
  }
  dispatch(openActivityToTab('side'))
  if (message) {
    await api.sideTurn(slot, message).catch((e: unknown) => {
      console.warn('[/side] sideTurn failed:', e)
    })
  }
  return { intercepted: true }
}
