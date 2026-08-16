/**
 * `DetailPanel` applies a `minWidth` FLOOR after every cap, and its own comment
 * documents the residual: once the row cannot afford the floor, the floor wins and
 * the panel overflows its row — "only reachable on a viewport narrower than
 * minWidth + reserveWidth where the layout is already unusable".
 *
 * A phone is not already unusable; it is the target. Computed from the clamp at a
 * 390px viewport:
 *
 *   Project Detail task panel  max(300, min(420, 234)) = 300  -> content  90px
 *   ChatPage search panel      max(320, min(400, 234)) = 320  -> chat     70px
 *
 * No caller can configure its way out, because the floor is applied last.
 *
 * The narrow case reuses the EXISTING `embedded` rendering rather than forking the
 * width style: that path is already full width, no divider, no width animation,
 * and its `!embedded` gate drops the resize handle too. A style fork was tried and
 * measured wrong — `width: 100%` on the inner div resolves against the motion
 * wrapper, which is `shrink-0` with an animated `width: 'auto'`, i.e. content-sized.
 * In an isolated 390px row that produced a 42px panel: narrower than before.
 *
 * These are RENDER assertions on the DOM the component produces. jsdom performs no
 * layout, so what they pin is the width CONTRACT the browser then resolves.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

const mobile = { value: false }
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))

const { default: DetailPanel } = await import('../components/DetailPanel')

afterEach(() => { cleanup(); mobile.value = false })

describe('DetailPanel at narrow widths', () => {
  it('renders the frameless full-width form while narrow', () => {
    mobile.value = true
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={420} minWidth={300}>x</DetailPanel>,
    )
    const root = container.firstElementChild as HTMLElement
    // `w-full`, and NO inline width: the width contract is gone rather than
    // overridden, so no floor can reassert itself.
    expect(root.className).toMatch(/\bw-full\b/)
    expect(root.getAttribute('style') ?? '').not.toMatch(/width/)
    // No motion wrapper to resolve a percentage against -- this IS the root.
    expect(container.querySelector('div[style*="width"]')).toBeNull()
  })

  it('keeps the pixel width contract on a desktop', () => {
    mobile.value = false
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={420} minWidth={300}>x</DetailPanel>,
    )
    const el = container.querySelector('div.border-l') as HTMLElement | null
    expect(el, 'expected the bordered panel root on desktop').not.toBeNull()
    expect(el!.style.width).toMatch(/^\d+px$/)
    expect(el!.style.minWidth).toBe('300px')
  })

  it('drops the divider while narrow and keeps it on a desktop', () => {
    mobile.value = true
    const { container: narrow } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    // Nothing sits to its left once it owns the width.
    expect(narrow.querySelector('div.border-l')).toBeNull()
    cleanup()
    mobile.value = false
    const { container: wide } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    expect(wide.querySelector('div.border-l')).not.toBeNull()
  })

  it('drops the resize handle on touch and keeps it on a desktop', () => {
    mobile.value = true
    const { container: narrow } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    // Pointer-only affordance, and there is nothing to resize against.
    expect(narrow.querySelector('[role="separator"][aria-orientation="vertical"]')).toBeNull()
    cleanup()
    mobile.value = false
    const { container: wide } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    expect(wide.querySelector('[role="separator"][aria-orientation="vertical"]')).not.toBeNull()
  })

  it('still offers a way back out while narrow', () => {
    // The panel owns the whole width, so its own close control is the ONLY route
    // back to the content it covers.
    mobile.value = true
    const { container } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    expect(container.querySelector('[aria-label]'), 'expected a labelled close control').not.toBeNull()
  })

  it('reuses the embedded rendering rather than forking it', () => {
    // The two paths must stay ONE path: a second full-width branch would drift.
    mobile.value = true
    const { container: narrow } = render(<DetailPanel title="t" onClose={() => {}}>x</DetailPanel>)
    const narrowCls = (narrow.firstElementChild as HTMLElement).className
    cleanup()
    mobile.value = false
    const { container: emb } = render(<DetailPanel title="t" onClose={() => {}} embedded>x</DetailPanel>)
    expect((emb.firstElementChild as HTMLElement).className).toBe(narrowCls)
  })
})
