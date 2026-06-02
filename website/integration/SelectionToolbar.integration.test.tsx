import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { useSelectionActions, type SelectionAction } from '../src/components/SelectionToolbar'

// Mock framer-motion to skip animations (immediate mount/unmount)
vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => children,
  motion: { div: ({ children, ...props }: any) => <div {...props}>{children}</div> },
}))

// jsdom doesn't implement Range.getBoundingClientRect
if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = function () {
    return new DOMRect(10, 10, 100, 20)
  }
}

function mockSelection(container: HTMLElement, text: string) {
  const textNode = container.firstChild as Text
  const range = document.createRange()
  range.setStart(textNode, 0)
  range.setEnd(textNode, text.length)

  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function Wrapper({ actions, children }: { actions: SelectionAction[]; children: string }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div>
      <div ref={ref} data-testid="container">{children}</div>
      <SelectionToolbar containerRef={ref} actions={actions} />
    </div>
  )
}

describe('SelectionToolbar', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); window.getSelection()?.removeAllRanges() })

  it('shows toolbar after mouseup with text selected inside container', async () => {
    const onClick = vi.fn()
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when selection is empty', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    window.getSelection()?.removeAllRanges()
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('calls action onClick with selected text when button is clicked', () => {
    const onClick = vi.fn()
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(onClick).toHaveBeenCalledWith('Hello World', expect.any(DOMRect))
  })

  it('stays visible after copy action (does not dismiss)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('hides after non-copy action (e.g. comment)', () => {
    const actions: SelectionAction[] = [{ id: 'comment', icon: null, label: 'Comment', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    expect(screen.queryByRole('button', { name: 'Comment' })).not.toBeInTheDocument()
  })

  it('hides on Escape key', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.keyUp(document, { key: 'Escape' })
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('hides on mousedown outside container and toolbar', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('does not hide on mousedown inside container (new selection)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    fireEvent.mouseDown(container)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not reposition on mouseup inside toolbar (copy click)', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    const btn = screen.getByRole('button', { name: 'Copy' })
    // mouseup on toolbar button should not re-trigger checkSelection
    fireEvent.mouseUp(btn, { clientX: 200, clientY: 200 })
    act(() => { vi.advanceTimersByTime(20) })

    // Toolbar still there, not repositioned (if it had run checkSelection
    // with the new mouse position, it would have set pos to {200, 208})
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('prevents default on button mousedown to preserve selection', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    const btn = screen.getByRole('button', { name: 'Copy' })
    const event = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    const prevented = !btn.dispatchEvent(event)
    expect(prevented).toBe(true)
  })

  it('renders multiple actions', () => {
    const actions: SelectionAction[] = [
      { id: 'comment', icon: null, label: 'Comment', onClick: vi.fn() },
      { id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() },
    ]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Comment' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when selection is outside the container', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(
      <div>
        <Wrapper actions={actions}>Inside</Wrapper>
        <div data-testid="outside">Outside text</div>
      </div>
    )

    const outside = screen.getByTestId('outside')
    // Select text in the outside element
    const textNode = outside.firstChild as Text
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 7)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)

    fireEvent.mouseUp(document, { clientX: 200, clientY: 100 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('positions toolbar using selection rect center for keyboard selection', () => {
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<Wrapper actions={actions}>Hello World</Wrapper>)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    // Trigger via keyboard (Shift key up) instead of mouse
    fireEvent.keyUp(document, { key: 'ArrowRight', shiftKey: true })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('does not show toolbar when container ref is null', () => {
    function NullRefWrapper({ actions }: { actions: SelectionAction[] }) {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div>
          <div data-testid="container">Some text</div>
          <SelectionToolbar containerRef={ref} actions={actions} />
        </div>
      )
    }
    const actions: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: vi.fn() }]
    render(<NullRefWrapper actions={actions} />)

    const container = screen.getByTestId('container')
    mockSelection(container, 'Some text')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('calls copyToClipboard via useSelectionActions copy action', () => {
    // Mock clipboard for jsdom
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    function UseActionsWrapper() {
      const ref = useRef<HTMLDivElement>(null)
      const actions = useSelectionActions()
      return (
        <div>
          <div ref={ref} data-testid="container">Hello World</div>
          <SelectionToolbar containerRef={ref} actions={actions} />
        </div>
      )
    }
    render(<UseActionsWrapper />)
    const container = screen.getByTestId('container')
    mockSelection(container, 'Hello World')
    fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
    act(() => { vi.advanceTimersByTime(20) })

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith('Hello World')
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })
})

describe('useSelectionActions', () => {
  function HookWrapper({ onQuote }: { onQuote?: (text: string, rect: DOMRect) => void }) {
    const actions = useSelectionActions(onQuote)
    return (
      <div data-testid="actions">
        {actions.map(a => <span key={a.id} data-testid={a.id}>{a.label}</span>)}
      </div>
    )
  }

  it('returns only Copy when no onQuote provided', () => {
    render(<HookWrapper />)
    expect(screen.getByTestId('copy')).toBeInTheDocument()
    expect(screen.queryByTestId('quote')).not.toBeInTheDocument()
  })

  it('returns Quote and Copy when onQuote provided', () => {
    render(<HookWrapper onQuote={vi.fn()} />)
    expect(screen.getByTestId('quote')).toBeInTheDocument()
    expect(screen.getByTestId('copy')).toBeInTheDocument()
  })
})
