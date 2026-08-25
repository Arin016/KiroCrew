/**
 * The tab a Connect click opens, before anyone knows where it should go.
 *
 * A browser only grants `window.open` while it can still attribute the call to
 * a user gesture. The approval URL is minted by a background runtime session
 * and arrives seconds later over the mint poll, so opening the tab *when the
 * URL lands* is opening it outside the gesture — every browser blocks that as
 * an unrequested popup, which is what forced Connect to be a two-step
 * "click, then click the link" flow.
 *
 * So the tab is opened SYNCHRONOUSLY in the click handler, on `about:blank`,
 * showing a translated placeholder; the poll then points that already-open tab
 * at the approval URL. Nothing in this module may be deferred behind an
 * `await` on the open path.
 */

import { placeholderHtml, type ApprovalTabCopy } from './approvalTabDocument'

export type { ApprovalTabCopy }

/** How long a placeholder tab waits for a URL before giving up on the mint. */
export const APPROVAL_TAB_TIMEOUT_MS = 30_000
/** How long the failure message stays readable before the tab closes itself. */
export const APPROVAL_TAB_CLOSE_DELAY_MS = 4_000
/** Mint-poll cadence while at least one placeholder tab is waiting. */
export const PENDING_TAB_POLL_MS = 1_000

/**
 * The slice of the opened window this module drives.
 *
 * Structural rather than `Window`, because the handle is always cross-checked
 * for `closed` and only ever written through these four members — and because a
 * test needs to be able to stand one up without a real browsing context.
 */
export interface ApprovalTabWindow {
  readonly closed: boolean
  close(): void
  location: { href: string }
  document: { open(): void; write(html: string): void; close(): void }
  opener?: unknown
}

export interface ApprovalTab {
  window: ApprovalTabWindow
  /** Epoch ms of the click, so the wait can be bounded without a timer. */
  openedAt: number
}

function paint(target: ApprovalTabWindow, copy: ApprovalTabCopy): void {
  try {
    target.document.open()
    target.document.write(placeholderHtml(copy))
    target.document.close()
  } catch {
    // The tab may already have navigated away from the same-origin blank
    // document, which makes it unwritable. The flow does not depend on the
    // placeholder rendering, so a failure here is cosmetic.
  }
}

/**
 * Open the approval tab. MUST be called synchronously inside the click handler.
 *
 * Returns `null` when the browser refused (popup blocker, headless context).
 * A refusal is not an error: the card still shows the approval link, so the
 * user keeps a way through.
 */
export function openApprovalTab(
  copy: ApprovalTabCopy,
  now: number = Date.now(),
): ApprovalTab | null {
  // No `noopener`: that returns null, and the handle is the whole point — it is
  // how the tab gets pointed at the approval URL once the mint answers.
  const opened = globalThis.window?.open?.('', '_blank') as ApprovalTabWindow | null | undefined
  if (!opened) return null
  paint(opened, copy)
  try {
    // The provider's consent page has no business reaching back into the
    // dashboard, and it never needs to: the one-time code goes to the runtime's
    // loopback listener, not through this tab. Severing the back-reference
    // while the document is still same-origin is the only chance to do it.
    opened.opener = null
  } catch {
    // Read-only in some engines; the tab is still ours to navigate.
  }
  return { window: opened, openedAt: now }
}

/** Point an open placeholder tab at the approval URL. */
export function navigateApprovalTab(tab: ApprovalTab, url: string): boolean {
  if (!url || tab.window.closed) return false
  try {
    tab.window.location.href = url
    return true
  } catch {
    return false
  }
}

/**
 * Tell the user the mint failed, then close the tab.
 *
 * Closing immediately would look like the click did nothing; leaving it open on
 * a dead placeholder is worse. So the message is readable for a beat first.
 */
export function failApprovalTab(
  tab: ApprovalTab,
  copy: ApprovalTabCopy,
  closeDelayMs: number = APPROVAL_TAB_CLOSE_DELAY_MS,
): void {
  if (tab.window.closed) return
  paint(tab.window, copy)
  setTimeout(() => closeApprovalTab(tab), closeDelayMs)
}

/** Close a placeholder tab we opened (Cancel, or a resolved flow). */
export function closeApprovalTab(tab: ApprovalTab): void {
  try {
    if (!tab.window.closed) tab.window.close()
  } catch {
    // Already gone, or the user detached it. Nothing left to do.
  }
}

/**
 * Tell the user there was nothing to approve, then close the tab.
 *
 * Same shape as `failApprovalTab` — a terminal message held readable for a beat
 * — because a tab that vanishes the instant it opens reads as a broken click.
 */
export function resolveApprovalTab(
  tab: ApprovalTab,
  copy: ApprovalTabCopy,
  closeDelayMs: number = APPROVAL_TAB_CLOSE_DELAY_MS,
): void {
  failApprovalTab(tab, copy, closeDelayMs)
}

/** What the mint poll tells a waiting tab to do. */
export type ApprovalTabOutcome = 'navigate' | 'granted' | 'fail' | 'wait'

/**
 * Decide a waiting tab's fate from the newest mint verdict.
 *
 * `granted` beats the rest: the provider already holds a grant, so there is
 * nothing to approve and any URL still on the card would send the user to a
 * consent page for an authorization they have.
 *
 * `mintFailed` is a mint that reached a terminal state without producing a
 * redeemable URL — a run that failed, one that expired, or a row this tab's
 * attempt no longer owns. Every one of those is final for THIS tab, which is
 * why it is one flag and not three: the placeholder has nothing left to wait
 * for. A merely absent or still-minting row is NOT that, and is worth waiting
 * out, so it falls through to the deadline below.
 *
 * The caller supplies these three facts from the mint row (see
 * `approvalTabView`); this function stays a pure decision over them so the
 * ordering can be pinned without a browsing context.
 */
export function approvalTabOutcome(
  live: { oauthUrl?: string; mintFailed?: boolean; granted?: boolean } | undefined,
  elapsedMs: number,
): ApprovalTabOutcome {
  if (live?.granted) return 'granted'
  if (live?.oauthUrl) return 'navigate'
  if (live?.mintFailed) return 'fail'
  return elapsedMs >= APPROVAL_TAB_TIMEOUT_MS ? 'fail' : 'wait'
}
