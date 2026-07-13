// Pure helpers for the sidebar "recency tint" — the graded accent stripe marking the
// most-recently-active sessions. Kept out of the component so they're unit-testable and
// so the tinted-session count stays a single configurable value.

// Number of most-recently-active sessions to tint in the sidebar. Constant for now;
// a follow-up will wire this to a user setting (session-colors config).
export const RECENT_TINT_COUNT = 5

/**
 * Rank the up-to-`count` most-recently-active sessions by `last_ts` (descending),
 * returning a key→rank map where 1 = most recent. `last_ts` is an ISO-8601 UTC string
 * (the last message of any role); sessions with a missing/unparseable `last_ts` are
 * excluded so an empty session never occupies a tint slot.
 */
export function computeRecentRank(
  slots: { key: string; last_ts?: string }[],
  count: number,
): Map<string, number> {
  const ranked = new Map<string, number>()
  slots
    .map(s => [s.key, Date.parse(s.last_ts || '') || 0] as [string, number])
    .filter(([, t]) => t > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, count)
    .forEach(([key], i) => ranked.set(key, i + 1))
  return ranked
}

/**
 * Graded recency-tint stripe for a session row, returned as an inset `box-shadow` value.
 * The least-recent tinted rank sits at the floor (MIN_W width / MIN_OP accent) and each
 * step up in recency adds a fixed increment (W_STEP px / OP_STEP %) up to a hard cap
 * (MAX_W / MAX_OP). Fixed steps keep each rank's look constant across counts — if the
 * configured count exceeds 5, the most-recent tints plateau at the cap rather than growing
 * unbounded. `color-mix` keeps it theme-aware.
 */
export function recencyTintShadow(rank: number, total: number): string {
  const MIN_W = 3, MAX_W = 7, W_STEP = 1
  const MIN_OP = 40, MAX_OP = 100, OP_STEP = 15
  const steps = total - rank // 0 for the least-recent tinted rank; grows with recency
  const width = Math.min(MAX_W, MIN_W + W_STEP * steps)
  const op = Math.min(MAX_OP, MIN_OP + OP_STEP * steps)
  return `inset ${width}px 0 0 color-mix(in srgb, var(--accent) ${op}%, transparent)`
}
