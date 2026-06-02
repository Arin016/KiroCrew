import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ChatInput', () => {
  describe('rendering', () => {
    it('renders textarea with Message input label', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByLabelText('Message input')).toBeInTheDocument()
    })

    it('renders Send button', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
    })

    it('renders drag handle', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByTitle(/Drag to resize/)).toBeInTheDocument()
    })

    it('uses custom placeholder', () => {
      renderWithProviders(<ChatInput {...defaultProps} placeholder="Type here…" />)
      expect(screen.getByPlaceholderText('Type here…')).toBeInTheDocument()
    })

    it('shows Stopping placeholder when disabled', () => {
      renderWithProviders(<ChatInput {...defaultProps} disabled />)
      expect(screen.getByPlaceholderText('Stopping…')).toBeInTheDocument()
    })
  })

  describe('send behavior', () => {
    it('disables Send button when input is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('enables Send button when input has text', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" />)
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('enables Send button when pendingFiles has items even without text', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/img.png']} />)
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('disables Send button when disabled prop is true', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" disabled />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('calls onSend on button click', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('calls onSend on Enter key when sendOnEnter is true', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('does not call onSend on Shift+Enter', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', shiftKey: true })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('does not call onSend on Enter during IME composition', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      const ta = screen.getByLabelText('Message input')
      const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
      Object.defineProperty(event, 'isComposing', { value: true })
      ta.dispatchEvent(event)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('does not send on Enter immediately after compositionEnd (50ms guard)', () => {
      vi.useFakeTimers()
      try {
        const onSend = vi.fn()
        renderWithProviders(<ChatInput {...defaultProps} value="hello" onSend={onSend} sendOnEnter={true} />)
        const ta = screen.getByLabelText('Message input')
        // Simulate: user types English in Chinese IME, presses Enter to commit
        fireEvent.compositionStart(ta)
        fireEvent.compositionEnd(ta)
        // Enter arrives immediately after compositionEnd — isComposing is false
        fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
        expect(onSend).not.toHaveBeenCalled()
        // After 50ms guard, Enter should work again
        vi.advanceTimersByTime(50)
        fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
        expect(onSend).toHaveBeenCalledOnce()
      } finally {
        vi.useRealTimers()
      }
    })

    it('does not call onSend on Enter when sendOnEnter is false', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="ctrl-enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('calls onSend on Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter-ctrl-newline" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('does not call onSend on Ctrl+Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter-ctrl-newline" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('inserts newline on Ctrl+Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="hello" onChange={onChange} sendOnEnter="enter-ctrl-newline" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      Object.defineProperty(ta, 'selectionStart', { value: 3, writable: true })
      Object.defineProperty(ta, 'selectionEnd', { value: 3, writable: true })
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onChange).toHaveBeenCalledWith('hel\nlo')
    })
  })

  describe('onChange', () => {
    it('calls onChange when typing', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} />)
      fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'hi' } })
      expect(onChange).toHaveBeenCalledWith('hi')
    })
  })

  describe('prefill hint', () => {
    it('shows prefill hint when enabled', () => {
      renderWithProviders(<ChatInput {...defaultProps} prefillHint />)
      expect(screen.getByText(/Plan pre-filled/)).toBeInTheDocument()
    })

    it('does not show prefill hint by default', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByText(/Plan pre-filled/)).not.toBeInTheDocument()
    })
  })

  describe('file action buttons', () => {
    it('does not show attach/screenshot buttons by default', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByTitle('Attach file')).not.toBeInTheDocument()
      expect(screen.queryByTitle(/Screenshot/)).not.toBeInTheDocument()
    })

    it('shows attach button with onUploadFiles', () => {
      renderWithProviders(<ChatInput {...defaultProps} onUploadFiles={vi.fn()} />)
      expect(screen.getByTitle('Attach file')).toBeInTheDocument()
    })

    it('shows screenshot button on macOS with onScreenshot', () => {
      renderWithProviders(<ChatInput {...defaultProps} isMac onScreenshot={vi.fn()} />)
      expect(screen.getByTitle(/Screenshot/)).toBeInTheDocument()
    })

    it('clicking attach button triggers hidden file input click', () => {
      const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click')
      renderWithProviders(<ChatInput {...defaultProps} onUploadFiles={vi.fn()} />)
      fireEvent.click(screen.getByTitle('Attach file'))
      expect(clickSpy).toHaveBeenCalled()
      clickSpy.mockRestore()
    })

    it('disables file buttons when uploading', () => {
      renderWithProviders(<ChatInput {...defaultProps} isMac onUploadFiles={vi.fn()} onScreenshot={vi.fn()} uploading />)
      expect(screen.getByTitle('Attach file')).toBeDisabled()
      expect(screen.getByTitle(/Screenshot/)).toBeDisabled()
    })
  })

  describe('drag-to-resize handle', () => {
    it('initiates drag on mousedown on handle', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="test" />)
      const handle = screen.getByTitle(/Drag to resize/)
      fireEvent.mouseDown(handle, { clientX: 100, clientY: 200 })
      expect(document.body.style.cursor).toBe('row-resize')
      expect(document.body.style.userSelect).toBe('none')
      // Clean up
      fireEvent.mouseUp(window)
    })

    it('resets height on double-click', () => {
      localStorage.setItem('mc-input-height', '300')
      renderWithProviders(<ChatInput {...defaultProps} value="test" />)
      const handle = screen.getByTitle(/Drag to resize/)
      fireEvent.doubleClick(handle)
      expect(localStorage.getItem('mc-input-height')).toBeNull()
    })
  })

  describe('drag-and-drop zone', () => {
    it('calls onDrop when files are dropped', () => {
      const onDrop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onDrop={onDrop} />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.drop(ta)
      expect(onDrop).toHaveBeenCalledOnce()
    })

    it('forwards dragover on textarea to parent handler', () => {
      const onDragOver = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onDragOver={onDragOver} />)
      fireEvent.dragOver(screen.getByLabelText('Message input'))
      expect(onDragOver).toHaveBeenCalledOnce()
    })

    it('stops drop propagation so parent drop zone does not double-fire', () => {
      const childOnDrop = vi.fn()
      const parentOnDrop = vi.fn()
      renderWithProviders(
        <div onDrop={parentOnDrop}>
          <ChatInput {...defaultProps} onDrop={childOnDrop} />
        </div>
      )
      fireEvent.drop(screen.getByLabelText('Message input'))
      expect(childOnDrop).toHaveBeenCalledOnce()
      expect(parentOnDrop).not.toHaveBeenCalled()
    })
  })

  describe('file preview strip', () => {
    it('does not render strip when pendingFiles is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      expect(screen.queryByRole('img')).not.toBeInTheDocument()
    })

    it('renders thumbnails for pending images', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png', '/tmp/b.png']} />)
      const imgs = screen.getAllByRole('img')
      expect(imgs).toHaveLength(2)
    })

    it('calls onRemoveFile when ✕ clicked', () => {
      const onRemove = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} onRemoveFile={onRemove} />)
      fireEvent.click(screen.getByTitle('Remove'))
      expect(onRemove).toHaveBeenCalledWith('/tmp/a.png')
    })

    it('renders file chip for non-image files', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/code.ts']} />)
      expect(screen.getByText('code.ts')).toBeInTheDocument()
      expect(screen.queryByRole('img')).not.toBeInTheDocument()
    })

    it('dispatches lightbox event when thumbnail clicked', () => {
      const spy = vi.fn()
      window.addEventListener('lightbox', spy)
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      fireEvent.click(screen.getByRole('img'))
      expect(spy).toHaveBeenCalledOnce()
      window.removeEventListener('lightbox', spy)
    })

    it('increases wrapper minHeight when files are attached and manually sized', () => {
      localStorage.setItem('mc-input-height', '150')
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      const wrapper = container.firstElementChild as HTMLElement
      // With files: minHeight should be INPUT_DRAG_MIN_H (93) + FILE_PREVIEW_H (81) = 174
      expect(wrapper.style.minHeight).toBe('174px')
    })

    it('uses base minHeight when no files attached and manually sized', () => {
      localStorage.setItem('mc-input-height', '150')
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = container.firstElementChild as HTMLElement
      // Without files: minHeight should be INPUT_DRAG_MIN_H (93)
      expect(wrapper.style.minHeight).toBe('93px')
    })

    it('wrapper uses flex-col layout for proper space distribution with file strip', () => {
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      const wrapper = container.firstElementChild as HTMLElement
      expect(wrapper.className).toContain('flex-col')
    })

    it('grows wrapper height when files are added with manual sizing', () => {
      localStorage.setItem('mc-input-height', '200')
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = screen.getByTestId('input-wrapper')
      expect(wrapper.style.height).toBe('200px')
      rerender(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      // 200 + FILE_PREVIEW_H (81) = 281
      expect(wrapper.style.height).toBe('281px')
    })

    it('shrinks wrapper height when files are removed with manual sizing', () => {
      localStorage.setItem('mc-input-height', '281')
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      rerender(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = screen.getByTestId('input-wrapper')
      // 281 - FILE_PREVIEW_H (81) = 200
      expect(wrapper.style.height).toBe('200px')
    })
  })

  describe('prompt history', () => {
    const sent = ['first', 'second', 'third']

    it('ArrowUp on empty input recalls newest message', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
    })

    it('repeated ArrowUp walks from newest to oldest', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenNthCalledWith(1, 'third')
      expect(onChange).toHaveBeenNthCalledWith(2, 'second')
      expect(onChange).toHaveBeenNthCalledWith(3, 'first')
    })

    it('ArrowUp at oldest stays on oldest (does not wrap)', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="first" />)
      ta.setSelectionRange(0, 0)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowDown past newest restores saved draft', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="my draft" />)
      const ta = screen.getByLabelText('Message input')
      // Caret at start so ArrowUp engages history
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      // Caret at end so ArrowDown engages history-exit
      ta.setSelectionRange('third'.length, 'third'.length)
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).toHaveBeenLastCalledWith('my draft')
    })

    it('ArrowDown within history recalls the next newer message', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Go up three times to reach "first"
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="first" />)
      // Now go down one step — should recall "second"
      ta.setSelectionRange('first'.length, 'first'.length)
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).toHaveBeenLastCalledWith('second')
    })

    it('ArrowUp is ignored when caret is mid-text and value non-empty', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="hello world" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      ta.setSelectionRange(5, 5)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowDown is ignored when not in history mode', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowDown' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowUp with no sent messages is a no-op', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={[]} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ignores ArrowUp with modifier keys (leaves native navigation)', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp', metaKey: true })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ignores ArrowUp when slash command menu is open', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Typing "/" opens the slash menu via internal onChange
      fireEvent.change(ta, { target: { value: '/' } })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="/" />)
      onChange.mockClear()
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('editing a recalled message exits history mode so next ArrowUp starts from newest', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Enter history, recall 'third'
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      // User edits the recalled text — useEffect resets historyIdxRef to -1
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third edited" />)
      // Next ArrowUp starts from newest again, not from the stale index
      ta.setSelectionRange(0, 0)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
    })

    it('ArrowDown in history mode is ignored when caret is not at end', () => {
      const onChange = vi.fn()
      const multiLine = ['first', 'line1\nline2', 'third']
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      // JS expression so the value contains a real newline matching the array entry.
      // A string attribute like value="line1\nline2" would pass literal backslash-n
      // and mismatch the recalled message, exiting history mode before ArrowDown fires.
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value={'line1\nline2'} />)
      // Caret mid-text, not at end — native cursor movement preserved
      ta.setSelectionRange(3, 3)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('moves caret to start after ArrowUp recall so repeated ↑ re-engages history', () => {
      const rafCbs: FrameRequestCallback[] = []
      const rafSpy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { rafCbs.push(cb); return 0 })
      try {
        const onChange = vi.fn()
        const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
        const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
        fireEvent.keyDown(ta, { key: 'ArrowUp' })
        // Commit the recalled value before the deferred caret-move runs (real browser ordering).
        rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
        rafCbs.forEach(cb => cb(0))
        expect(ta.selectionStart).toBe(0)
        expect(ta.selectionEnd).toBe(0)
      } finally {
        rafSpy.mockRestore()
      }
    })
  })

  // ── Reasoning effort button (Mesh-1412) ──
  describe('reasoning effort button', () => {
    it('renders for claude_code provider', () => {
      const onClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="claude_code"
          reasoningEffort="high"
          onReasoningEffortClick={onClick}
        />
      )
      expect(screen.getByTitle(/Reasoning: High/)).toBeInTheDocument()
    })

    it('renders Default label when effort is empty', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="claude_code"
          reasoningEffort=""
          onReasoningEffortClick={vi.fn()}
        />
      )
      expect(screen.getByTitle(/Reasoning: Default/)).toBeInTheDocument()
    })

    it('shown when onReasoningEffortClick provided regardless of providerId', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort="high"
          onReasoningEffortClick={vi.fn()}
        />
      )
      expect(screen.getByTitle(/Reasoning: High/)).toBeInTheDocument()
    })

    it('hidden when handler missing even on supported provider', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} providerId="claude_code" reasoningEffort="high" />
      )
      expect(screen.queryByTitle(/Reasoning:/)).not.toBeInTheDocument()
    })

    it('shown when providerId is undefined but callback provided', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} reasoningEffort="high" onReasoningEffortClick={vi.fn()} />
      )
      expect(screen.getByTitle(/Reasoning: High/)).toBeInTheDocument()
    })

    it('disabled while running', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="claude_code"
          reasoningEffort="medium"
          onReasoningEffortClick={vi.fn()}
          isRunning
        />
      )
      const btn = screen.getByTitle(/Stop the current response to switch reasoning effort/)
      expect(btn).toBeDisabled()
    })

    it('invokes handler with click rect', () => {
      const onClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="claude_code"
          reasoningEffort="low"
          onReasoningEffortClick={onClick}
        />
      )
      fireEvent.click(screen.getByTitle(/Reasoning: Low/))
      expect(onClick).toHaveBeenCalledOnce()
      // First arg should be a DOMRect-like object
      expect(onClick.mock.calls[0][0]).toBeTruthy()
    })
  })

  describe('prompt optimizer (slot binding)', () => {
    // jsdom doesn't implement document.execCommand. Install a stub before each
    // test so we can spy on the call site that setTextUndoable uses to write
    // optimized text into the textarea.
    let originalExec: typeof document.execCommand | undefined
    beforeEach(() => {
      originalExec = (document as any).execCommand // eslint-disable-line @typescript-eslint/no-explicit-any
      ;(document as any).execCommand = () => true // eslint-disable-line @typescript-eslint/no-explicit-any
    })
    afterEach(() => {
      if (originalExec === undefined) delete (document as any).execCommand // eslint-disable-line @typescript-eslint/no-explicit-any
      else (document as any).execCommand = originalExec // eslint-disable-line @typescript-eslint/no-explicit-any
    })

    it('drops optimize result when value changed mid-flight (e.g. user switched chat tabs)', async () => {
      let resolveFetch: ((value: Response) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(res => { resolveFetch = res })
      )
      // Spy on document.execCommand — setTextUndoable writes the optimized
      // text via execCommand('insertText', ...). If the guard fires, this is
      // never called for the optimized text. Asserting on onChange would be
      // vacuous because jsdom treats execCommand as a no-op so onChange
      // wouldn't fire either way. Removing the guard from ChatInput's
      // onSuccess makes this assertion fail (verified locally), so the test
      // exercises the guard rather than passing trivially.
      const execSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      try {
        const onChange = vi.fn()
        const { rerender } = renderWithProviders(
          <ChatInput {...defaultProps} value="fix bug" onChange={onChange} />,
        )
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        // Let the mutation start so variables.prompt is captured at "fix bug".
        await new Promise(r => setTimeout(r, 10))
        // Simulate the user switching slots: parent re-renders with a different
        // slot's draft. This is the exact pattern ChatPage uses on slot change.
        rerender(<ChatInput {...defaultProps} value="review CR-123" onChange={onChange} />)
        await new Promise(r => setTimeout(r, 10))
        execSpy.mockClear()
        resolveFetch!(new Response(
          JSON.stringify({ optimized: 'OPTIMIZED FIX BUG WITH MORE DETAIL', changed: true }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
        // Wait long enough for any deferred onSuccess to settle.
        await new Promise(r => setTimeout(r, 50))
        // The guard must short-circuit BEFORE setTextUndoable runs, so
        // execCommand('insertText', ..., '<optimized text>') must never fire.
        const insertCalls = execSpy.mock.calls.filter(c => c[0] === 'insertText')
        expect(insertCalls).toEqual([])
      } finally {
        execSpy.mockRestore()
        fetchSpy.mockRestore()
      }
    })

    it('drops optimize fallback when fetch fails after a slot switch (onError path)', async () => {
      // Same hazard as the success path: when the optimizer fetch fails after
      // the user has switched slots, onError must NOT call setTextUndoable on
      // the (now different) textarea. Without the guard, the fallback
      // setTextUndoable(valueRef.current.trim()) re-issues a focus/select on
      // the new slot's textarea — visible UX glitch even though the inserted
      // text matches what's already there.
      let rejectFetch: ((reason?: Error) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>((_res, rej) => { rejectFetch = rej })
      )
      const execSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      // Suppress the console.warn the onError path emits so the test output
      // stays clean.
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      try {
        const onChange = vi.fn()
        const { rerender } = renderWithProviders(
          <ChatInput {...defaultProps} value="fix bug" onChange={onChange} />,
        )
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10))
        rerender(<ChatInput {...defaultProps} value="review CR-123" onChange={onChange} />)
        await new Promise(r => setTimeout(r, 10))
        execSpy.mockClear()
        rejectFetch!(new Error('network'))
        await new Promise(r => setTimeout(r, 50))
        const insertCalls = execSpy.mock.calls.filter(c => c[0] === 'insertText')
        expect(insertCalls).toEqual([])
      } finally {
        warnSpy.mockRestore()
        execSpy.mockRestore()
        fetchSpy.mockRestore()
      }
    })
  })

  describe('autoFocusKey', () => {
    it('focuses textarea on first non-null key', () => {
      renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(screen.getByLabelText('Message input')).toHaveFocus()
    })

    it('focuses textarea when key changes', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).toHaveFocus()
    })

    it('does not re-focus on a re-render with the same key', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(ta).not.toHaveFocus()
    })

    it('does not re-focus when disabled flips false on the same key (e.g. AI finishes responding)', () => {
      // Once a key has been focused, subsequent disabled flips for the SAME key
      // must not steal focus back from a user who is reading or scrolling.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      expect(ta).toHaveFocus()
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled={false} />)
      expect(ta).not.toHaveFocus()
    })

    it('defers focus when key changes while disabled, then applies it once disabled clears', () => {
      // User picks a different session while it is still stopping (disabled=true).
      // Focus must NOT be lost — it should land once disabled flips false.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled={false} />)
      expect(ta).toHaveFocus()
    })

    it('does not steal focus from another input element', () => {
      const { rerender } = renderWithProviders(
        <>
          <input data-testid="other" />
          <ChatInput {...defaultProps} autoFocusKey="A" />
        </>,
      )
      const other = screen.getByTestId('other')
      other.focus()
      expect(other).toHaveFocus()
      rerender(
        <>
          <input data-testid="other" />
          <ChatInput {...defaultProps} autoFocusKey="B" />
        </>,
      )
      expect(other).toHaveFocus()
    })
  })

  describe('Quick Send', () => {
    it('passes quickSend to FollowUpBar when options present', () => {
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['A', 'B']} followUpPicked={new Set()} onFollowUpSelect={vi.fn()} quickSend={true} />)
      expect(screen.getAllByTitle(/Click to send instantly/).length).toBeGreaterThan(0)
    })

    it('fires onFollowUpSelect with MouseEvent on option click', () => {
      const onSelect = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['Go']} followUpPicked={new Set()} onFollowUpSelect={onSelect} quickSend={true} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }))
      expect(onSelect).toHaveBeenCalledWith('Go', expect.any(Object))
    })
  })

  describe('stop button', () => {
    it('shows armed Stop button while running, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} />)
      const btn = screen.getByTestId('stop-button-armed')
      expect(btn).toHaveAttribute('aria-label', 'Stop generation')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('shows pulsing Force stop button when soft_pending, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} stopState="soft_pending" />)
      const btn = screen.getByTestId('stop-button-pulsing')
      expect(btn).toHaveAttribute('aria-label', 'Force stop')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('keeps showing the stop affordance when soft_pending even after isRunning flips false', () => {
      // Regression: the chat_done event clears isRunning before the backend
      // flips stop_state back to idle. The button must NOT revert to Send mid-stop.
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="soft_pending" />)
      expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
    })

    it('shows disabled killing spinner when stopState is killing (after isRunning false)', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="killing" />)
      const btn = screen.getByRole('button', { name: 'Killing session' })
      expect(btn).toBeDisabled()
      fireEvent.click(btn)
      expect(onStop).not.toHaveBeenCalled()
    })

    it('shows Queue message button (calls onSend) when running with pending text', () => {
      const onSend = vi.fn()
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="more" isRunning onStop={onStop} onSend={onSend} />)
      const btn = screen.getByRole('button', { name: 'Queue message' })
      fireEvent.click(btn)
      expect(onSend).toHaveBeenCalled()
      expect(onStop).not.toHaveBeenCalled()
    })
  })

  describe('global "/" focus shortcut', () => {
    it('focuses textarea when "/" is pressed outside any input', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ;(ta as HTMLElement).blur()
      fireEvent.keyDown(document, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })

    it('does not intercept "/" when already typing in the textarea', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ta.focus()
      fireEvent.keyDown(ta, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })
  })
})
