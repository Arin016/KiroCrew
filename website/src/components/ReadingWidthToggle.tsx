import type { ReadingWidth } from '../hooks/useReadingWidth'

const LABELS: Record<ReadingWidth, string> = { md: 'M', full: 'F' }
const TITLES: Record<ReadingWidth, string> = { md: 'Medium width', full: 'Full width' }

export default function ReadingWidthToggle({ value, onToggle }: { value: ReadingWidth; onToggle: () => void }) {
  return (
    <button type="button"
      className={`w-[26px] h-[26px] flex items-center justify-center rounded-md text-[11px] font-medium cursor-pointer border transition-all ${value === 'full' ? 'border-accent bg-accent-subtle text-accent' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
      onClick={onToggle}
      title={TITLES[value]}
      aria-label={TITLES[value]}
      aria-pressed={value === 'full'}
    >{LABELS[value]}</button>
  )
}
