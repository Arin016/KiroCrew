/**
 * The detail panes' header sits OUTSIDE the scroller, so its height is a
 * standing cost. Measured against the panes' own markup it is 211px of a
 * 390x844 phone and 272px of a 320x568 one, which leaves the description and
 * timeline barely half the shortest phone — so while narrow the header collapses
 * once the reader has scrolled past it.
 *
 * Two things have to hold, and each fails silently in a different way:
 *   1. the hysteresis band, or the header flickers under a stationary finger
 *      (collapsing grows the scroll area, which can clamp `scrollTop` back down);
 *   2. every compact class carries an `sm:` reset, or a resize strands a
 *      compacted 15px title on a desktop where the header never scrolls.
 */
import { describe, it, expect } from 'vitest'
import { render, act } from '@testing-library/react'
import { nextCollapsed, useCollapsingHeader } from '../apps/issue-radar/lib/useCollapsingHeader'

describe('useCollapsingHeader hysteresis', () => {
  it('collapses only past the collapse threshold', () => {
    expect(nextCollapsed(false, 0)).toBe(false)
    expect(nextCollapsed(false, 56)).toBe(false)
    expect(nextCollapsed(false, 57)).toBe(true)
  })

  it('holds the collapse across the band instead of re-expanding', () => {
    // The band is the whole point: a single threshold would flip back here, and
    // expanding restores the offset that collapsed it — a flicker loop.
    expect(nextCollapsed(true, 40)).toBe(true)
    expect(nextCollapsed(true, 17)).toBe(true)
    expect(nextCollapsed(true, 16)).toBe(false)
    expect(nextCollapsed(true, 0)).toBe(false)
  })

  it('is asymmetric — the expand threshold is strictly below the collapse one', () => {
    // Pins the direction rather than the numbers: swapping the two thresholds
    // still satisfies every case above read individually.
    const collapseAt = [...Array(200).keys()].find((t) => nextCollapsed(false, t))
    const expandAt = [...Array(200).keys()].find((t) => !nextCollapsed(true, t + 1)) ?? Infinity
    expect(collapseAt).toBeGreaterThan(expandAt as number)
  })
})

function Probe() {
  const { collapsed, onScroll } = useCollapsingHeader()
  return (
    <div data-testid="scroller" onScroll={onScroll}>
      <span data-testid="state">{collapsed ? 'collapsed' : 'expanded'}</span>
    </div>
  )
}

describe('useCollapsingHeader wiring', () => {
  it('reads scrollTop off the element the handler is hung on', () => {
    const { getByTestId } = render(<Probe />)
    const scroller = getByTestId('scroller')
    const state = () => getByTestId('state').textContent

    expect(state()).toBe('expanded')
    // jsdom performs no layout, so scrollTop is assigned directly — the fact
    // under test is that the handler reads currentTarget, not a captured ref.
    Object.defineProperty(scroller, 'scrollTop', { value: 200, writable: true })
    act(() => { scroller.dispatchEvent(new Event('scroll', { bubbles: true })) })
    expect(state()).toBe('collapsed')

    ;(scroller as HTMLElement & { scrollTop: number }).scrollTop = 4
    act(() => { scroller.dispatchEvent(new Event('scroll', { bubbles: true })) })
    expect(state()).toBe('expanded')
  })
})

describe('the panes wire the collapse to the narrow scroller', () => {
  async function pane(name: string): Promise<string> {
    return (await import(`../apps/issue-radar/components/${name}.tsx?raw`)).default as string
  }

  // Both panes, because their headers are byte-identical declarations: fixing
  // one and leaving the other is how this defect class has survived review here
  // before.
  for (const name of ['IssueDetail', 'PrDetail']) {
    it(`${name}: listens on the wrapper that only scrolls while stacked`, async () => {
      const s = await pane(name)
      // The signal must come off the WRAPPER, not a viewport query. Above `sm:`
      // the wrapper is overflow-visible and the two body columns scroll
      // themselves, so hanging the handler here means it can only ever fire in
      // the layout that has a collapsible header — and there is no second
      // breakpoint to keep in step (useIsMobile flips at 768px, `sm:` at 640px).
      expect(s).toMatch(/onScroll=\{onScroll\}\n\s*className="flex flex-col sm:flex-row gap-6 px-6 py-5 h-full sm:items-stretch overflow-y-auto sm:overflow-visible"/)
      expect(s, 'the collapse must not be driven off a viewport query')
        .not.toMatch(/collapsed[^\n]*useIsMobile/)
    })

    it(`${name}: resets every compact class at sm:`, async () => {
      const s = await pane(name)
      // Without the resets, a narrow→wide resize leaves `collapsed` true and the
      // desktop header keeps its compact form with nothing left that scrolls to
      // expand it again.
      expect(s).toContain("'grid-rows-[0fr] sm:grid-rows-[1fr]'")
      expect(s).toContain("'grid-rows-[1fr] sm:grid-rows-[0fr]'")
      expect(s).toContain("'opacity-0 sm:opacity-100'")
      expect(s).toContain("'opacity-100 sm:opacity-0'")
      expect(s).toContain("'pt-2.5 pb-2 sm:pt-5 sm:pb-4'")
      expect(s).toContain("collapsed ? 'mt-1.5 sm:mt-3' : 'mt-3'")
      expect(s).toContain("collapsed ? 'hidden sm:contents' : 'contents'")
    })

    it(`${name}: eases the height instead of snapping it`, async () => {
      const s = await pane(name)
      // The reclaim is a HEIGHT change, so something must animate a layout
      // property — a transform-only animation would shrink the title visually
      // while its box still occupied the 140px this feature exists to give back.
      // `1fr`↔`0fr` on a grid track is the cross-browser way to ease to a
      // content height without measuring it in JS (Safari has interpolated grid
      // tracks since 16.0; scroll-driven timelines only reached it in 26).
      expect(s, `${name}: the title height must animate`)
        .toContain('transition-[grid-template-rows]')
      // Two tracks moving in lockstep, so the pair's height interpolates
      // linearly between the end states instead of bumping up mid-flight.
      expect((s.match(/transition-\[grid-template-rows\]/g) ?? []).length).toBe(2)
      expect(s, `${name}: the crossfade must animate`).toContain('transition-opacity')
      expect(s, `${name}: the header padding must animate`).toContain('transition-[padding]')
      // Every animated element opts out under prefers-reduced-motion. Five:
      // two grid tracks, two crossfades, one padding.
      expect((s.match(/motion-reduce:transition-none/g) ?? []).length).toBeGreaterThanOrEqual(5)
    })

    it(`${name}: swaps two titles rather than resizing one`, async () => {
      const s = await pane(name)
      // `line-clamp` cannot be interpolated — there is no CSS that eases four
      // lines into one — so a single resizing element could only ever snap. The
      // clamp is therefore GONE from the header, replaced by a one-line echo
      // that `truncate`s.
      //
      // Scoped to the header and stripped of JSX comments before matching:
      // `line-clamp-2` is legitimately used further down the pane (a linked-ref
      // title), and the prose right above this element quotes the very pattern
      // being banned — an unscoped substring test fails on its own explanation.
      const header = s.match(/<header[\s\S]*?<\/header>/)
      expect(header, `${name}: expected a header element`).not.toBeNull()
      const code = header![0].replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
      expect(code, `${name}: no un-animatable clamp may come back`).not.toMatch(/line-clamp/)
      expect(code).toContain('text-[15px] font-bold leading-tight text-text-strong truncate')
      // Exactly one real heading: the full-size one, which stays in the DOM in
      // both states (a zero-height track, never `display:none`) so assistive tech
      // always reads the whole title.
      expect((code.match(/<h1\b/g) ?? []).length, `${name}: exactly one h1`).toBe(1)
      expect(code).toMatch(/<h1 className="text-\[27px\]/)
      // The echo is decorative, or the title is announced twice.
      expect(code, `${name}: the compact echo must be aria-hidden`).toMatch(/aria-hidden="true"/)
    })

    it(`${name}: keeps state and the outbound link in the compact bar`, async () => {
      const s = await pane(name)
      // What the compact bar drops is only what the opening comment card repeats
      // a few pixels below (author, when, identity). State and the #number are
      // the pane's identity and appear nowhere else standing, so they stay.
      const meta = s.match(/<div className=\{`flex items-center gap-2 flex-wrap[\s\S]*?\n {12}<\/div>/)
      expect(meta, `${name}: expected the header meta row`).not.toBeNull()
      const hidden = meta![0].slice(meta![0].indexOf("'hidden sm:contents'"))
      expect(hidden).toContain('<MemberBadge')
      expect(hidden).not.toContain('<StatePill')
      expect(hidden).not.toContain('font-mono text-muted hover:text-accent hover:underline')
      // `locked` is pane state, not repeated metadata: CommentCard's props are
      // author/when/assoc/role/body/reactions and carry no lock, so collapsing
      // it would leave a locked issue with no lock indicator ANYWHERE once
      // scrolled — the one case where "only what the card repeats" was violated.
      expect(hidden, `${name}: the lock warning must not be collapsed away`)
        .not.toContain('issueDetail.locked')
    })
  }
})
