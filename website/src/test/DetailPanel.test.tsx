import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import DetailPanel from '../components/DetailPanel'

// The panel's pixel width lives as an inline style on the inner div that also
// carries the left border (`div.border-l`). Read it back to assert clamping.
function panelWidth(container: HTMLElement): number {
  const el = container.querySelector('div.border-l') as HTMLElement | null
  if (!el) throw new Error('panel element not found')
  return parseInt(el.style.width, 10)
}

const setViewport = (w: number) => {
  Object.defineProperty(window, 'innerWidth', { value: w, configurable: true, writable: true })
}

// Regression for Mesh-2230: a persisted width sized on a wide monitor must not
// push the panel (shrink-0, in an overflow-hidden row) past a smaller viewport,
// which clipped the right-edge header actions (diff toggle / Edit·Preview).
describe('DetailPanel width clamp (Mesh-2230)', () => {
  const ORIG = window.innerWidth
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); setViewport(ORIG) })

  it('clamps a persisted width wider than the viewport to 60% of the viewport', () => {
    setViewport(1000) // cap = 600
    localStorage.setItem('mc-test-w', '2000')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(600)
  })

  it('clamps an oversized initialWidth to 60% of the viewport', () => {
    setViewport(1000) // cap = 600
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} initialWidth={5000} minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(600)
  })

  it('leaves a width that already fits unchanged', () => {
    setViewport(1600) // cap = 960
    localStorage.setItem('mc-test-w', '480')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(480)
  })

  it('re-clamps down when the viewport shrinks', () => {
    setViewport(2000) // cap = 1200
    localStorage.setItem('mc-test-w', '1100')
    const { container } = render(
      <DetailPanel title="t" onClose={() => {}} storageKey="mc-test-w" minWidth={300}>x</DetailPanel>,
    )
    expect(panelWidth(container)).toBe(1100) // fits initially
    act(() => { setViewport(800); window.dispatchEvent(new Event('resize')) }) // cap = 480
    expect(panelWidth(container)).toBe(480)
  })
})
