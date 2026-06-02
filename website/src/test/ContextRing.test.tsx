/**
 * ContextRing tooltip: shows absolute "used / window" tokens when known so a
 * percentage can't be misread (e.g. 44% of a 200k window, not 1M).
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ContextRing from '../components/ContextRing'

describe('ContextRing', () => {
  it('shows only percent when token counts are absent', () => {
    const { container } = render(<ContextRing pct={44} />)
    expect(container.querySelector('title')?.textContent).toBe('Context: 44%')
  })

  it('shows absolute tokens in the tooltip when window is known', () => {
    const { container } = render(<ContextRing pct={44} usedTokens={88000} windowTokens={200000} />)
    // 88k / 200k makes the 44% unambiguous.
    expect(container.querySelector('title')?.textContent).toBe('Context: 44% (88k / 200k tokens)')
  })

  it('ignores a zero/absent window (no misleading token text)', () => {
    const { container } = render(<ContextRing pct={9} usedTokens={88000} windowTokens={0} />)
    expect(container.querySelector('title')?.textContent).toBe('Context: 9%')
  })

  it('renders the numeric label only when pct > 0', () => {
    const { container, rerender } = render(<ContextRing pct={0} />)
    expect(container.querySelector('text')).toBeNull()
    rerender(<ContextRing pct={50} />)
    expect(container.querySelector('text')?.textContent).toBe('50')
  })
})
