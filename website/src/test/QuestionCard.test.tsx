import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import QuestionCard from '../components/QuestionCard'

describe('QuestionCard', () => {
  const singleQuestion = [{
    question: 'What is your favorite color?',
    header: 'Preference',
    options: [
      { label: 'Red', description: 'A warm color' },
      { label: 'Blue', description: 'A cool color' },
      { label: 'Green', description: 'Nature color' },
    ],
    multiSelect: false,
  }]

  const multiQuestion = [{
    question: 'Which features do you want?',
    header: 'Features',
    options: [
      { label: 'Dark mode', description: 'Less eye strain' },
      { label: 'Notifications', description: 'Stay updated' },
    ],
    multiSelect: true,
  }]

  it('renders question text and options', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('What is your favorite color?')).toBeInTheDocument()
    expect(screen.getByText('Preference')).toBeInTheDocument()
    expect(screen.getByText('Red')).toBeInTheDocument()
    expect(screen.getByText('Blue')).toBeInTheDocument()
    expect(screen.getByText('A warm color')).toBeInTheDocument()
  })

  it('selecting an option highlights it', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const redBtn = screen.getByText('Red').closest('button')!
    fireEvent.click(redBtn)
    expect(redBtn.className).toContain('border-accent')
  })

  it('single-select deselects previous option', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    fireEvent.click(screen.getByText('Blue').closest('button')!)
    expect(screen.getByText('Red').closest('button')!.className).not.toContain('bg-accent-subtle')
    expect(screen.getByText('Blue').closest('button')!.className).toContain('bg-accent-subtle')
  })

  it('multi-select allows multiple selections', () => {
    render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    expect(screen.getByText('Dark mode').closest('button')!.className).toContain('border-accent')
    expect(screen.getByText('Notifications').closest('button')!.className).toContain('border-accent')
  })

  it('submit button disabled when nothing selected', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const submit = screen.getByText('Submit').closest('button')!
    expect(submit).toBeDisabled()
  })

  it('calls onSubmit with selected option', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Green').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Green' })
  })

  it('calls onSubmit with custom text input', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Purple' } })
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Purple' })
  })

  it('custom input clears option selection', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Yellow' } })
    expect(screen.getByText('Red').closest('button')!.className).not.toContain('bg-accent-subtle')
  })

  it('selecting option clears custom input', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Yellow' } })
    fireEvent.click(screen.getByText('Red').closest('button')!)
    expect((input as HTMLInputElement).value).toBe('')
  })

  it('Enter key submits when answer is ready', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Orange' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Orange' })
  })

  it('multi-select submit joins answers with comma', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={multiQuestion} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'Which features do you want?': 'Dark mode, Notifications' })
  })
})
