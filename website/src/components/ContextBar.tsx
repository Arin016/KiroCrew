/** Builds the context tooltip string. Shared so the bar and its parent pill show identical text. */
export function contextTip(pct: number): string {
  return `Context: ${Math.round(Math.min(pct, 100))}%`
}

/** Compact horizontal context-usage bar for the input bar. */
export default function ContextBar(
  { pct, width = 40, height = 3 }:
    { pct: number; width?: number; height?: number },
) {
  const p = Math.round(Math.min(pct, 100))
  const fill = p >= 90 ? 'var(--danger)' : p >= 75 ? 'var(--warn)' : 'var(--accent)'
  const tip = contextTip(pct)
  const r = height / 2
  return (
    <span title={tip} aria-label={tip} className="inline-flex shrink-0">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="block">
        <rect x="0" y="0" width={width} height={height} rx={r} ry={r} fill="var(--text)" opacity="0.15" />
        <rect x="0" y="0" width={(width * p) / 100} height={height} rx={r} ry={r} fill={fill} style={{ transition: 'width 500ms' }} />
      </svg>
    </span>
  )
}
