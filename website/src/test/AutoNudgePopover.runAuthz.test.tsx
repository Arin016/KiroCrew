import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import userEvent from '@testing-library/user-event'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'

/**
 * The authorization control is the operator's half of the per-run grant, so these
 * assert what an operator can and cannot do from it: offer the window only while
 * a run is live, never offer a second one on top of a live grant, and never
 * present a renew affordance.
 */

const BASE: AutoNudgeLoop = {
  id: 'loop-1',
  slot_key: 'chat-1-x',
  message: 'watch the build',
  idle_secs: 60,
  max_cycles: 24,
  cycle_count: 3,
  active: true,
  last_fire_ts: 0,
  auto_approve_remaining_secs: 0,
  auto_approve_windows: [7200, 28800, 43200],
}

function renderPopover(loop: AutoNudgeLoop | null, onChange = vi.fn()) {
  render(
    <AutoNudgePopover
      slotKey="chat-1-x"
      loop={loop}
      open
      onOpenChange={vi.fn()}
      onChange={onChange}
    />,
  )
  return onChange
}


/** Renders the popover with a REAL parent: `onChange` updates the loop prop.
 *
 * A `vi.fn()` onChange leaves the component on its initial props forever, so any
 * assertion about post-save rendering would be asserting against a parent that
 * does not exist in the app.
 */
function renderWithLiveParent(initial: AutoNudgeLoop | null) {
  const onOpenChange = vi.fn()
  function Host() {
    const [loop, setLoop] = useState<AutoNudgeLoop | null>(initial)
    return (
      <AutoNudgePopover
        slotKey="chat-1-x"
        loop={loop}
        open
        onOpenChange={onOpenChange}
        onChange={setLoop}
      />
    )
  }
  render(<Host />)
  return { onOpenChange }
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{"ok":true}', { status: 200 })))
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('per-run auto-approve authorization', () => {
  it('offers the server-supplied windows while a run is live and ungranted', () => {
    renderPopover(BASE)
    expect(screen.getByRole('button', { name: /authorize auto-approve for 2h/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /authorize auto-approve for 8h/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /authorize auto-approve for 12h/i })).toBeInTheDocument()
  })

  it('posts the clicked window to the authorize endpoint', async () => {
    renderPopover(BASE)
    await userEvent.click(screen.getByRole('button', { name: /authorize auto-approve for 8h/i }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('/api/autonudge/loop-1/authorize')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ window_secs: 28800 })
  })

  it('shows the remaining window instead of the offer once granted', () => {
    renderPopover({ ...BASE, auto_approve_remaining_secs: 3 * 3600 })
    expect(screen.queryByRole('button', { name: /authorize auto-approve for 2h/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /authorize auto-approve for 12h/i })).not.toBeInTheDocument()
    expect(screen.getByText(/ends in 3h/i)).toBeInTheDocument()
  })

  it('never offers a renew affordance', () => {
    renderPopover({ ...BASE, auto_approve_remaining_secs: 600 })
    // A run whose window is nearly spent is exactly where a renew button would be
    // tempting. The property is the absence of the CONTROL, not of the word --
    // the copy itself says the grant is never extended.
    expect(screen.queryByRole('button', { name: /renew|extend/i })).not.toBeInTheDocument()
    // And no window button either: an offer next to a live grant would let an
    // operator stack a second window on top of the first.
    expect(screen.queryByRole('button', { name: /authorize auto-approve/i })).not.toBeInTheDocument()
  })

  it('says the window is released when the run stops', () => {
    renderPopover({ ...BASE, auto_approve_remaining_secs: 7200 })
    expect(screen.getByText(/released as soon as the run stops/i)).toBeInTheDocument()
  })

  it('hides the control entirely for a stopped run', () => {
    renderPopover({ ...BASE, active: false })
    expect(screen.queryByText(/auto-approve for this run/i)).not.toBeInTheDocument()
  })

  it('hides the control when no loop is armed', () => {
    renderPopover(null)
    expect(screen.queryByText(/auto-approve for this run/i)).not.toBeInTheDocument()
  })

  it('falls back to the default offer when the server sends no windows', () => {
    const { auto_approve_windows: _omitted, ...withoutWindows } = BASE
    renderPopover(withoutWindows as AutoNudgeLoop)
    expect(screen.getByRole('button', { name: /authorize auto-approve for 2h/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /authorize auto-approve for 8h/i })).toBeInTheDocument()
  })

  it('maps a server error code to a catalog string, not raw server prose', async () => {
    // The server's `error` field is advisory English. Echoing it verbatim puts an
    // untranslated fragment in a localized UI, so the `code` is what gets mapped.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{"error":"owner only","code":"not_owner"}', { status: 403 })),
    )
    const onChange = renderPopover(BASE)
    await userEvent.click(screen.getByRole('button', { name: /authorize auto-approve for 8h/i }))
    await waitFor(() =>
      expect(screen.getByText(/only the owner can authorize/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText('owner only')).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('falls back to the generic message for an unknown code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{"error":"boom","code":"wat"}', { status: 500 })),
    )
    renderPopover(BASE)
    await userEvent.click(screen.getByRole('button', { name: /authorize auto-approve for 8h/i }))
    await waitFor(() =>
      expect(screen.getByText(/could not authorize this run/i)).toBeInTheDocument(),
    )
  })

  it('reflects the grant locally so the offer does not flash back', async () => {
    const onChange = renderPopover(BASE)
    await userEvent.click(screen.getByRole('button', { name: /authorize auto-approve for 2h/i }))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(onChange.mock.calls[0][0].auto_approve_remaining_secs).toBe(7200)
  })

  it('renders a permanent grant with its own sentence, not "ends in no expiry"', () => {
    renderPopover({ ...BASE, auto_approve_remaining_secs: -1 })
    expect(screen.getByText(/has no timed expiry/i)).toBeInTheDocument()
    expect(screen.queryByText(/ends in/i)).not.toBeInTheDocument()
  })

  it('offers an early revoke once granted', async () => {
    // Without this the only way to shed a mis-clicked 12h window is Stop loop --
    // destroying the work to reduce the grant.
    const onChange = renderPopover({ ...BASE, auto_approve_remaining_secs: 12 * 3600 })
    await userEvent.click(screen.getByRole('button', { name: /revoke auto-approve/i }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('/api/autonudge/loop-1/authorize')
    expect(init.method).toBe('DELETE')
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(onChange.mock.calls[0][0].auto_approve_remaining_secs).toBe(0)
  })

  it('does not offer revoke before anything is granted', () => {
    renderPopover(BASE)
    expect(
      screen.queryByRole('button', { name: /revoke auto-approve/i }),
    ).not.toBeInTheDocument()
  })

  it('keeps the button labels short but names the action to assistive tech', () => {
    // Three "Authorize auto-approve for 12h" labels need ~560px in a ~388px
    // popover, so the verb is stated once in the helper and the buttons carry the
    // duration. The full phrase stays in aria-label.
    renderPopover(BASE)
    const btn = screen.getByRole('button', { name: /authorize auto-approve for 8h/i })
    expect(btn).toHaveTextContent(/^8h$/)
    expect(screen.getByText(/authorize auto-approve so this run keeps working/i)).toBeInTheDocument()
  })

  it('reports a failed revoke as a failed revoke, not a failed authorization', async () => {
    // The user asked to DROP authority. Telling them authorizing failed is the
    // wrong sentence on a security control.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network')
      }),
    )
    renderPopover({ ...BASE, auto_approve_remaining_secs: 7200 })
    await userEvent.click(screen.getByRole('button', { name: /revoke auto-approve/i }))
    await waitFor(() =>
      expect(screen.getByText(/could not revoke/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/could not authorize/i)).not.toBeInTheDocument()
  })

  it('maps a revoke server error without claiming an authorization failed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{"error":"owner only","code":"not_owner"}', { status: 403 })),
    )
    renderPopover({ ...BASE, auto_approve_remaining_secs: 7200 })
    await userEvent.click(screen.getByRole('button', { name: /revoke auto-approve/i }))
    await waitFor(() =>
      expect(screen.getByText(/only the owner can authorize/i)).toBeInTheDocument(),
    )
  })

  it('keeps the popover open after arming so the offer is discoverable', async () => {
    // Closing here is what hid the feature at the one moment it matters: arm an
    // overnight run, the panel closes, and the operator walks away un-authorized
    // into the exact stall this PR fixes.
    const onOpenChange = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({ loop: { ...BASE, active: true, auto_approve_remaining_secs: 0 } }),
          { status: 200 },
        ),
      ),
    )
    render(
      <AutoNudgePopover
        slotKey="chat-1-x"
        loop={null}
        open
        onOpenChange={onOpenChange}
        onChange={vi.fn()}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /start loop/i }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('still closes after a save on an already-authorized run', async () => {
    const onOpenChange = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            loop: { ...BASE, active: true, auto_approve_remaining_secs: 7200 },
          }),
          { status: 200 },
        ),
      ),
    )
    render(
      <AutoNudgePopover
        slotKey="chat-1-x"
        loop={BASE}
        open
        onOpenChange={onOpenChange}
        onChange={vi.fn()}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('says a goal rewrite releases the window, naming the real field labels', () => {
    renderPopover({ ...BASE, auto_approve_remaining_secs: 7200 })
    expect(screen.getByText(/rewriting the goal releases it/i)).toBeInTheDocument()
    // The caveat must name the controls that are actually on screen, so match the
    // sentence -- the field LABEL also contains this phrase.
    expect(
      screen.getByText(/changing Idle seconds before nudge or Max cycles does not/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/saving a new goal starts a different run/i)).not.toBeInTheDocument()
  })

  it('announces explicitly when a save released the window', async () => {
    // Relying on the offer re-rendering is what made this silent: authorize 8h,
    // polish the wording, Save, walk away -- and lose the night.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            loop: { ...BASE, active: true, auto_approve_remaining_secs: 0 },
          }),
          { status: 200 },
        ),
      ),
    )
    renderWithLiveParent({ ...BASE, auto_approve_remaining_secs: 28800 })
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() =>
      expect(screen.getByText(/released because the goal changed/i)).toBeInTheDocument(),
    )
  })

  it('acknowledges a save that keeps the panel open', async () => {
    // Otherwise the panel just fails to close, which habituated users read as
    // "save did not land".
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({ loop: { ...BASE, active: true, auto_approve_remaining_secs: 0 } }),
          { status: 200 },
        ),
      ),
    )
    renderWithLiveParent(null)
    await userEvent.click(screen.getByRole('button', { name: /start loop/i }))
    await waitFor(() => expect(screen.getByText(/goal saved/i)).toBeInTheDocument())
  })

  it('clears the notice once the operator re-authorizes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        new Response(
          JSON.stringify(
            String(url).includes('/authorize')
              ? { ok: true }
              : { loop: { ...BASE, active: true, auto_approve_remaining_secs: 0 } },
          ),
          { status: 200 },
        ),
      ),
    )
    renderWithLiveParent({ ...BASE, auto_approve_remaining_secs: 28800 })
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() =>
      expect(screen.getByText(/released because the goal changed/i)).toBeInTheDocument(),
    )
    await userEvent.click(screen.getByRole('button', { name: /authorize auto-approve for 2h/i }))
    await waitFor(() =>
      expect(screen.queryByText(/released because the goal changed/i)).not.toBeInTheDocument(),
    )
  })
})
