import type { DisplayItem } from '../pages/chat/types'

/**
 * Given the list of displayItems rendered by Virtuoso and the topmost
 * visible item index, return the displayItem index of the nearest user
 * message strictly above the viewport top edge.
 *
 * Returns -1 if there is no user message above (i.e. the viewport is at
 * or above the first user turn).
 *
 * User messages always appear as top-level `single` items in displayItems
 * because the turn builder in `ChatPage.tsx` pushes user messages directly
 * to `turns[]` at the boundary between turns. `turn` blocks and `group`
 * items never contain a user message.
 */
export function findPrevUserMsgDisplayIdx(
  displayItems: DisplayItem[],
  topmostVisibleIdx: number,
): number {
  for (let i = Math.min(topmostVisibleIdx - 1, displayItems.length - 1); i >= 0; i--) {
    const item = displayItems[i]
    if (item.kind === 'single' && item.msg.role === 'user') return i
  }
  return -1
}
