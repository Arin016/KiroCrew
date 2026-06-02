import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

// jsdom polyfill: scroll-layout uses ResizeObserver to track when the chip
// strip can scroll left/right.
if (typeof globalThis.ResizeObserver === 'undefined') {
  ;(globalThis as any).ResizeObserver = class { observe() {}; unobserve() {}; disconnect() {} }
}

describe('FollowUpBar', () => {
  it('renders a button per option', () => {
    render(<FollowUpBar options={['Alpha', 'Beta', 'Gamma']} picked={new Set()} onSelect={() => {}} />)
    expect(screen.getByRole('button', { name: 'Alpha' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Beta' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Gamma' })).toBeInTheDocument()
  })

  it('calls onSelect with the exact option text on click', () => {
    const onSelect = vi.fn()
    render(<FollowUpBar options={['Ship it', 'Pause']} picked={new Set()} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: 'Ship it' }))
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('Ship it', expect.any(Object))
  })

  it('fires onSelect for both picked and unpicked chips', () => {
    const onSelect = vi.fn()
    render(<FollowUpBar options={['A', 'B']} picked={new Set(['A'])} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: 'A' }))
    fireEvent.click(screen.getByRole('button', { name: 'B' }))
    expect(onSelect).toHaveBeenCalledTimes(2)
    expect(onSelect).toHaveBeenNthCalledWith(1, 'A', expect.any(Object))
    expect(onSelect).toHaveBeenNthCalledWith(2, 'B', expect.any(Object))
  })

  it('highlights picked chips and leaves unpicked chips muted', () => {
    render(<FollowUpBar options={['Picked', 'Unpicked']} picked={new Set(['Picked'])} onSelect={() => {}} />)
    const pickedBtn = screen.getByRole('button', { name: 'Picked' })
    const unpickedBtn = screen.getByRole('button', { name: 'Unpicked' })
    // Picked: accent chip styling
    expect(pickedBtn.className).toContain('border-accent')
    expect(pickedBtn.className).toContain('text-accent')
    expect(pickedBtn.className).toContain('bg-accent-subtle')
    expect(pickedBtn.getAttribute('title')).toMatch(/remove/i)
    // Unpicked: muted styling
    expect(unpickedBtn.className).toContain('text-muted')
    expect(unpickedBtn.className).toContain('bg-bg-elevated')
    expect(unpickedBtn.getAttribute('title')).toMatch(/add to input/i)
  })

  it('is stateless — chip style changes only when the picked prop changes', () => {
    const { rerender } = render(
      <FollowUpBar options={['X']} picked={new Set()} onSelect={() => {}} />
    )
    const btn = screen.getByRole('button', { name: 'X' })
    expect(btn.className).toContain('text-muted')
    fireEvent.click(btn)
    expect(btn.className).toContain('text-muted')
    rerender(<FollowUpBar options={['X']} picked={new Set(['X'])} onSelect={() => {}} />)
    expect(screen.getByRole('button', { name: 'X' }).className).toContain('bg-accent-subtle')
  })

  it('defaults to multiline layout (flex-wrap, no shrink-0)', () => {
    const { container } = render(
      <FollowUpBar options={['A', 'B']} picked={new Set()} onSelect={() => {}} />
    )
    // outer wrapper uses flex-wrap, no horizontal scroll
    expect(container.querySelector('.flex-wrap')).toBeInTheDocument()
    expect(container.querySelector('.overflow-x-auto')).not.toBeInTheDocument()
    // chips are not pinned with shrink-0
    expect(screen.getByRole('button', { name: 'A' }).className).not.toContain('shrink-0')
  })

  it('renders single-line scrollable layout when layout="scroll"', () => {
    const { container } = render(
      <FollowUpBar options={['A', 'B']} picked={new Set()} onSelect={() => {}} layout="scroll" />
    )
    // scrollable strip, not wrapping
    expect(container.querySelector('.overflow-x-auto')).toBeInTheDocument()
    expect(container.querySelector('.flex-wrap')).not.toBeInTheDocument()
    // chips have shrink-0 so they don't squish
    expect(screen.getByRole('button', { name: 'A' }).className).toContain('shrink-0')
    // still calls onSelect with the right option + event
    const onSelect = vi.fn()
    const { rerender } = render(
      <FollowUpBar options={['Ship']} picked={new Set()} onSelect={onSelect} layout="scroll" />
    )
    void rerender
    fireEvent.click(screen.getByRole('button', { name: 'Ship' }))
    expect(onSelect).toHaveBeenCalledWith('Ship', expect.any(Object))
  })
})
