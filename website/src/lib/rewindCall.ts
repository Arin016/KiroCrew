/**
 * Rewind helper — wraps `api.rewind` with a rollback callback that fires on
 * failure. Extracted from ChatPage so the success/failure branches can be
 * unit-tested without mounting the full chat page.
 */

import { api } from '../api/client'

/**
 * Call `/api/chat/slots/{slot}/rewind` and invoke `rollback` if the request
 * rejects. Logs a warning on failure (debug only).
 */
export async function rewindWithRollback(
  slot: string,
  ts: string,
  content: string,
  rollback: () => void,
): Promise<void> {
  try {
    await api.rewind(slot, ts, content)
  } catch (e) {
    console.warn('rewind failed', e)
    rollback()
  }
}
