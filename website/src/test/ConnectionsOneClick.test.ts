/**
 * The one-click approval tab, at the seam.
 *
 * The load-bearing property is not tested here but in
 * `ConnectionsPage.oneClick.test.tsx`: that `window.open` runs INSIDE the click
 * gesture. What this file pins is the behaviour that seam depends on — the
 * placeholder document never carries a URL, a waiting tab is navigated rather
 * than reopened, a failed mint gets a message and then a closed tab, and the
 * mint row this branch actually serves is folded into those three inputs
 * correctly.
 */
import { describe, expect, it, vi } from 'vitest'
import {
  APPROVAL_TAB_CLOSE_DELAY_MS,
  APPROVAL_TAB_TIMEOUT_MS,
  PENDING_TAB_POLL_MS,
  approvalTabOutcome,
  closeApprovalTab,
  failApprovalTab,
  navigateApprovalTab,
  openApprovalTab,
  type ApprovalTab,
  type ApprovalTabWindow,
} from '../pages/connections/oneClickTab'
import { escapeHtml, placeholderHtml } from '../pages/connections/approvalTabDocument'
import { approvalTabView, type PendingConnect } from '../pages/connections/ConnectionsPage'
import type { ConnectionMintState } from '../api/client'

const COPY = { title: 'Opening the approval page', message: 'Preparing…', lang: 'en' }
const AUTHORIZE_URL = 'https://mcp.notion.com/authorize?state=abc'

interface FakeTabWindow extends ApprovalTabWindow {
  closed: boolean
  written: string[]
  closeCalls: number
}

function fakeTabWindow(): FakeTabWindow {
  const win: FakeTabWindow = {
    closed: false,
    written: [],
    closeCalls: 0,
    close() {
      win.closeCalls++
      win.closed = true
    },
    location: { href: '' },
    document: {
      open: () => undefined,
      write: (html: string) => void win.written.push(html),
      close: () => undefined,
    },
  }
  return win
}

const tabFor = (win: ApprovalTabWindow, openedAt = 0): ApprovalTab => ({ window: win, openedAt })

describe('placeholder document', () => {
  it('escapes translated copy so no value can inject markup', () => {
    expect(escapeHtml('<b>"x" & \'y\'</b>')).toBe(
      '&lt;b&gt;&quot;x&quot; &amp; &#39;y&#39;&lt;/b&gt;',
    )
    const html = placeholderHtml({ ...COPY, title: '<script>bad()</script>' })
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('carries the translated copy and the document language', () => {
    const html = placeholderHtml({ title: 'Titre', message: 'Préparation', lang: 'fr' })
    expect(html).toContain('<html lang="fr">')
    expect(html).toContain('Titre')
    expect(html).toContain('Préparation')
  })

  it('never writes a URL into the document', () => {
    // The approval URL reaches the tab through `location`, so it never becomes
    // markup, link text or a referrer.
    const html = placeholderHtml(COPY)
    expect(html).not.toMatch(/https?:\/\//)
    expect(html).toContain('content="no-referrer"')
  })
})

describe('openApprovalTab', () => {
  it('asks for a real handle, paints the placeholder, and severs the opener', () => {
    const win = fakeTabWindow()
    const open = vi.fn().mockReturnValue(win)
    vi.spyOn(window, 'open').mockImplementation(open as unknown as typeof window.open)
    try {
      const tab = openApprovalTab(COPY, 1234)

      expect(tab).not.toBeNull()
      expect(tab?.openedAt).toBe(1234)
      // No `noopener`: that returns null, and the handle is the whole point.
      expect(open).toHaveBeenCalledWith('', '_blank')
      expect(win.written.join('')).toContain('Opening the approval page')
      // The consent page has no business reaching back into the dashboard.
      expect(win.opener).toBeNull()
    } finally {
      vi.restoreAllMocks()
    }
  })

  it('treats a blocked popup as a non-error so the card fallback still applies', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      expect(openApprovalTab(COPY)).toBeNull()
    } finally {
      vi.restoreAllMocks()
    }
  })
})

describe('navigateApprovalTab', () => {
  it('points an open tab at the approval URL', () => {
    const win = fakeTabWindow()
    expect(navigateApprovalTab(tabFor(win), AUTHORIZE_URL)).toBe(true)
    expect(win.location.href).toBe(AUTHORIZE_URL)
  })

  it('refuses a closed tab or an empty URL rather than throwing', () => {
    const closed = fakeTabWindow()
    closed.closed = true
    expect(navigateApprovalTab(tabFor(closed), AUTHORIZE_URL)).toBe(false)
    expect(navigateApprovalTab(tabFor(fakeTabWindow()), '')).toBe(false)
  })
})

describe('failApprovalTab', () => {
  it('shows the message, then closes the tab', () => {
    vi.useFakeTimers()
    try {
      const win = fakeTabWindow()
      failApprovalTab(tabFor(win), { ...COPY, message: 'Could not prepare it.' })

      // Readable first: closing on the spot looks like the click did nothing.
      expect(win.written.join('')).toContain('Could not prepare it.')
      expect(win.closeCalls).toBe(0)

      vi.advanceTimersByTime(APPROVAL_TAB_CLOSE_DELAY_MS)
      expect(win.closeCalls).toBe(1)
      expect(win.closed).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves an already-closed tab alone', () => {
    const win = fakeTabWindow()
    win.closed = true
    failApprovalTab(tabFor(win), COPY)
    expect(win.written).toEqual([])
  })
})

describe('closeApprovalTab', () => {
  it('closes an open tab and tolerates one that is already gone', () => {
    const win = fakeTabWindow()
    closeApprovalTab(tabFor(win))
    expect(win.closeCalls).toBe(1)
    closeApprovalTab(tabFor(win))
    expect(win.closeCalls).toBe(1)
  })
})

describe('approvalTabOutcome', () => {
  it('navigates as soon as the mint delivers a URL', () => {
    expect(approvalTabOutcome({ oauthUrl: AUTHORIZE_URL }, 0)).toBe('navigate')
  })

  it('fails on a mint that reached a terminal state with nothing to redeem', () => {
    expect(approvalTabOutcome({ mintFailed: true }, 0)).toBe('fail')
  })

  it('keeps waiting on a merely stale mint, which a fresh one can still satisfy', () => {
    expect(approvalTabOutcome({}, 0)).toBe('wait')
    expect(approvalTabOutcome(undefined, 0)).toBe('wait')
  })

  it('gives up once the wait is longer than the user would tolerate', () => {
    expect(approvalTabOutcome(undefined, APPROVAL_TAB_TIMEOUT_MS - 1)).toBe('wait')
    expect(approvalTabOutcome(undefined, APPROVAL_TAB_TIMEOUT_MS)).toBe('fail')
  })

  it('prefers a delivered URL over a timeout that arrived in the same poll', () => {
    expect(approvalTabOutcome({ oauthUrl: AUTHORIZE_URL }, APPROVAL_TAB_TIMEOUT_MS * 2))
      .toBe('navigate')
  })

  it('lets an already-authorized provider outrank a URL still on the card', () => {
    // A stale URL would send the user to a consent page for a grant they hold.
    expect(approvalTabOutcome({ granted: true }, 0)).toBe('granted')
    expect(approvalTabOutcome({ granted: true, oauthUrl: AUTHORIZE_URL }, 0)).toBe('granted')
    expect(approvalTabOutcome({ granted: true, mintFailed: true }, 0)).toBe('granted')
  })
})

describe('approvalTabView folds the mint row into those three inputs', () => {
  const mint = (over: Partial<ConnectionMintState>): ConnectionMintState =>
    ({ slug: 'notion', state: 'minting', ...over })
  const pending: PendingConnect = { kind: 'new', sinceTs: 0, token: 'tok1' }

  it('serves a URL only from a waiting row', () => {
    expect(approvalTabView(mint({ state: 'waiting', oauth_url: AUTHORIZE_URL }), pending).oauthUrl)
      .toBe(AUTHORIZE_URL)
    // Every other state either has no URL or holds one no redirect can redeem,
    // which is the same rule the card's own link applies.
    expect(approvalTabView(mint({ state: 'expired', oauth_url: AUTHORIZE_URL }), pending).oauthUrl)
      .toBe('')
  })

  it('drops a non-web scheme before the decision, not after it', () => {
    // Sanitized here, a refused URL reads as "still no URL" and the tab keeps
    // waiting. Sanitized downstream it would read as `navigate`, releasing the
    // tab from tracking and then declining to move it — a placeholder orphaned
    // with nothing left to resolve it. It is also the only thing standing between
    // a `javascript:` payload and a tab this page navigates.
    const view = approvalTabView(
      mint({ state: 'waiting', oauth_url: 'javascript:alert(1)' }),
      pending,
    )
    expect(view.oauthUrl).toBe('')
    expect(approvalTabOutcome(view, 0)).toBe('wait')
  })

  it('waits through a row that has not decided anything yet', () => {
    expect(approvalTabView(mint({ state: 'minting' }), pending))
      .toMatchObject({ oauthUrl: '', granted: false, mintFailed: false })
    expect(approvalTabView(undefined, pending))
      .toMatchObject({ granted: false, mintFailed: false })
  })

  it('reads a granted row as nothing left to approve', () => {
    expect(approvalTabView(mint({ state: 'granted' }), pending).granted).toBe(true)
  })

  it('collapses every cleared-but-ungranted wait into one terminal failure', () => {
    // A mint that ran and failed, one that expired, and a row a sibling tab
    // replaced are three different facts about the FLOW and one identical fact
    // about this tab: no verdict is ever coming for it.
    expect(approvalTabView(mint({ state: 'failed' }), pending).mintFailed).toBe(true)
    expect(approvalTabView(mint({ state: 'expired' }), pending).mintFailed).toBe(true)
    expect(approvalTabView(mint({ state: 'waiting', token: 'other' }), pending).mintFailed)
      .toBe(true)
  })

  it('takes a verdict the mint row never observed from the flow itself', () => {
    // A connect can succeed without ever producing a `granted` row (the chat
    // banner reports the grant, or the probe starts answering ok). Without this
    // the tab would wait out its deadline and then claim the approval page had
    // failed for a flow that worked.
    expect(approvalTabView(undefined, pending, { completed: true }).granted).toBe(true)
    expect(approvalTabView(undefined, pending, { failed: true }).mintFailed).toBe(true)
  })

  it('leaves a deadline fail distinguishable from a mint-row fail', () => {
    // The page needs to tell them apart to keep one message per failure: a
    // terminal row is reported on the card by the settlement path in its own
    // words, while a deadline has no other reporter and must be voiced by the tab
    // path. `mintFailed` is exactly that discriminator.
    const noVerdict = approvalTabView(undefined, pending)
    expect(noVerdict.mintFailed).toBe(false)
    expect(approvalTabOutcome(noVerdict, APPROVAL_TAB_TIMEOUT_MS)).toBe('fail')
  })
})

describe('poll cadence with a placeholder tab open', () => {
  it('polls every second while a tab waits — the user is staring at it', () => {
    // Tighter than the idle mint cadence, which is what the page falls back to.
    expect(PENDING_TAB_POLL_MS).toBe(1_000)
  })
})
