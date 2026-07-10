import type { ChatFolder } from '../types'

/** Breadcrumb separator — matches the server-side folder_breadcrumb (U+203A). */
export const FOLDER_PATH_SEP = ' › '

export interface OrderedFolder {
  readonly folder: ChatFolder
  /** Ancestor names root→parent (excludes the folder itself). Empty for root folders. */
  readonly ancestors: readonly string[]
  /** Depth in the tree (0 for root folders). Equals ancestors.length. */
  readonly depth: number
  /** Full ancestry path root→leaf, e.g. "Parent › Child". Equals the name for root folders. */
  readonly path: string
}

/**
 * Flatten folders into pre-order (tree) sequence so children sit directly under
 * their parent, siblings sorted by `order` then name. Each entry carries its
 * ancestor names (for breadcrumb rendering) and depth (for indentation).
 * Orphans (parent_id pointing at a missing folder) are treated as roots.
 * Cycle/depth guarded.
 *
 * Shared by the folder pickers (move-to-folder submenu, new-chat-in-folder)
 * so the indented tree ordering stays identical everywhere.
 */
export function orderFoldersWithPaths(folders: readonly ChatFolder[]): OrderedFolder[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const childrenOf = (pid: string) =>
    folders
      .filter(f => {
        const parent = f.parent_id && byId.has(f.parent_id) ? f.parent_id : ''
        return parent === pid
      })
      .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name))

  const out: OrderedFolder[] = []
  const walk = (folder: ChatFolder, ancestors: string[], visited: Set<string>) => {
    if (visited.has(folder.id) || ancestors.length > 20) return
    visited.add(folder.id)
    out.push({
      folder,
      ancestors: [...ancestors],
      depth: ancestors.length,
      path: [...ancestors, folder.name].join(FOLDER_PATH_SEP),
    })
    for (const child of childrenOf(folder.id)) walk(child, [...ancestors, folder.name], visited)
  }
  const visited = new Set<string>()
  for (const root of childrenOf('')) walk(root, [], visited)
  // Safety net: surface any folder the walk missed (e.g. a cycle root) so no
  // destination silently disappears from the picker.
  for (const f of folders) if (!visited.has(f.id)) out.push({ folder: f, ancestors: [], depth: 0, path: f.name })
  return out
}
