/**
 * One-click Connect, at the component.
 *
 * The property under test is a TIMING one: `window.open` has to run while the
 * browser can still attribute it to the click. Every assertion here that reads
 * `openSpy` with no `await` between it and `fireEvent.click` is checking exactly
 * that — a version of this flow that waits for the mint before opening a tab
 * gets its popup blocked, which is the bug this design removes.
 *
 * The mint feed is this branch's own: POST /api/connections/mint starts a row and
 * GET serves its state, so a delivered URL arrives as `{ state: 'waiting',
 * oauth_url }` rather than on the status payload.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { configure, fireEvent, screen, waitFor, within } from '@testing-library/react'

import type { ChatMessage, McpServer, RootState } from '../types'
import type { ApprovalTabWindow } from '../pages/connections/oneClickTab'
import { APPROVAL_TAB_TIMEOUT_MS } from '../pages/connections/oneClickTab'

// One connect here is a multi-hop async chain — write the MCP entry, request the
// mint, then wait out a mint refetch that polls on a 1s cadence while a
// placeholder tab is open. That is already at the RTL default of 1s before any
// scheduling noise, so this file goes flaky when it shares a machine with other
// suites. Waiting longer costs nothing on a passing run, and the setting cannot
// leak: `pool: 'forks'` gives every file its own process.
configure({ asyncUtilTimeout: 8_000 })

const mcpServers = vi.fn()
const mcpProbe = vi.fn()
const mcpApply = vi.fn()
const mcpCustomAdd = vi.fn()
const mcpCustomGet = vi.fn()
const mcpCustomUpdate = vi.fn()
const mcpOAuthRelay = vi.fn()
const connectionsMint = vi.fn()
const connectionsMintState = vi.fn()
const connectionsPremint = vi.fn()
const connectionsStatus = vi.fn()
const connectionsCancel = vi.fn()
const connectionsDisconnect = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    mcpServers: (...a: unknown[]) => mcpServers(...a),
    mcpProbe: (...a: unknown[]) => mcpProbe(...a),
    mcpApply: (...a: unknown[]) => mcpApply(...a),
    mcpCustomAdd: (...a: unknown[]) => mcpCustomAdd(...a),
    mcpCustomGet: (...a: unknown[]) => mcpCustomGet(...a),
    mcpCustomUpdate: (...a: unknown[]) => mcpCustomUpdate(...a),
    mcpOAuthRelay: (...a: unknown[]) => mcpOAuthRelay(...a),
    connectionsMint: (...a: unknown[]) => connectionsMint(...a),
    connectionsMintState: (...a: unknown[]) => connectionsMintState(...a),
    connectionsPremint: (...a: unknown[]) => connectionsPremint(...a),
    connectionsStatus: (...a: unknown[]) => connectionsStatus(...a),
    connectionsCancel: (...a: unknown[]) => connectionsCancel(...a),
    connectionsDisconnect: (...a: unknown[]) => connectionsDisconnect(...a),
  },
}))

vi.mock('../pages/overview/McpTab', () => ({ default: () => null }))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { createTestStore, renderWithProviders } from './helpers'

const NOTION_URL = 'https://mcp.notion.com/mcp'
const AUTHORIZE_URL = 'https://mcp.notion.com/authorize?state=abc'

interface FakeTabWindow extends ApprovalTabWindow {
  closed: boolean
  written: string[]
}

function fakeTabWindow(): FakeTabWindow {
  const win: FakeTabWindow = {
    closed: false,
    written: [],
    close() {
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

/** A live mint row for Notion, as GET /api/connections/mint serves it. */
const mintRow = (over: Record<string, unknown> = {}) =>
  ({ slug: 'notion', state: 'minting', token: 'tok1', ...over })

function mount({ servicesEnabled = true, messages = [] as ChatMessage[] } = {}) {
  const store = createTestStore({
    chat: { messages, slotMessages: {} } as unknown as RootState['chat'],
  })
  return renderWithProviders(<ConnectionsPage servicesEnabled={servicesEnabled} />, { store })
}

/** The Notion card, once the gallery has rendered. */
async function notionCard(): Promise<HTMLElement> {
  return waitFor(() => {
    const found = document.getElementById('connection-notion')
    if (!found) throw new Error('Notion card not rendered')
    return found
  })
}

async function notionConnectButton(): Promise<HTMLElement> {
  return within(await notionCard()).getByRole('button', { name: 'Connect' })
}

let tabWindow: FakeTabWindow
let openSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  mcpApply.mockReset().mockResolvedValue({ ok: true })
  mcpCustomAdd.mockReset().mockResolvedValue({ ok: true, added: [], enabled: true })
  mcpCustomGet.mockReset().mockResolvedValue({ name: 'notion', spec: { url: NOTION_URL }, enabled: true })
  mcpCustomUpdate.mockReset().mockResolvedValue({ ok: true, name: 'notion' })
  mcpOAuthRelay.mockReset().mockResolvedValue({ ok: true })
  connectionsMint.mockReset().mockResolvedValue({
    ok: true, slug: 'notion', state: 'minting', token: 'tok1',
  })
  connectionsMintState.mockReset().mockResolvedValue(mintRow())
  connectionsPremint.mockReset().mockResolvedValue({ ok: true, preminting: ['notion'] })
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
  connectionsDisconnect.mockReset().mockResolvedValue({
    ok: true,
    disconnected: 'notion',
    grantRemoved: true,
    grantSurviving: [],
    entryRemoved: true,
    grantSharedWith: [],
  })

  tabWindow = fakeTabWindow()
  openSpy = vi.fn().mockReturnValue(tabWindow)
  vi.spyOn(window, 'open').mockImplementation(openSpy as unknown as typeof window.open)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('pre-mint on Services mount', () => {
  it('warms the approval URLs as soon as the gallery is on screen, once per mount', async () => {
    mount()
    await notionConnectButton()
    await waitFor(() => expect(connectionsPremint).toHaveBeenCalledTimes(1))
    // Re-renders keep flowing from the status poll; the request must not repeat.
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(connectionsPremint).toHaveBeenCalledTimes(1)
  })

  it('stays silent while the provider gallery is held closed', async () => {
    mount({ servicesEnabled: false })
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(connectionsPremint).not.toHaveBeenCalled()
  })

  it('does not surface the route being absent — it lands with the pre-mint slice', async () => {
    // Until that backend route exists this is a 404, which `j` rejects. The card
    // must show nothing about it: the mint the click starts is what actually
    // delivers the URL, and this only shortens the wait.
    connectionsPremint.mockRejectedValue(Object.assign(new Error('HTTP 404'), { status: 404 }))
    mount()
    const card = await notionCard()
    await waitFor(() => expect(connectionsPremint).toHaveBeenCalled())
    expect(within(card).queryByRole('status')).toBeNull()
    expect(within(card).queryByRole('alert')).toBeNull()
  })
})

describe('Connect opens its tab inside the click gesture', () => {
  it('calls window.open synchronously, before any request goes out', async () => {
    mount()
    const connect = await notionConnectButton()

    fireEvent.click(connect)

    // NO await above this line: the tab is open by the time the click handler
    // returns, which is the only moment the browser will allow it.
    expect(openSpy).toHaveBeenCalledTimes(1)
    expect(openSpy).toHaveBeenCalledWith('', '_blank')
    expect(mcpCustomAdd).toHaveBeenCalled()
    expect(openSpy.mock.invocationCallOrder[0])
      .toBeLessThan(mcpCustomAdd.mock.invocationCallOrder[0])

    await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
  })

  it('paints translated waiting copy into the blank tab', async () => {
    mount()
    fireEvent.click(await notionConnectButton())

    await waitFor(() => expect(tabWindow.written.join('')).toContain('Opening the approval page'))
    expect(tabWindow.written.join('')).toContain('This tab will continue automatically')
    await waitFor(() => expect(connectionsMint).toHaveBeenCalled())
  })

  it('navigates that same tab when the URL arrives — never opens a second one', async () => {
    mount()
    const connect = await notionConnectButton()
    // The mint answers on the next poll, which is where a deferred open would
    // have had to happen.
    connectionsMintState.mockResolvedValue(mintRow({ state: 'waiting', oauth_url: AUTHORIZE_URL }))

    fireEvent.click(connect)

    await waitFor(() => expect(tabWindow.location.href).toBe(AUTHORIZE_URL))
    expect(openSpy).toHaveBeenCalledTimes(1)
  })

  it('keeps polling once the tab it opened takes focus away from the dashboard', async () => {
    // The premise of this whole design is that the user ends up looking at the
    // placeholder tab -- which makes the dashboard document `hidden`. react-query
    // gates interval refetches on `focusManager.isFocused()`
    // (`document.visibilityState !== 'hidden'`) unless the query opts in, so
    // without that opt-in the ONLY fetch is the initial one at t≈0, which answers
    // `minting` with no URL. Every later poll is skipped, the tab is never pointed
    // anywhere, and the 30s deadline reports the approval page as unavailable --
    // one click made strictly worse than the two it replaced.
    //
    // The first mint read answers `minting`; only a SECOND read carries the URL,
    // so the tab can only be navigated if a background poll actually ran.
    connectionsMintState
      .mockResolvedValueOnce(mintRow({ state: 'minting' }))
      .mockResolvedValue(mintRow({ state: 'waiting', oauth_url: AUTHORIZE_URL }))
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    try {
      mount()

      fireEvent.click(await notionConnectButton())
      // The opened tab is what takes focus, so announce it the way a browser does.
      document.dispatchEvent(new Event('visibilitychange'))

      await waitFor(() => expect(connectionsMintState.mock.calls.length).toBeGreaterThan(1))
      await waitFor(() => expect(tabWindow.location.href).toBe(AUTHORIZE_URL))
    } finally {
      hidden.mockRestore()
    }
  })

  it('keeps the card usable when the browser blocks the tab', async () => {
    openSpy.mockReturnValue(null)
    mount()
    const connect = await notionConnectButton()
    connectionsMintState.mockResolvedValue(mintRow({ state: 'waiting', oauth_url: AUTHORIZE_URL }))

    fireEvent.click(connect)

    // A refused popup is not an error: the demoted fallback link carries the user.
    await waitFor(() => expect(screen.getByText('Re-open approval')).toBeTruthy())
    expect(screen.getByText('Tab did not open?')).toBeTruthy()
  })
})

describe('a mint that will never answer this tab', () => {
  it('tells the tab, and leaves the mint failure on the card in its own words', async () => {
    mount()
    const connect = await notionConnectButton()
    connectionsMintState.mockResolvedValue(mintRow({ state: 'failed', reason: 'mint_timeouterror' }))

    fireEvent.click(connect)

    await waitFor(() => expect(tabWindow.written.join('')).toContain('Approval page unavailable'))
    expect(tabWindow.written.join('')).toContain('Close this tab and try connecting again')
    // One message per failure: a terminal mint row is already reported by the
    // settlement path, and the tab writing a generic "approval page could not be
    // prepared" over it replaced a specific reason with a vaguer one.
    const card = await notionCard()
    await waitFor(() => expect(within(card).getByRole('alert')).toBeTruthy())
    expect(within(card).getByRole('alert').textContent).toContain('Action failed')
    expect(within(card).queryByText(/approval page could not be prepared/)).toBeNull()
  })

  it('does not fail the tab while the mint is merely still running', async () => {
    mount()
    const connect = await notionConnectButton()

    fireEvent.click(connect)

    await waitFor(() => expect(connectionsMint).toHaveBeenCalled())
    expect(tabWindow.written.join('')).not.toContain('Approval page unavailable')
    expect(tabWindow.closed).toBe(false)
  })

  it('never strands the tab on a URL the page refuses to navigate to', async () => {
    // A non-web scheme is filtered before the decision, so it reads as "no URL
    // yet" rather than "navigate" — otherwise the tab would be released from
    // tracking and then not moved, orphaning it on the placeholder for good.
    mount()
    const connect = await notionConnectButton()
    connectionsMintState.mockResolvedValue(
      mintRow({ state: 'waiting', oauth_url: 'javascript:alert(1)' }),
    )

    fireEvent.click(connect)

    await waitFor(() => expect(connectionsMint).toHaveBeenCalled())
    expect(tabWindow.location.href).toBe('')
    // Still tracked and still waiting: the deadline (or a good URL on a later
    // poll) resolves it, but it is never silently abandoned.
    expect(tabWindow.closed).toBe(false)
    expect(tabWindow.written.join('')).not.toContain('Approval page unavailable')
  })

  it('falls through to the failure message when the tab refuses navigation', async () => {
    mount()
    const connect = await notionConnectButton()
    Object.defineProperty(tabWindow, 'location', {
      get: () => {
        throw new Error('detached')
      },
    })
    connectionsMintState.mockResolvedValue(mintRow({ state: 'waiting', oauth_url: AUTHORIZE_URL }))

    fireEvent.click(connect)

    await waitFor(() => expect(tabWindow.written.join('')).toContain('Approval page unavailable'))
  })

  it('gives up at once when the request that would have minted was refused', async () => {
    // No mint is running and no poll will ever answer, so waiting out the
    // deadline would leave the user on a placeholder for 30s over a failure that
    // is already known.
    mcpCustomAdd.mockRejectedValue(new Error('name already in use'))
    mount()

    fireEvent.click(await notionConnectButton())

    await waitFor(() => expect(tabWindow.written.join('')).toContain('Approval page unavailable'))
    expect(connectionsMint).not.toHaveBeenCalled()
    // And the card keeps the gateway's own reason, which is more use than the
    // tab's generic one.
    const card = await notionCard()
    expect(within(card).getByRole('alert').textContent).toContain('name already in use')
  })

  it('cannot be re-clicked into a second attempt while the first is in flight', async () => {
    // `failPendingTab` is keyed by slug, so if a re-click could supersede the
    // placeholder while the FIRST request was still running, that request's
    // rejection would kill the SECOND attempt's live tab. This is what makes that
    // unreachable: every action on the card is `disabled={!!busy}`, and `busy` is
    // set for this slug before the request is awaited — so there is only ever one
    // attempt, and one tab, per provider at a time.
    let hold!: () => void
    mcpCustomAdd.mockImplementation(() => new Promise(resolve => {
      hold = () => resolve({ ok: true, added: [], enabled: true })
    }))
    mount()
    const connect = await notionConnectButton()

    fireEvent.click(connect)

    const card = await notionCard()
    await waitFor(() => expect(
      within(card).getByRole('button', { name: /Connecting/ }),
    ).toBeDisabled())
    // A second gesture reaches nothing, so no second tab is ever opened.
    fireEvent.click(within(card).getByRole('button', { name: /Connecting/ }))
    expect(openSpy).toHaveBeenCalledTimes(1)

    hold()
    await waitFor(() => expect(connectionsMint).toHaveBeenCalled())
  })
})

describe('a provider that turns out to hold a grant already', () => {
  it('says there was nothing to approve rather than sending the user to consent', async () => {
    mount()
    const connect = await notionConnectButton()
    connectionsMintState.mockResolvedValue(mintRow({ state: 'granted' }))

    fireEvent.click(connect)

    await waitFor(() => expect(tabWindow.written.join('')).toContain('nothing to approve'))
    expect(tabWindow.location.href).toBe('')
  })

  it('takes that verdict from the grant, never from the reachability probe', async () => {
    // `server.status === 'ok'` is the TOKENLESS /api/mcp probe's verdict about the
    // endpoint and outlives a revoked grant, so it must never read as "already
    // authorized". It IS a terminal condition for the wait (pre-existing), which
    // stops the mint poll — so no URL can still arrive for this tab. The tab is
    // therefore closed QUIETLY: claiming a grant would be a lie, and letting the
    // deadline claim the approval page failed would be a lie about a flow the card
    // has already resolved.
    mcpServers.mockResolvedValue([{
      name: 'notion',
      command: '',
      url: NOTION_URL,
      status: 'ok',
      source: 'mcp.json',
      enabled: true,
    } as McpServer])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'not_connected', grantPresent: false }],
    })
    mount()
    const card = await notionCard()

    fireEvent.click(await waitFor(() => within(card).getByRole('button', { name: /Connect|Authorize/ })))

    await waitFor(() => expect(tabWindow.closed).toBe(true))
    expect(tabWindow.written.join('')).not.toContain('nothing to approve')
    expect(tabWindow.written.join('')).not.toContain('Approval page unavailable')
    expect(within(card).queryByText(/approval page could not be prepared/)).toBeNull()
  })

  it('does take it from this attempt\'s own granted mint row', async () => {
    // The one source that IS this attempt's verdict: the mint ran and found there
    // was nothing to approve. A cached status row saying a grant exists on disk is
    // not the same claim -- see the next case.
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true }],
    })
    connectionsMintState.mockResolvedValue(mintRow({ state: 'granted' }))
    mount()
    const card = await notionCard()

    fireEvent.click(await waitFor(() => within(card).getByRole('button', { name: /Connect|Authorize/ })))

    await waitFor(() => expect(tabWindow.written.join('')).toContain('nothing to approve'))
    expect(tabWindow.location.href).toBe('')
  })

  it('never takes it from a cached grant row while the card still wants consent', async () => {
    // `grantPresent` is a ≤30s-polled "a grant artifact is on disk", not "this
    // attempt needs no consent". Reconnect is the primary button on a
    // needs-attention card, so a failing entry beside a cached `grantPresent: true`
    // is reachable -- and reading that row as the verdict painted "nothing to
    // approve" and closed the tab on the very settlement pass where the card was
    // still waiting for a real approval URL. Only this attempt's own mint row may
    // end the tab that way.
    mcpServers.mockResolvedValue([{
      name: 'notion',
      command: '',
      url: NOTION_URL,
      status: 'error',
      source: 'mcp.json',
      enabled: true,
    } as McpServer])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true }],
    })
    // The mint is genuinely still running: no verdict for this attempt yet.
    connectionsMintState.mockResolvedValue(mintRow({ state: 'minting' }))
    mount()
    const card = await notionCard()

    fireEvent.click(await waitFor(() => within(card).getByRole('button', { name: /Reconnect/ })))

    await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
    // The card is still asking for approval, so the tab must still be waiting for
    // it -- neither closed nor told there is nothing to approve.
    expect(tabWindow.written.join('')).not.toContain('nothing to approve')
    expect(tabWindow.closed).toBe(false)
    expect(within(card).getByRole('button', { name: /Cancel/ })).toBeTruthy()
  })
})

describe('cancel', () => {
  it('closes the placeholder tab the click opened', async () => {
    mount()
    fireEvent.click(await notionConnectButton())

    const card = await notionCard()
    const cancel = await waitFor(() => within(card).getByRole('button', { name: /Cancel/ }))
    fireEvent.click(cancel)

    await waitFor(() => expect(tabWindow.closed).toBe(true))
    await waitFor(() => expect(connectionsCancel).toHaveBeenCalledWith('notion', 'tok1'))
  })
})

describe('the placeholder tab deadline', () => {
  it('fails a tab whose mint never answers, without needing another poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      mount()
      const connect = await notionConnectButton()
      fireEvent.click(connect)
      await waitFor(() => expect(tabWindow.written.length).toBeGreaterThan(0))
      // No further poll is simulated: the real bug was that react-query pauses
      // polling once the placeholder takes focus, so only a timer can fire.
      await vi.advanceTimersByTimeAsync(APPROVAL_TAB_TIMEOUT_MS + 10)

      expect(tabWindow.written.join('')).toContain('Approval page unavailable')
      // The one failure with no other voice — nothing answered at all — so this
      // is where the card message belongs and nowhere else.
      const card = await notionCard()
      expect(within(card).getByRole('alert').textContent)
        .toContain('The approval page could not be prepared')
    } finally {
      vi.useRealTimers()
    }
  })

  it('takes its tabs and its timers with it when the page unmounts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const view = mount()
      fireEvent.click(await notionConnectButton())
      await waitFor(() => expect(tabWindow.written.length).toBeGreaterThan(0))

      view.unmount()

      // The tab goes: nothing is left that could ever point it anywhere.
      expect(tabWindow.closed).toBe(true)
      const painted = tabWindow.written.length
      // And the deadline goes with it, rather than firing `setFeedback` on a
      // component that no longer exists.
      await vi.advanceTimersByTimeAsync(APPROVAL_TAB_TIMEOUT_MS + 10)
      expect(tabWindow.written.length).toBe(painted)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('the entry write is unchanged by the tab', () => {
  it('still round-trips a stored spec on reconnect rather than clearing its hints', async () => {
    // The one-click tab is additive: it must not turn Reconnect's read-modify-write
    // back into a `{ url }`-only PUT, which cleared configured scopes/clientId.
    mcpServers.mockResolvedValue([{
      name: 'notion',
      command: '',
      url: NOTION_URL,
      status: 'error',
      source: 'mcp.json',
      enabled: true,
    } as McpServer])
    mcpCustomGet.mockResolvedValue({
      name: 'notion',
      spec: { url: 'https://old.example/mcp', scopes: ['read'], clientId: 'client-1' },
      enabled: true,
    })
    mount()
    const card = await notionCard()

    fireEvent.click(await waitFor(() => within(card).getByRole('button', { name: /Reconnect/ })))

    expect(openSpy).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(mcpCustomUpdate).toHaveBeenCalledWith(
      'notion',
      { url: NOTION_URL, scopes: ['read'], clientId: 'client-1' },
    ))
    expect(mcpCustomAdd).not.toHaveBeenCalled()
  })
})
