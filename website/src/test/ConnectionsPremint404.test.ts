/**
 * The pre-mint warm-up must be INVISIBLE until its backend route exists.
 *
 * The page fires `api.connectionsPremint()` once per mount of the Services panel
 * and swallows the result. The route lands with the pre-mint slice, so until then
 * every dashboard mount gets a 404 — and that is only acceptable if the 404
 * genuinely reaches nothing: no unhandled rejection, no auth banner, no card
 * error, no state change.
 *
 * This exercises the REAL `api.connectionsPremint` against a stubbed `fetch`
 * rather than a mocked `api`, because the thing under test is what the client's
 * own `j`/`post` helpers do with a 404 body — which a mocked `api` would hide.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api/client'

const originalFetch = globalThis.fetch

function notFound(body = '{"error":"not found"}'): Response {
  return new Response(body, {
    status: 404,
    statusText: 'Not Found',
    headers: { 'Content-Type': 'application/json' },
  })
}

let rejections: unknown[]
let onRejection: (event: PromiseRejectionEvent) => void

beforeEach(() => {
  rejections = []
  onRejection = event => void rejections.push(event.reason)
  window.addEventListener('unhandledrejection', onRejection)
})

afterEach(() => {
  window.removeEventListener('unhandledrejection', onRejection)
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('the pre-mint warm-up before its route exists', () => {
  it('rejects rather than resolving, so a bare call could not silently pass', async () => {
    // Establishing the premise for the next case: `j` throws on a non-ok
    // response, so the page's `.catch()` is load-bearing rather than decorative.
    globalThis.fetch = vi.fn().mockResolvedValue(notFound()) as unknown as typeof fetch
    await expect(api.connectionsPremint()).rejects.toThrow()
  })

  it('is fully absorbed by the caller-side catch the page uses', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(notFound()) as unknown as typeof fetch

    // Exactly the page's call shape (see the premint effect in ConnectionsPage).
    let settled: unknown = 'unset'
    void api.connectionsPremint().catch(() => undefined).then(v => { settled = v })
    // Two microtask drains: one for the fetch, one for the catch/then chain.
    await Promise.resolve()
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(settled).toBeUndefined()
    expect(rejections).toEqual([])
  })

  it('raises no re-auth banner, because that path is 403-with-a-header only', async () => {
    // `checkSessionExpired` is the one side effect `j` runs BEFORE it throws, and
    // it acts only on a 403 carrying `X-Auth-Required`. A 404 must leave the
    // document untouched — otherwise every mount would flash a sign-in prompt.
    globalThis.fetch = vi.fn().mockResolvedValue(notFound()) as unknown as typeof fetch
    const before = document.body.innerHTML

    await api.connectionsPremint().catch(() => undefined)

    expect(document.body.innerHTML).toBe(before)
    expect(document.querySelector('[data-testid="session-expired-banner"]')).toBeNull()
    expect(document.body.textContent).not.toMatch(/sign in|session expired/i)
  })

  it('does not reach the artifact-write bookkeeping the POST helper wires up', async () => {
    // `post()` routes every request through `trackArtifactWrite`, which counts a
    // write against an artifact slug parsed out of the URL. The premint path must
    // not look like an artifact write; the URL carries no `/api/artifacts/`
    // segment, so nothing is counted.
    const spy = vi.fn().mockResolvedValue(notFound())
    globalThis.fetch = spy as unknown as typeof fetch

    await api.connectionsPremint().catch(() => undefined)

    expect(spy).toHaveBeenCalledTimes(1)
    const url = String(spy.mock.calls[0][0])
    expect(url).toBe('/api/connections/premint')
    expect(url).not.toContain('/api/artifacts/')
  })
})
