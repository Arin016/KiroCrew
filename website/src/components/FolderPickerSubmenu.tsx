import { Folder, Check } from 'lucide-react'
import type { ChatFolder } from '../types'

interface FolderPickerSubmenuProps {
  readonly folders: readonly ChatFolder[]
  /** Invoked with the chosen folder id, or null for the root entry. */
  readonly onPick: (folderId: string | null) => void
  /** When set, the matching entry shows a checkmark. null/'' marks the root entry. */
  readonly currentFolderId?: string | null
  /** Prepend a "No folder (root)" entry (Move-to-folder uses this; New-chat-in-folder does not). */
  readonly includeRoot?: boolean
  /** Label for the root entry. */
  readonly rootLabel?: string
  /** Hover passthroughs so a parent driving a hover-flyout can keep it open while the pointer is inside. */
  readonly onMouseEnter?: () => void
  readonly onMouseLeave?: () => void
}

/**
 * Flyout panel listing chat folders for a pick action. Shared by the sidebar's
 * "New chat in folder" create-menu and the session header's "Move to folder"
 * menu so the two stay visually and behaviorally identical. Presentational only:
 * the parent owns open/close state and supplies the pick handler.
 */
export default function FolderPickerSubmenu({
  folders,
  onPick,
  currentFolderId,
  includeRoot = false,
  rootLabel = 'No folder (root)',
  onMouseEnter,
  onMouseLeave,
}: FolderPickerSubmenuProps) {
  const atRoot = currentFolderId == null || currentFolderId === ''
  const itemClass = 'w-full px-3 py-1.5 text-left text-[12.5px] text-text flex items-center gap-2 hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors'
  return (
    <div role="menu" tabIndex={-1} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}
      className="absolute left-full top-0 ml-1 min-w-[170px] max-w-[220px] max-h-[280px] overflow-y-auto rounded-lg border border-border bg-bg-elevated shadow-lg py-1">
      {includeRoot && (
        <button role="menuitem" title={rootLabel} className={itemClass} onClick={() => onPick(null)}>
          <Folder size={13} className="text-muted shrink-0" /> <span className="truncate">{rootLabel}</span>
          {atRoot && <Check size={13} className="ml-auto text-accent shrink-0" />}
        </button>
      )}
      {folders.map(f => (
        <button key={f.id} role="menuitem" title={f.name} className={itemClass} onClick={() => onPick(f.id)}>
          <Folder size={13} className="text-accent shrink-0" /> <span className="truncate">{f.name}</span>
          {currentFolderId === f.id && <Check size={13} className="ml-auto text-accent shrink-0" />}
        </button>
      ))}
    </div>
  )
}
