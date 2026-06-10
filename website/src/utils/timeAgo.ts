/** Human-readable relative time from a unix timestamp (seconds) */
export function timeAgo(ts: number): string {
  // Guard against a missing/unparseable timestamp: callers that derive ts from
  // an absent or bad date pass 0 / NaN, which would otherwise render as a
  // garbage age (ts=0 → ~20602d). Sub-second positives aren't meaningful either.
  if (!ts || !Number.isFinite(ts) || ts < 1) return '--'
  const s = Math.floor((Date.now() / 1000) - ts)
  if (s < 60) return s < 10 ? 'just now' : `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
