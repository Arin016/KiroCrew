/**
 * Quota-safe Web Storage writes.
 *
 * Why this exists: the dashboard accumulates a lot of per-origin localStorage —
 * most notably the per-session virtualizer height caches (`vc_heights_<sid>`,
 * up to 2000 entries each), which pile up across every chat session ever opened
 * and are never reclaimed. Over time the ~5-10 MB origin quota fills up. Once it
 * does, the NEXT `localStorage.setItem` anywhere in the app throws
 * `QuotaExceededError` — and many call sites write raw, unguarded. When one of
 * those fires synchronously on the websocket `onmessage` -> Redux dispatch ->
 * re-render path, the exception bubbles out of an event handler / commit phase
 * (which a React ErrorBoundary cannot catch) and white-screens the whole app.
 *
 * `safeSetItem` makes every write defensive:
 *   1. Try the write.
 *   2. On a quota error, reclaim disposable space (height caches are pure
 *      derived measurement data — safe to drop; they rebuild from the DOM) and
 *      retry once.
 *   3. If it still fails, swallow it (best-effort persistence) and warn in dev.
 *
 * This mirrors the existing guard patterns already used by `chatDrafts`,
 * `pasteTokens`, `commentDrafts`, `HeightCache`, and `dashboardSlice` — this
 * module just centralizes them so the dozens of remaining raw call sites can
 * adopt a single, well-tested helper.
 */

/** Prefix of the per-session virtualizer height caches. These hold pure
 *  derived pixel measurements and are safe to drop under storage pressure;
 *  the virtualizer re-measures from the DOM and repopulates them. */
const HEIGHT_CACHE_PREFIX = 'vc_heights_'

/**
 * Detect a storage-quota exception across browsers.
 *
 * Chrome/Safari throw a DOMException named `QuotaExceededError` (code 22).
 * Firefox throws `NS_ERROR_DOM_QUOTA_REACHED` (code 1014). We check name and
 * code defensively because the name is the most reliable signal but some
 * engines historically only set the legacy numeric code.
 */
export function isQuotaExceededError(err: unknown): boolean {
  if (!(err instanceof DOMException)) return false
  return (
    err.name === 'QuotaExceededError' ||
    err.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
    err.code === 22 ||
    err.code === 1014
  )
}

/**
 * Drop disposable localStorage entries to free space when the quota is hit.
 *
 * Currently reclaims every `vc_heights_*` key. These are the dominant source
 * of unbounded growth (one bounded cache per session, but unbounded in the
 * NUMBER of sessions, with no cleanup for closed/deleted sessions) and are the
 * cheapest data to lose. Returns true if anything was removed, so the caller
 * knows a retry is worthwhile.
 */
function reclaimSpace(): boolean {
  if (typeof localStorage === 'undefined') return false
  let removed = false
  try {
    // Collect first, then delete: removing while iterating by index shifts
    // subsequent indices and would skip keys.
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith(HEIGHT_CACHE_PREFIX)) doomed.push(k)
    }
    for (const k of doomed) {
      try {
        localStorage.removeItem(k)
        removed = true
      } catch {
        /* best-effort */
      }
    }
  } catch {
    /* enumerating storage can throw in locked-down environments */
  }
  return removed
}

function warnDev(key: string, err: unknown): void {
  if (import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.warn(`safeStorage: persist of "${key}" failed`, err)
  }
}

/**
 * Write to localStorage without ever throwing.
 *
 * Returns true if the value was persisted, false if it was dropped (quota
 * exhausted after reclaim, storage disabled, or serialization error upstream).
 * Callers that need to know whether persistence succeeded can branch on the
 * return value; most can ignore it (best-effort persistence).
 */
export function safeSetItem(key: string, value: string): boolean {
  if (typeof localStorage === 'undefined') return false
  try {
    localStorage.setItem(key, value)
    return true
  } catch (err) {
    // Only attempt reclaim+retry for genuine quota errors — a SecurityError
    // (storage disabled) or anything else won't be fixed by freeing space.
    if (isQuotaExceededError(err) && reclaimSpace()) {
      try {
        localStorage.setItem(key, value)
        return true
      } catch (retryErr) {
        warnDev(key, retryErr)
        return false
      }
    }
    warnDev(key, err)
    return false
  }
}

/**
 * Write to sessionStorage without ever throwing. sessionStorage is per-tab and
 * far less prone to filling up, so there is nothing useful to reclaim — we just
 * swallow failures so a full/disabled store can't crash the app.
 */
export function safeSetSessionItem(key: string, value: string): boolean {
  if (typeof sessionStorage === 'undefined') return false
  try {
    sessionStorage.setItem(key, value)
    return true
  } catch (err) {
    warnDev(key, err)
    return false
  }
}
