/**
 * FolderPickerSubmenu is the shared folder-list flyout consumed by the sidebar's
 * "New chat in folder" create-menu and the session header's "Move to folder" menu.
 * These assertions lock the contract both call sites depend on:
 *   - every folder renders as a pickable menuitem,
 *   - includeRoot prepends a "No folder (root)" entry that picks null,
 *   - currentFolderId drives exactly one checkmark (folder or root),
 *   - onPick receives the folder id (string) or null for root.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import FolderPickerSubmenu from '../components/FolderPickerSubmenu'
import type { ChatFolder } from '../types'

const folders: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

describe('FolderPickerSubmenu', () => {
  it('renders one menuitem per folder and picks the folder id', () => {
    const onPick = vi.fn()
    const { getByTitle } = render(<FolderPickerSubmenu folders={folders} onPick={onPick} />)
    fireEvent.click(getByTitle('Beta'))
    expect(onPick).toHaveBeenCalledWith('f2')
  })

  it('omits the root entry by default (New-chat-in-folder semantics)', () => {
    const { queryByTitle } = render(<FolderPickerSubmenu folders={folders} onPick={vi.fn()} />)
    expect(queryByTitle('No folder (root)')).toBeNull()
  })

  it('prepends a root entry that picks null when includeRoot is set', () => {
    const onPick = vi.fn()
    const { getByTitle } = render(<FolderPickerSubmenu folders={folders} onPick={onPick} includeRoot />)
    fireEvent.click(getByTitle('No folder (root)'))
    expect(onPick).toHaveBeenCalledWith(null)
  })

  it('checkmarks the current folder and not others', () => {
    const { getByTitle } = render(<FolderPickerSubmenu folders={folders} onPick={vi.fn()} currentFolderId="f1" />)
    // lucide renders an <svg> with class lucide-check inside the matching button only
    expect(getByTitle('Alpha').querySelector('.lucide-check')).not.toBeNull()
    expect(getByTitle('Beta').querySelector('.lucide-check')).toBeNull()
  })

  it('checkmarks the root entry when the session is at root', () => {
    const { getByTitle } = render(
      <FolderPickerSubmenu folders={folders} onPick={vi.fn()} includeRoot currentFolderId={undefined} />,
    )
    expect(getByTitle('No folder (root)').querySelector('.lucide-check')).not.toBeNull()
    expect(getByTitle('Alpha').querySelector('.lucide-check')).toBeNull()
  })
})
