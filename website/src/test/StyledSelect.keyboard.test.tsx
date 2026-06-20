import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import StyledSelect from '../components/StyledSelect'

// >5 options exercises the filter-input path; <=5 exercises the no-filter path.
const MANY_OPTIONS = [
  'emerald', 'monokai', 'solarized', 'amber', 'dracula', 'nord', 'rosepine',
]
const FEW_OPTIONS = ['emerald', 'monokai', 'solarized']

describe('StyledSelect — keyboard navigation', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
  })

  /** Force the pointer kind so isTouchDevice() is deterministic. */
  function mockPointerKind(kind: 'touch' | 'mouse') {
    const matches = (q: string) =>
      kind === 'touch'
        ? /pointer:\s*coarse|hover:\s*none/.test(q)
        : /pointer:\s*fine|hover:\s*hover/.test(q)
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: matches(query), media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
  }

  const flush = () => act(async () => { await new Promise(r => setTimeout(r, 5)) })
  const opt = (name: string | RegExp) =>
    screen.getByRole('option', { name: typeof name === 'string' ? new RegExp(name) : name })

  function open(value: string, options: string[], withPlaceholder = true) {
    const onChange = vi.fn()
    render(
      <StyledSelect
        value={value}
        options={options}
        onChange={onChange}
        placeholder={withPlaceholder ? '— none —' : undefined}
      />,
    )
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    return { onChange }
  }

  // ── No-filter path (<=5 options) ──────────────────────────────────────────

  it('moves focus into the list on open and lands on the selected option', async () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    await flush()
    expect(document.activeElement).toBe(screen.getByRole('option', { selected: true }))
    expect(opt('monokai')).toHaveAttribute('aria-selected', 'true')
  })

  it('does NOT steal focus into the list on touch devices', async () => {
    mockPointerKind('touch')
    open('monokai', FEW_OPTIONS)
    await flush()
    expect(document.activeElement).not.toBe(opt('monokai'))
  })

  it('ArrowDown / ArrowUp move roving focus between options', () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    const emerald = opt('emerald')
    emerald.focus()
    fireEvent.keyDown(emerald, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opt('monokai'))
    fireEvent.keyDown(opt('monokai'), { key: 'ArrowUp' })
    expect(document.activeElement).toBe(opt('emerald'))
  })

  it('Home / End jump to the first / last option', () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    const monokai = opt('monokai')
    monokai.focus()
    fireEvent.keyDown(monokai, { key: 'Home' })
    // First data-option is the placeholder ("— none —")
    expect(document.activeElement).toBe(opt('— none —'))
    fireEvent.keyDown(document.activeElement!, { key: 'End' })
    expect(document.activeElement).toBe(opt('solarized'))
  })

  it('options are out of the natural tab order (roving tabindex)', () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    expect(opt('emerald')).toHaveAttribute('tabindex', '-1')
  })

  it('Escape closes the listbox and returns focus to the trigger', async () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    await flush()
    const trigger = screen.getByRole('button', { expanded: true })
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })

  it('Tab closes the listbox and returns focus to the trigger (no focus leak to the portal)', async () => {
    mockPointerKind('mouse')
    open('monokai', FEW_OPTIONS)
    await flush()
    const trigger = screen.getByRole('button', { expanded: true })
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })

  it('selecting an option fires onChange, closes, and returns focus to the trigger', () => {
    mockPointerKind('mouse')
    const { onChange } = open('monokai', FEW_OPTIONS)
    const trigger = screen.getByRole('button', { expanded: true })
    fireEvent.click(opt('solarized'))
    expect(onChange).toHaveBeenCalledWith('solarized')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(trigger)
  })

  // ── Filter-input path (>5 options) ─────────────────────────────────────────

  it('ArrowDown from the filter input drops focus into the first option', async () => {
    mockPointerKind('mouse')
    open('emerald', MANY_OPTIONS, false)
    await flush()
    const input = screen.getByPlaceholderText('Filter…')
    expect(document.activeElement).toBe(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opt('emerald'))
  })

  it('Enter in the filter input selects the sole remaining match', async () => {
    mockPointerKind('mouse')
    const { onChange } = open('emerald', MANY_OPTIONS, false)
    await flush()
    const input = screen.getByPlaceholderText('Filter…') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'dracula' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('dracula')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})
