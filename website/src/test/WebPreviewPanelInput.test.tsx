/**
 * Browse-mirror input surface tests.
 *
 * Covers the two things a jsdom test can actually prove about this surface: the
 * key mapping (pure), and that the panel exposes a keyboard-accessible input
 * region wired to the input endpoint. Pointer *geometry* is asserted through the
 * mapping helper rather than synthetic layout, because jsdom reports zero-size
 * rects — a click test that "passed" on a 0x0 image would prove nothing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'

import { browserKeyFromEvent } from '../utils/browserKeys'
import WebPreviewPanel from '../components/WebPreviewPanel'
import { renderWithProviders } from './helpers'

/** Build the minimal React-like keyboard event the mapper reads. */
function keyEvent(over: Partial<Record<string, unknown>>) {
  return {
    key: 'a',
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    shiftKey: false,
    ...over,
  } as never
}

describe('browserKeyFromEvent', () => {
  it('maps a printable character to itself', () => {
    expect(browserKeyFromEvent(keyEvent({ key: 'a' }))).toBe('a')
    expect(browserKeyFromEvent(keyEvent({ key: 'A' }))).toBe('A')
  })

  it('maps named keys the backend allowlist accepts', () => {
    expect(browserKeyFromEvent(keyEvent({ key: 'Enter' }))).toBe('Enter')
    expect(browserKeyFromEvent(keyEvent({ key: 'ArrowLeft' }))).toBe('ArrowLeft')
    expect(browserKeyFromEvent(keyEvent({ key: ' ' }))).toBe('Space')
  })

  it('prefixes modifiers', () => {
    expect(browserKeyFromEvent(keyEvent({ key: 'a', ctrlKey: true }))).toBe('Control+a')
    expect(browserKeyFromEvent(keyEvent({ key: 'v', metaKey: true }))).toBe('Meta+v')
    expect(browserKeyFromEvent(keyEvent({ key: 'Tab', shiftKey: true }))).toBe('Shift+Tab')
  })

  it('does not send Shift for a character that already reflects it', () => {
    // The browser gives "A" for shift+a; "Shift+A" would double-apply it.
    expect(browserKeyFromEvent(keyEvent({ key: 'A', shiftKey: true }))).toBe('A')
  })

  it('ignores bare modifier presses', () => {
    for (const key of ['Shift', 'Control', 'Alt', 'Meta']) {
      expect(browserKeyFromEvent(keyEvent({ key }))).toBeNull()
    }
  })

  it('does not forward keys reserved by the dashboard', () => {
    // F-keys and Escape are not forwarded: Escape is the release affordance, so
    // forwarding it would make the surface impossible to leave by keyboard.
    expect(browserKeyFromEvent(keyEvent({ key: 'F5' }))).toBeNull()
    expect(browserKeyFromEvent(keyEvent({ key: 'Escape' }))).toBeNull()
  })
})

describe('WebPreviewPanel input surface', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  /** Push a frame for this session so the live mirror mounts. */
  function emitFrame(sessionKey: string) {
    act(() => {
      window.dispatchEvent(
        new CustomEvent('kirocrew-browser-frame', {
          detail: {
            data: 'AAAA',
            format: 'jpeg',
            session_key: sessionKey,
            device_width: 800,
            device_height: 600,
          },
        }),
      )
    })
  }

  it('exposes the frame as a focusable, labelled input region', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    // Keyboard reachable and named — what the blocking accessible-interactive
    // -elements rule requires of an interactive non-native element.
    expect(surface).toHaveAttribute('tabIndex', '0')
    expect(surface.getAttribute('aria-label')).toBeTruthy()
  })

  it('tells the user the surface is interactive rather than view-only', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    expect(screen.getByText(/Click, scroll, or type here/i)).toBeInTheDocument()
  })

  it('switches the hint to a release affordance while focused', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    act(() => {
      fireEvent.focus(surface)
    })
    // Keyboard users must be able to see how to get back out.
    expect(screen.getByText(/press Escape to release/i)).toBeInTheDocument()
  })

  it('forwards a typed key to the input endpoint', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    fireEvent.keyDown(surface, { key: 'a' })
    const posted = fetchMock.mock.calls.find(c => String(c[0]).includes('/api/browser/input'))
    expect(posted).toBeTruthy()
    const body = JSON.parse(String(posted![1].body))
    expect(body).toMatchObject({ verb: 'key', key: 'a', session_key: 'sess-1' })
  })

  it('does not forward Escape to the page', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    fireEvent.keyDown(surface, { key: 'Escape' })
    const posted = fetchMock.mock.calls.filter(c => String(c[0]).includes('/api/browser/input'))
    expect(posted).toHaveLength(0)
  })

  it('does not forward keystrokes mid-IME-composition', () => {
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    fireEvent.keyDown(surface, { key: 'a', isComposing: true })
    const posted = fetchMock.mock.calls.filter(c => String(c[0]).includes('/api/browser/input'))
    expect(posted).toHaveLength(0)
  })

  /** jsdom reports zero-size rects, so pointer geometry needs a stubbed rect for
   *  the frame image — otherwise every event is discarded as a letterbox click and
   *  the assertions would pass for the wrong reason. */
  function stubFrameRect() {
    vi.spyOn(HTMLImageElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 200, bottom: 100, width: 200, height: 100,
      toJSON: () => ({}),
    } as DOMRect)
  }

  function postedBodies() {
    return fetchMock.mock.calls
      .filter(c => String(c[0]).includes('/api/browser/input'))
      .map(c => JSON.parse(String(c[1].body)))
  }

  it('sends clickCount 1 even for a double-click', () => {
    // The browser fires click twice (detail 1 then 2). Forwarding e.detail would
    // deliver 1 + 2 = 3 clicks and action a submit control three times.
    stubFrameRect()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    fireEvent.click(surface, { clientX: 100, clientY: 50, detail: 1 })
    fireEvent.click(surface, { clientX: 100, clientY: 50, detail: 2 })
    const clicks = postedBodies().filter(b => b.verb === 'click')
    expect(clicks).toHaveLength(2)
    expect(clicks.every(c => c.clickCount === 1)).toBe(true)
    // Coordinates travel normalized, not as pixels.
    expect(clicks[0]).toMatchObject({ x: 0.5, y: 0.5 })
  })

  it('discards a click that lands in the letterbox rather than clamping it', () => {
    stubFrameRect()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    // Below the 100px-tall frame — i.e. in the black bar.
    fireEvent.click(surface, { clientX: 100, clientY: 180, detail: 1 })
    expect(postedBodies().filter(b => b.verb === 'click')).toHaveLength(0)
  })

  it('forwards wheel through a non-passive native listener', () => {
    // React 18 attaches its root wheel handler as passive, so a JSX onWheel could
    // not preventDefault and the dashboard would scroll behind the panel.
    stubFrameRect()
    renderWithProviders(<WebPreviewPanel sessionKey="sess-1" />)
    emitFrame('sess-1')
    const surface = screen.getByRole('application')
    const evt = new WheelEvent('wheel', {
      clientX: 100, clientY: 50, deltaY: 120, bubbles: true, cancelable: true,
    })
    act(() => {
      surface.dispatchEvent(evt)
    })
    expect(evt.defaultPrevented).toBe(true)
  })
})
