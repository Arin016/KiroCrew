/** Human-readable relative time from a unix timestamp (seconds) */
export function timeAgo(ts: number): string {
  const s = Math.floor((Date.now() / 1000) - ts)
  if (s < 60) return s < 10 ? 'just now' : `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
