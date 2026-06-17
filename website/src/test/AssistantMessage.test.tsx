import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage, { parseOptions } from '../pages/chat/AssistantMessage'

// Mock MarkdownRenderer to avoid complex markdown parsing in tests
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
// Mock useSmoothStream to passthrough — its rAF loop conflicts with vi.useFakeTimers()
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))
import { copySessionLink } from '../utils/shareUrl'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

describe('AssistantMessage', () => {
  it('renders markdown content', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md')).toHaveTextContent('Hello world')
  })

  it('does not add streaming-cursor class (replaced by inline gradient)', () => {
    const { container } = render(<AssistantMessage content="typing…" isStreaming={true} slotRunning={true} />)
    expect(container.querySelector('.streaming-cursor')).not.toBeInTheDocument()
  })

  it('does not render inline option buttons (options are surfaced via FollowUpBar now)', () => {
    render(<AssistantMessage content="Pick [OPTIONS: Alpha|Beta]" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
    expect(screen.queryByText('Beta')).not.toBeInTheDocument()
    expect(screen.queryByText(/Send/)).not.toBeInTheDocument()
  })

  it('shows "Use as Plan" button for valid plan JSON', () => {
    const planContent = '<!-- plan_task_id:test-123 -->\nHere is the plan:\n```json\n[{"title":"Step 1","description":"Do thing"}]\n```'
    render(<AssistantMessage content={planContent} isStreaming={false} slotRunning={false} planTaskId="test-123" onApplyPlan={() => Promise.resolve(true)} />)
    expect(screen.getByText(/Use as Plan/)).toBeInTheDocument()
  })

  it('does not show plan button while streaming', () => {
    const planContent = '```json\n[{"title":"Step 1","description":"Do thing"}]\n```'
    render(<AssistantMessage content={planContent} isStreaming={true} slotRunning={true} planTaskId="test-123" onApplyPlan={() => Promise.resolve(true)} />)
    expect(screen.queryByText(/Use as Plan/)).not.toBeInTheDocument()
  })

  it('shows regenerate button when onRegenerate is provided and not streaming/running', () => {
    const onRegenerate = vi.fn()
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} onRegenerate={onRegenerate} />)
    const btn = screen.getByTitle('Regenerate')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('hides regenerate button while slot is running', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={true} onRegenerate={() => {}} />)
    expect(screen.queryByTitle('Regenerate')).not.toBeInTheDocument()
  })

  it('shows variant arrows when multiple variants exist and calls onSwitchVariant', () => {
    const onSwitch = vi.fn()
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v2" isStreaming={false} slotRunning={false} variants={variants} variantIdx={1} onSwitchVariant={onSwitch} />)
    expect(screen.getByText('2/3')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(onSwitch).toHaveBeenCalledWith(0)
    fireEvent.click(screen.getByTitle('Next version'))
    expect(onSwitch).toHaveBeenCalledWith(2)
  })

  it('disables previous arrow at first variant', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    render(<AssistantMessage content="v1" isStreaming={false} slotRunning={false} variants={variants} variantIdx={0} onSwitchVariant={() => {}} />)
    expect(screen.getByTitle('Previous version')).toBeDisabled()
    expect(screen.getByTitle('Next version')).not.toBeDisabled()
  })

  it('does not render variant arrows when only one variant', () => {
    const variants = [{ content: 'v1' }]
    render(<AssistantMessage content="v1" isStreaming={false} slotRunning={false} variants={variants} variantIdx={0} onSwitchVariant={() => {}} />)
    expect(screen.queryByTitle('Previous version')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Next version')).not.toBeInTheDocument()
  })

  it('disables variant arrows when slotRunning', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} onSwitchVariant={() => {}} slotRunning={true} />)
    expect(screen.getByTitle('Previous version')).toBeDisabled()
    expect(screen.getByTitle('Next version')).toBeDisabled()
  })

  it('does not show regenerate button when onRegenerate not provided', () => {
    render(<AssistantMessage content="hello" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTitle('Regenerate')).not.toBeInTheDocument()
  })

  it('shows read-only variant nav when onSwitchVariant not provided', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    render(<AssistantMessage content="v1" isStreaming={false} variants={variants} variantIdx={0} />)
    expect(screen.getByTitle('Previous version')).toBeInTheDocument()
  })

  it('defaults to last variant index when variantIdx omitted', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v3" isStreaming={false} variants={variants} onSwitchVariant={() => {}} />)
    expect(screen.getByText('3/3')).toBeInTheDocument()
  })

  it('local variant browsing changes displayed content without calling API', () => {
    const variants = [{ content: 'version one text' }, { content: 'version two text' }]
    render(<AssistantMessage content="version two text" isStreaming={false} variants={variants} variantIdx={1} />)
    expect(screen.getByText('2/2')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(screen.getByText('1/2')).toBeInTheDocument()
    expect(screen.getByTestId('md')).toHaveTextContent('version one text')
  })

  it('calls onSwitchVariant for last message but uses local state for older messages', () => {
    const apiSwitch = vi.fn()
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    const { unmount } = render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} onSwitchVariant={apiSwitch} />)
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(apiSwitch).toHaveBeenCalledWith(0)
    unmount()
    render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} />)
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(screen.getByTestId('md')).toHaveTextContent('v1')
  })

  it('renders fork button when onFork is provided and calls it on click', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} forkIndex={0} />)
    const forkBtn = screen.getByTitle('Fork conversation from here')
    fireEvent.click(forkBtn)
    expect(onFork).toHaveBeenCalledTimes(1)
    expect(onFork).toHaveBeenCalledWith(0)
  })

  it('does not render fork button when onFork is undefined', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  it('does not render fork button when forkIndex is undefined (gated by parent)', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  it('does not render fork button while streaming', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="typing…" isStreaming={true} slotRunning={true} onFork={onFork} forkIndex={0} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })
})

describe('parseOptions', () => {
  it('parses [OPTIONS: a|b|c] multi syntax', () => {
    const { options, multi, isPlan } = parseOptions('Pick one [OPTIONS: Alpha|Beta|Gamma]')
    expect(options).toEqual(['Alpha', 'Beta', 'Gamma'])
    expect(multi).toBe(true)
    expect(isPlan).toBe(false)
  })

  it('parses [OPTION: a|b] single syntax', () => {
    const { options, multi } = parseOptions('Yes or no? [OPTION: Yes|No]')
    expect(options).toEqual(['Yes', 'No'])
    expect(multi).toBe(false)
  })

  it('returns empty options for content without markers', () => {
    const { options } = parseOptions('Just regular content')
    expect(options).toEqual([])
  })

  it('flags isPlan when both plan header and stage marker present', () => {
    const content = '📋 Plan for: foo\n\nStage 1: do thing\n[OPTION: approved|rejected]'
    const { isPlan } = parseOptions(content)
    expect(isPlan).toBe(true)
  })

  it('strips the option marker from parsed text', () => {
    const { text } = parseOptions('Pick [OPTIONS: A|B]')
    expect(text).toBe('Pick')
  })

  it('shows "Copy link to message" button when messageTs and slotKey are provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" />)
    expect(screen.getByTitle('Copy link to message')).toBeInTheDocument()
  })

  it('hides "Copy link to message" button when messageTs is not provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('hides "Copy link to message" button when slotKey is not provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('calls copySessionLink with correct args on link button click', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" mode="orchestrator" />)
    fireEvent.click(screen.getByTitle('Copy link to message'))
    expect(copySessionLink).toHaveBeenCalledWith('chat-1', 'My Chat', '2025-05-13T14:00:00.000Z', 'orchestrator')
  })

  it('does not show "Copy link to message" while streaming', () => {
    render(<AssistantMessage content="typing" isStreaming={true} slotRunning={true} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })
})
