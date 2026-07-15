/**
 * Regression: the conversation-map (ChatProgressTrack) panel must close when
 * the user clicks or focuses away.
 *
 * The bug this pins: on open the panel focuses itself (panelRef.focus()), so the
 * onMouseLeave handler's "focus is inside" guard keeps it open once the pointer
 * leaves. With no outside-click handler, the panel stayed stuck open. The fix
 * adds capture-phase pointerdown + focusin listeners that dismiss it when the
 * event target is outside both the trigger and the panel.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ChatProgressTrack from '../pages/chat/ChatProgressTrack'
import type { ChatSection } from '../hooks/useChatNavigation'

const sections: ChatSection[] = [
  { label: 'first prompt', msgIdx: 0, displayIdx: 0 },
  { label: 'second prompt', msgIdx: 2, displayIdx: 2 },
  { label: 'third prompt', msgIdx: 4, displayIdx: 4 },
]

function renderTrack() {
  render(
    <ChatProgressTrack sections={sections} currentIdx={0} onScrollToSection={() => {}} />,
  )
  return screen.getByRole('button', { name: /Chat progress/ })
}

describe('ChatProgressTrack outside dismissal', () => {
  it('closes when clicking outside the panel', () => {
    const trigger = renderTrack()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    // Open via hover on the nav container.
    fireEvent.mouseEnter(screen.getByRole('navigation', { name: 'Chat progress' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    // Click somewhere outside — panel should dismiss.
    fireEvent.pointerDown(document.body)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('closes when focus moves outside the panel', () => {
    const trigger = renderTrack()
    fireEvent.mouseEnter(screen.getByRole('navigation', { name: 'Chat progress' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    const outside = document.createElement('button')
    document.body.appendChild(outside)
    fireEvent.focusIn(outside)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    outside.remove()
  })

  it('stays open when clicking inside the panel', () => {
    const trigger = renderTrack()
    fireEvent.mouseEnter(screen.getByRole('navigation', { name: 'Chat progress' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    // A pointerdown that originates inside the listbox must not dismiss.
    fireEvent.pointerDown(screen.getByRole('listbox', { name: 'Conversation sections' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
  })
})
