import type { ChatFolder } from '../types'

/**
 * Resolve which agent to use when creating a session in a folder.
 * Priority: folder.default_agent → globalDefaultAgent → undefined
 */
export function resolveFolderAgent(
  folders: ChatFolder[],
  folderId: string,
  globalDefaultAgent: string
): string | undefined {
  const folder = folders.find(f => f.id === folderId)
  return folder?.default_agent || globalDefaultAgent || undefined
}
