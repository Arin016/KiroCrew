import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })
import { copyToClipboard } from '../utils/clipboard'
import { copySessionLink } from '../utils/shareUrl'

const renderContent = (content: string) => <span data-testid="content">{content}</span>

describe('UserMessage', () => {
  it('renders message content', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTestId('content')).toHaveTextContent('hello')
  })

  it('shows timestamp when provided', () => {
    render(<UserMessage content="hi" timestamp="Apr 27, 2026, 08:00 PM" renderContent={renderContent} />)
    expect(screen.getByText('Apr 27, 2026, 08:00 PM')).toBeInTheDocument()
  })

  it('hides timestamp when not provided', () => {
    const { container } = render(<UserMessage content="hi" renderContent={renderContent} />)
    expect(container.querySelector('.font-mono')).not.toBeInTheDocument()
  })

  it('shows edit button when onEditResend is provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    expect(screen.getByTitle('Edit & Resend')).toBeInTheDocument()
  })

  it('hides edit button when onEditResend is not provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} />)
    expect(screen.queryByTitle('Edit & Resend')).not.toBeInTheDocument()
  })

  it('hides edit button when canEdit is false', () => {
    render(<UserMessage content="hi" renderContent={renderContent} onEditResend={() => {}} />)
    expect(screen.queryByTitle('Edit & Resend')).not.toBeInTheDocument()
  })

  it('enters edit mode on pencil click', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    expect(screen.getByRole('textbox')).toHaveValue('original')
    expect(screen.getByText('Send')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('cancels edit on Cancel click', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.click(screen.getByText('Cancel'))
    // Back to view mode
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByTestId('content')).toBeInTheDocument()
  })

  it('cancels edit on Escape key', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('calls onEditResend with new content on Send click', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'edited')
  })

  it('calls onEditResend on Enter key (without Shift)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'new msg' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: false })
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'new msg')
  })

  it('does not submit on Shift+Enter (allows newline)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: true })
    expect(onEditResend).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toBeInTheDocument() // still editing
  })

  it('does not call onEditResend when content is empty', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).not.toHaveBeenCalled()
  })

  it('allows resend with same content (acts as regenerate)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="same" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'same')
  })

  it('trims whitespace before sending', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  trimmed  ' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'trimmed')
  })

  it('shows copy button always', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTitle('Copy')).toBeInTheDocument()
  })

  it('copies content to clipboard on copy click', async () => {
    render(<UserMessage content="copy me" renderContent={renderContent} />)
    fireEvent.click(screen.getByTitle('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('copy me')
  })

  it('shows "Copy link to message" button when slotKey and messageTs are provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" />)
    expect(screen.getByTitle('Copy link to message')).toBeInTheDocument()
  })

  it('hides "Copy link to message" button when messageTs is empty', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="" slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('hides "Copy link to message" button when slotKey is not provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('calls copySessionLink with correct args on link button click', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" mode="orchestrator" />)
    fireEvent.click(screen.getByTitle('Copy link to message'))
    expect(copySessionLink).toHaveBeenCalledWith('chat-1', 'My Chat', '2025-05-13T14:00:00.000Z', 'orchestrator')
  })

  it('exits edit mode after successful send', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'new' } })
    fireEvent.click(screen.getByText('Send'))
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})
