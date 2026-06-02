const MAX_PROMPT_LENGTH = 4000

/** Extract a prompt from a presigned token's payload (channel challenge-and-redirect). */
export function extractPromptFromToken(token: string): string | null {
  try {
    const parts = token.split('.')
    if (parts.length < 2) return null
    const payload = JSON.parse(atob(parts[0].replace(/-/g, '+').replace(/_/g, '/')))
    if (typeof payload.prompt !== 'string' || payload.prompt.length === 0) return null
    if (payload.prompt.length > MAX_PROMPT_LENGTH) return null
    return payload.prompt
  } catch {
    return null
  }
}
