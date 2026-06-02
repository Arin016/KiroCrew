/** Compact circular context-usage indicator for the input bar. */
export default function ContextRing(
  { pct, usedTokens, windowTokens }: { pct: number; usedTokens?: number; windowTokens?: number },
) {
  const p = Math.round(Math.min(pct, 100))
  const r = 8, stroke = 2.5, circ = 2 * Math.PI * r
  const offset = p > 0 ? circ - (circ * p) / 100 : circ
  const fill = p >= 90 ? 'var(--danger)' : p >= 75 ? 'var(--warn)' : 'currentColor'
  // Show absolute tokens when known so "44%" is unambiguous (e.g. 44% of a
  // 200k window, not 1M). Compact k-formatting keeps the native tooltip short.
  const fmtK = (n: number) => n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`
  const tip = windowTokens && windowTokens > 0
    ? `Context: ${p}% (${fmtK(usedTokens ?? 0)} / ${fmtK(windowTokens)} tokens)`
    : `Context: ${p}%`
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" className="shrink-0" style={{ marginRight: -2 }}>
      <title>{tip}</title>
      <circle cx="11" cy="11" r={r} fill="none" stroke="currentColor" strokeWidth={stroke} opacity="0.2" />
      <circle cx="11" cy="11" r={r} fill="none" stroke={fill} strokeWidth={stroke}
        strokeDasharray={circ} strokeDashoffset={offset}
        strokeLinecap="round" transform="rotate(-90 11 11)" style={{ transition: 'stroke-dashoffset 500ms' }} />
      {p > 0 && <text x="11" y="11.5" textAnchor="middle" dominantBaseline="central" fill={fill} fontSize="8" fontFamily="monospace" fontWeight="700">{p}</text>}
    </svg>
  )
}
