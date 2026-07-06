import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FileChangeChips, { type FileChangeEntry } from '../components/FileChangeChips'

const change = (path: string, before: string, after: string) => ({ path, before, after })

describe('FileChangeChips', () => {
  it('renders nothing when fileChanges is empty', () => {
    const { container } = render(<FileChangeChips fileChanges={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when fileChanges is undefined', () => {
    // Component guards against undefined too — keeps consumers from needing
    // their own falsy guard.
    const { container } = render(<FileChangeChips fileChanges={undefined as unknown as FileChangeEntry[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one chip per file change', () => {
    render(
      <FileChangeChips
        fileChanges={[
          change('/a.ts', 'a', 'a\nb'),
          change('/b.py', 'x', 'y'),
        ]}
      />
    )
    expect(screen.getByText('a.ts')).toBeInTheDocument()
    expect(screen.getByText('b.py')).toBeInTheDocument()
  })

  it('shows basename, not full path', () => {
    render(<FileChangeChips fileChanges={[change('/abs/path/to/deep/file.ts', '', 'x')]} />)
    expect(screen.getByText('file.ts')).toBeInTheDocument()
    expect(screen.queryByText('/abs/path/to/deep/file.ts')).not.toBeInTheDocument()
  })

  it('LCS counts pure additions correctly', () => {
    // before: 1 line, after: 3 lines (2 additions, 0 removals)
    render(<FileChangeChips fileChanges={[change('/grow.ts', 'a', 'a\nb\nc')]} />)
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.queryByText(/^-\d+$/)).not.toBeInTheDocument()
  })

  it('LCS counts pure removals correctly', () => {
    render(<FileChangeChips fileChanges={[change('/shrink.ts', 'a\nb\nc', 'a')]} />)
    expect(screen.getByText('-2')).toBeInTheDocument()
    expect(screen.queryByText(/^\+\d+$/)).not.toBeInTheDocument()
  })

  it('LCS detects pure moves as +N/-N (not 0/0)', () => {
    // Reordered lines: same multiset, but LCS sees them as add+remove.
    render(<FileChangeChips fileChanges={[change('/move.rs', 'a\nb\nc', 'c\nb\na')]} />)
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.getByText('-2')).toBeInTheDocument()
  })

  it('renders nothing for stats when before === after', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/same.ts', 'x', 'x')]} />)
    // The chip itself still renders, but no +N/-N text spans.
    expect(container.querySelector('button')).toBeTruthy()
    expect(container.textContent).not.toMatch(/[+-]\d/)
  })

  it('mixed add/remove on a real-ish edit', () => {
    const before = 'line1\nline2\nline3'
    const after = 'line1\nline2_modified\nline3\nline4'
    render(<FileChangeChips fileChanges={[change('/mix.ts', before, after)]} />)
    // line2 → line2_modified counts as +1/-1; line4 added → another +1.
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.getByText('-1')).toBeInTheDocument()
  })

  it('clicking expanded chip calls onOpenDiff with (path, after, before)', () => {
    const onOpenDiff = vi.fn()
    render(
      <FileChangeChips
        fileChanges={[change('/click.ts', 'before-content', 'after-content')]}
        onOpenDiff={onOpenDiff}
      />
    )
    fireEvent.click(screen.getByText('click.ts').closest('button')!)
    expect(onOpenDiff).toHaveBeenCalledWith('/click.ts', 'after-content', 'before-content')
  })

  it('clicking a chip does not throw when onOpenDiff is missing', () => {
    render(<FileChangeChips fileChanges={[change('/click.ts', 'a', 'b')]} />)
    // No throw means the optional-chained handler is safe.
    expect(() => fireEvent.click(screen.getByText('click.ts').closest('button')!)).not.toThrow()
  })

  it('minimal style hides filename in main chip but exposes hover label', () => {
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/minimal.ts', 'a', 'a\nb')]}
        style="minimal"
      />
    )
    // The chip button itself does NOT have the basename inside it.
    const button = container.querySelector('button')
    expect(button?.textContent).not.toContain('minimal.ts')
    // Hover label exists somewhere in the markup with the basename.
    expect(container.textContent).toContain('minimal.ts')
    // Stats still rendered inside the button.
    expect(button?.textContent).toContain('+1')
  })

  it('minimal style click also triggers onOpenDiff', () => {
    const onOpenDiff = vi.fn()
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/min-click.ts', 'a', 'b')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
      />
    )
    fireEvent.click(container.querySelector('button')!)
    expect(onOpenDiff).toHaveBeenCalledWith('/min-click.ts', 'b', 'a')
  })

  it('falls back to expanded for an unknown style value', () => {
    // Defensive default in the renderer map covers stale localStorage values
    // (e.g. legacy "tooltip"/"compact"/"full") until the migration runs.
    render(
      <FileChangeChips
        fileChanges={[change('/legacy.ts', 'a', 'a\nb')]}
        // @ts-expect-error — intentional invalid style for the fallback path
        style="tooltip"
      />
    )
    expect(screen.getByText('legacy.ts')).toBeInTheDocument()
  })

  it('handles empty after content (file fully cleared)', () => {
    render(<FileChangeChips fileChanges={[change('/cleared.ts', 'a\nb', '')]} />)
    // Empty-after is treated as 0 lines (not [''] phantom). Expect -2/+0.
    expect(screen.getByText('-2')).toBeInTheDocument()
    expect(screen.queryByText(/^\+\d+$/)).not.toBeInTheDocument()
  })

  it('handles empty before content as 0 lines (new file shows only +N)', () => {
    // Pre-fix: new file showed +1/-1 because ''.split('\n') == ['']. Fix
    // treats empty before as 0 lines so a 1-line new file shows just +1.
    render(<FileChangeChips fileChanges={[change('/brand-new.ts', '', 'hello')]} />)
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(screen.queryByText(/^-\d+$/)).not.toBeInTheDocument()
  })

  it('keys chips by path (no duplicate-key warnings)', () => {
    const consoleErr = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <FileChangeChips
        fileChanges={[
          change('/a.ts', 'x', 'y'),
          change('/b.ts', 'x', 'y'),
          change('/c.ts', 'x', 'y'),
        ]}
      />
    )
    expect(consoleErr).not.toHaveBeenCalledWith(
      expect.stringContaining('unique "key"'),
    )
    consoleErr.mockRestore()
  })

  it('uses the multiset fallback for huge files (>1M LCS cells)', () => {
    // m * n > 1_000_000 triggers the cheap multiset branch. Build two
    // files with ~1100 lines each so the cell budget is exceeded but the
    // test still runs in <100ms.
    const N = 1100
    const before = Array.from({ length: N }, (_, i) => `before-line-${i}`).join('\n')
    // Treatment: drop 5 unique lines, add 7 unique ones; the rest match.
    const afterLines = Array.from({ length: N }, (_, i) => `before-line-${i}`)
    afterLines.splice(0, 5)
    afterLines.push('extra-1', 'extra-2', 'extra-3', 'extra-4', 'extra-5', 'extra-6', 'extra-7')
    const after = afterLines.join('\n')
    render(<FileChangeChips fileChanges={[change('/huge.log', before, after)]} />)
    // Multiset branch: under-counts pure moves, but unique additions and
    // unique removals are exact. Expect +7/-5.
    expect(screen.getByText('+7')).toBeInTheDocument()
    expect(screen.getByText('-5')).toBeInTheDocument()
  })
})
