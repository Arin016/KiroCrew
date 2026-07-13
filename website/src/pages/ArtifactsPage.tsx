import { useState, useMemo, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bookmark, ExternalLink, X, Loader2, Folder as FolderIcon, FolderPlus, FolderOpen, ChevronRight, ChevronDown, MoreVertical, Pencil, Trash2 } from 'lucide-react'
import { DndContext, PointerSensor, useSensor, useSensors, DragOverlay, MeasuringStrategy, pointerWithin, type DragEndEvent, type DragStartEvent, type CollisionDetection, type Modifier } from '@dnd-kit/core'
import { openPopout } from '../utils/artifactPopout'
import { api } from '../api/client'
import { Card, CardTitle, PageHeader, StatCard, Btn, Badge, SearchInput, EmptyState, Input } from '../components/ui'
import { useImeGuard } from '../hooks/useImeGuard'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../components/ui/dropdown-menu'
import InfoTip from '../components/InfoTip'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import ArtifactFolderDeleteDialog from '../components/ArtifactFolderDeleteDialog'
import { DndDraggable, DndDroppable } from '../components/dnd'
import { useArtifactFolders, useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { childFolders, isDescendantFolder, folderSubtreeStats } from '../utils/artifactFolderTree'
import { safeSetItem } from '../utils/safeStorage'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import type { Artifact, ArtifactFolder } from '../types'

const sel =
  'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring'

const KIND_OPTIONS = ['', 'widget', 'html', 'markdown', 'svg', 'json', 'text'] as const

const KIND_BADGE: Record<Artifact['kind'], 'ok' | 'err' | 'warn' | 'aim'> = {
  widget: 'aim',
  html: 'ok',
  markdown: 'ok',
  svg: 'warn',
  json: 'ok',
  text: 'ok',
}

function isoToTs(iso: string): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0
}

const artifactLibraryCollision: CollisionDetection = (args) => pointerWithin(args)

// Center the DragOverlay ghost on the cursor. Without this the overlay spawns
// at the dragged element's top-left — grabbing a tall row near its bottom
// leaves the ghost pixels above the pointer. (Inline port of
// @dnd-kit/modifiers' snapCenterToCursor; the package isn't a dependency.)
const snapOverlayToCursor: Modifier = ({ activatorEvent, draggingNodeRect, transform }) => {
  if (draggingNodeRect && activatorEvent && 'clientX' in activatorEvent && 'clientY' in activatorEvent) {
    const evt = activatorEvent as PointerEvent
    const offsetX = evt.clientX - draggingNodeRect.left
    const offsetY = evt.clientY - draggingNodeRect.top
    return {
      ...transform,
      x: transform.x + offsetX - draggingNodeRect.width / 2,
      y: transform.y + offsetY - draggingNodeRect.height / 2,
    }
  }
  return transform
}

/** Payload carried by draggable rows; routes the drop in handleDragEnd. */
type LibraryDrag =
  | { type: 'artifact'; slug: string; name: string; folderId: string }
  | { type: 'folder'; id: string; name: string }

type FolderActions = {
  onOpen: (folderId: string) => void
  onRename: (f: ArtifactFolder) => void
  onMove: (f: ArtifactFolder, newParentId: string) => void
  onDelete: (f: ArtifactFolder) => void
  onSetColor: (f: ArtifactFolder, color: string) => void
  /** Folder currently in inline-rename mode (its row swaps the name for an input). */
  renamingId: string | null
  onRenameSubmit: (f: ArtifactFolder, name: string) => void
  onRenameCancel: () => void
}

/** Curated folder color palette (works on light + dark themes). '' = none. */
const FOLDER_COLORS = ['#ef4444', '#f59e0b', '#22c55e', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899', '#94a3b8'] as const

/** Swatch strip for picking a folder color ('' clears back to default). */
function FolderColorSwatches({ value, onPick, size = 16 }: { value?: string; onPick: (color: string) => void; size?: number }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap" role="radiogroup" aria-label="Folder color">
      {FOLDER_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={value === c}
          aria-label={`Color ${c}`}
          onClick={(e) => { e.stopPropagation(); onPick(c) }}
          onPointerDown={(e) => e.stopPropagation()}
          className={`rounded-full border cursor-pointer transition-transform hover:scale-110 ${
            value === c ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
          }`}
          style={{ width: size, height: size, background: c }}
        />
      ))}
      <button
        type="button"
        role="radio"
        aria-checked={!value}
        aria-label="No color"
        title="No color"
        onClick={(e) => { e.stopPropagation(); onPick('') }}
        onPointerDown={(e) => e.stopPropagation()}
        className={`rounded-full border cursor-pointer transition-transform hover:scale-110 flex items-center justify-center text-muted bg-transparent ${
          !value ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
        }`}
        style={{ width: size, height: size }}
      >
        <X size={Math.max(8, size - 7)} />
      </button>
    </div>
  )
}

/** Folder glyph — same composition as the chat sidebar's FolderGlyph: the
 * Lucide Folder icon is always the icon (design-token colorable, CSS-sized,
 * fixed footprint), with the auto-derived emoji overlaid as a small badge on
 * the closed folder's flat face. Expanded folders show the open glyph alone
 * (its angled flap has no flat face for the badge). */
function FolderGlyph({ folder, size = 16, open = false }: { folder: ArtifactFolder; size?: number; open?: boolean }) {
  const Glyph = open ? FolderOpen : FolderIcon
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <Glyph size={size} className="shrink-0" style={{ color: folder.color || 'var(--accent)' }} />
      {folder.icon && !open && (
        <span
          aria-hidden
          className="absolute inset-x-0 bottom-0 flex items-center justify-center leading-none pointer-events-none"
          style={{ top: Math.round(size * 0.42), fontSize: Math.max(7, Math.round(size * 0.52)) }}
        >
          {folder.icon}
        </span>
      )}
    </span>
  )
}

/** Inline folder-name editor (create + rename) — the same native pattern the
 * chat sidebar uses for slot/folder renames: autofocused input, Enter commits,
 * Escape cancels, blur commits a non-empty value. IME-guarded. */
function FolderNameInput({ initial = '', placeholder = 'Folder name', onCommit, onCancel }: {
  initial?: string
  placeholder?: string
  onCommit: (name: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const cancelledRef = useRef(false)
  const ime = useImeGuard()
  return (
    <Input
      autoFocus
      value={value}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => setValue(e.target.value)}
      onFocus={(e) => e.target.select()}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      className="w-full bg-transparent border border-accent rounded px-1.5 py-0.5 text-text-strong outline-none text-sm select-text"
      {...ime.bindEnter<HTMLInputElement>({
        onEnter: () => { (document.activeElement as HTMLInputElement)?.blur() },
        onEscape: () => { cancelledRef.current = true; onCancel() },
        onBlur: () => {
          if (cancelledRef.current) { cancelledRef.current = false; return }
          const name = value.trim()
          if (name) onCommit(name)
          else onCancel()
        },
      })}
    />
  )
}

/** Shared "…" menu for a folder row. The move submenu excludes the folder's
 * own subtree — a folder can't become its own descendant. */
function FolderMenu({ folder, folders, actions }: { folder: ArtifactFolder; folders: ArtifactFolder[]; actions: FolderActions }) {
  const moveTargets = folders.filter(f => !isDescendantFolder(folders, folder.id, f.id))
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          onClick={(e) => e.stopPropagation()}
          className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
          title="Folder actions"
          aria-label={`Actions for folder ${folder.name}`}
        >
          <MoreVertical size={13} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuItem onSelect={() => actions.onRename(folder)}>
          <Pencil size={13} className="text-muted shrink-0" /> Rename
        </DropdownMenuItem>
        <FolderMoveSubmenu
          variant="dropdown"
          folders={moveTargets}
          currentFolderId={folder.parent_id || null}
          onPick={(pid) => actions.onMove(folder, pid || '')}
        />
        <DropdownMenuSeparator />
        {/* Color swatches live inline (not a menu item) so picking one doesn't
            navigate — the menu closes after the pick via the row's own click. */}
        <div className="px-2 py-1.5">
          <div className="text-[11px] text-muted mb-1.5">Color</div>
          <FolderColorSwatches value={folder.color} onPick={(c) => actions.onSetColor(folder, c)} />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="text-danger" onSelect={() => actions.onDelete(folder)}>
          <Trash2 size={13} className="shrink-0" /> Delete…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function LibraryTableHead() {
  const th = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
  return (
    <thead>
      <tr>
        <th className={`${th} min-w-[160px]`}>Name</th>
        <th className={`${th} w-[180px]`}>Slug</th>
        <th className={`${th} w-[100px]`}>Kind</th>
        <th className={`${th} w-[60px]`}>Ver</th>
        <th className={`${th} min-w-[160px]`}>Tags</th>
        <th className={`${th} w-[110px]`}>Updated</th>
        <th className={`${th} w-[120px]`}>Actions</th>
      </tr>
    </thead>
  )
}

/** One artifact row, shared by the flat table and the folder tree. Draggable
 * onto folder rows / the Unfiled lane (indent nests it under its folder). */
function ArtifactRow({ a, onOpen, onDelete, deletingSlug, indent = 0, dropFolderId, dropHighlight = false }: {
  a: Artifact
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  indent?: number
  /** When set, the row also accepts drops, filing the dragged item into this
   * folder (''=unfile) — so dropping anywhere over an expanded folder's
   * region (or the Unfiled section) works, not just on the header row. */
  dropFolderId?: string
  /** True while the active drag hovers anywhere over this row's folder region. */
  dropHighlight?: boolean
}) {
  const inner = (setDropRef?: (el: HTMLElement | null) => void) => (
    <DndDraggable id={`artifact-row:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
        <tr
          ref={(el) => { setNodeRef(el); setDropRef?.(el) }}
          {...listeners}
          style={{ opacity: isDragging ? 0.4 : 1 }}
          className={`transition-colors cursor-pointer ${dropHighlight ? 'bg-accent/10' : 'hover:bg-bg-hover'}`}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) {
              openPopout(a.slug, a.name)
            } else {
              onOpen(a.slug)
            }
          }}
        >
          <td className="px-2.5 py-2 border-b border-border" style={indent > 0 ? { paddingLeft: `${10 + indent * 20}px` } : undefined}>
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-strong font-medium">{a.name}</span>
            </div>
            {a.description && <div className="text-[12px] text-muted truncate max-w-[400px]">{a.description}</div>}
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <code className="text-[12px] text-muted">{a.slug}</code>
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <Badge variant={KIND_BADGE[a.kind]}>{a.kind}</Badge>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-sm text-muted">v{a.version}</td>
          <td className="px-2.5 py-2 border-b border-border">
            <div className="flex flex-wrap gap-1">
              {(a.tags || []).map((t) => (
                <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
              ))}
            </div>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">{_timeAgo(isoToTs(a.updated_at))}</td>
          <td className="px-2.5 py-2 border-b border-border">
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); openPopout(a.slug, a.name) }}
                className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                title="Pop out into its own window"
                aria-label="Pop out to window"
              >
                <ExternalLink size={13} />
              </button>
              <button
                type="button"
                disabled={deletingSlug === a.slug}
                onClick={(e) => { e.stopPropagation(); onDelete(a) }}
                className="p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer bg-transparent border-none disabled:opacity-60 disabled:cursor-default"
                title="Remove from artifacts library (does not delete the source file or widget)"
                aria-label="Remove from artifacts library"
              >
                {deletingSlug === a.slug ? <Loader2 size={13} className="animate-spin" /> : <X size={13} />}
              </button>
            </div>
          </td>
        </tr>
      )}
    </DndDraggable>
  )
  if (dropFolderId === undefined) return inner()
  return (
    <DndDroppable id={`row-drop:${a.slug}`} data={{ type: 'folder-drop', folderId: dropFolderId }}>
      {({ setNodeRef }) => inner(setNodeRef)}
    </DndDroppable>
  )
}

/** The original compact flat table of the local artifact library (rendered
 * while any filter is active, when folder scoping is bypassed). */
function LibraryTable({
  items,
  onOpen,
  onDelete,
  deletingSlug,
}: {
  items: Artifact[]
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse table-striped">
        <LibraryTableHead />
        <tbody>
          {items.map((a) => (
            <ArtifactRow key={a.slug} a={a} onOpen={onOpen} onDelete={onDelete} deletingSlug={deletingSlug} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Folder header row in the tree table: collapsible (chevron / row click),
 * draggable (reorder among siblings, nest elsewhere), droppable. */
function FolderRow({ folder, folders, depth, expanded, onToggle, actions, dropHighlight = false }: {
  folder: ArtifactFolder
  folders: ArtifactFolder[]
  depth: number
  expanded: boolean
  onToggle: (id: string) => void
  actions: FolderActions
  /** True while the active drag hovers anywhere over this folder's region
   * (header row or any row inside it) — lights the whole folder up. */
  dropHighlight?: boolean
}) {
  const stats = folderSubtreeStats(folders, folder.id)
  const Chevron = expanded ? ChevronDown : ChevronRight
  const renaming = actions.renamingId === folder.id
  return (
    <DndDroppable id={`folder-row-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
      {({ setNodeRef: setDropRef, isOver }) => (
        <DndDraggable id={`folder-row:${folder.id}`} data={{ type: 'folder', id: folder.id, name: folder.name } satisfies LibraryDrag}>
          {({ setNodeRef: setDragRef, listeners, isDragging }) => (
            <tr
              ref={(el) => { setDropRef(el); setDragRef(el) }}
              {...(renaming ? {} : listeners)}
              onClick={() => { if (!renaming) onToggle(folder.id) }}
              style={{ opacity: isDragging ? 0.4 : 1 }}
              className={`group cursor-pointer transition-colors ${isOver || dropHighlight ? 'bg-accent/15' : 'hover:bg-bg-hover'}`}
              aria-expanded={expanded}
            >
              <td colSpan={7} className="px-2.5 py-1.5 border-b border-border" style={depth > 0 ? { paddingLeft: `${10 + depth * 20}px` } : undefined}>
                <div className={`flex items-center gap-1.5 rounded transition-shadow ${isOver || dropHighlight ? 'ring-2 ring-inset ring-accent/50 px-1 -mx-1' : ''}`}>
                  <Chevron size={13} className="text-muted shrink-0" />
                  <FolderGlyph folder={folder} size={14} open={expanded} />
                  {renaming ? (
                    <span className="min-w-0 flex-1 max-w-[280px]">
                      <FolderNameInput
                        initial={folder.name}
                        placeholder="Rename folder"
                        onCommit={(name) => actions.onRenameSubmit(folder, name)}
                        onCancel={actions.onRenameCancel}
                      />
                    </span>
                  ) : (
                    <span className="text-sm text-text-strong font-medium truncate">{folder.name}</span>
                  )}
                  <span className="text-[11px] text-muted">
                    {stats.artifactCount}{stats.subfolderCount > 0 ? ` · ${stats.subfolderCount} folder${stats.subfolderCount === 1 ? '' : 's'}` : ''}
                  </span>
                  <span className="ml-auto opacity-0 group-hover:opacity-100 transition-opacity">
                    <FolderMenu folder={folder} folders={folders} actions={actions} />
                  </span>
                </div>
              </td>
            </tr>
          )}
        </DndDraggable>
      )}
    </DndDroppable>
  )
}

/** Nested, collapsible tree table (browse mode): folders in pre-order with
 * their artifacts indented beneath, Unfiled at the end. Collapsed by default —
 * expansion is client-local (localStorage), by design (Mesh-2720 §2.5). */
function LibraryTree({ items, folders, expandedIds, onToggleExpand, folderActions, onOpen, onDelete, deletingSlug, overFolderId, dragActive }: {
  items: Artifact[]
  folders: ArtifactFolder[]
  expandedIds: ReadonlySet<string>
  onToggleExpand: (id: string) => void
  folderActions: FolderActions
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  /** Folder the active drag currently hovers (''=Unfiled, null=none). */
  overFolderId: string | null
  /** True while any library drag is in flight. */
  dragActive: boolean
}) {
  const folderIds = new Set(folders.map(f => f.id))
  const byFolder = new Map<string, Artifact[]>()
  for (const a of items) {
    // Dangling folder_id (deleted folder) degrades to Unfiled.
    const fid = a.folder_id && folderIds.has(a.folder_id) ? a.folder_id : ''
    const bucket = byFolder.get(fid)
    if (bucket) bucket.push(a)
    else byFolder.set(fid, [a])
  }
  const rows: React.ReactNode[] = []
  const walk = (parentId: string, depth: number, visited: Set<string>) => {
    for (const f of childFolders(folders, parentId)) {
      if (visited.has(f.id) || depth > 20) continue
      visited.add(f.id)
      const expanded = expandedIds.has(f.id)
      rows.push(
        <FolderRow
          key={`folder:${f.id}`}
          folder={f}
          folders={folders}
          depth={depth}
          expanded={expanded}
          onToggle={onToggleExpand}
          actions={folderActions}
          dropHighlight={overFolderId === f.id}
        />,
      )
      if (expanded) {
        for (const a of byFolder.get(f.id) || []) {
          rows.push(
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              indent={depth + 1}
              dropFolderId={f.id}
              dropHighlight={overFolderId === f.id}
            />,
          )
        }
        walk(f.id, depth + 1, visited)
      }
    }
  }
  walk('', 0, new Set())
  const unfiled = byFolder.get('') || []
  const unfiledHot = overFolderId === ''
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse table-striped">
        <LibraryTableHead />
        <tbody>
          {rows}
          {folders.length > 0 && (
            <DndDroppable id="unfiled-lane" data={{ type: 'folder-drop', folderId: '' }}>
              {({ setNodeRef, isOver }) => (
                <tr ref={setNodeRef} className={`transition-colors ${isOver || unfiledHot ? 'bg-accent/15' : ''}`}>
                  <td colSpan={7} className="px-2.5 border-b border-border" style={{ paddingTop: dragActive ? 10 : 6, paddingBottom: dragActive ? 10 : 6 }}>
                    <div className={`flex items-center gap-2 rounded transition-all ${
                      dragActive ? `border border-dashed px-2 py-1.5 ${isOver || unfiledHot ? 'border-accent text-text' : 'border-border text-muted'}` : ''
                    }`}>
                      <span className="text-[11px] uppercase tracking-[.04em] text-muted font-medium">
                        Unfiled · {unfiled.length}
                      </span>
                      {dragActive && (
                        <span className="text-[11px] text-muted italic">— drop here to unfile</span>
                      )}
                    </div>
                  </td>
                </tr>
              )}
            </DndDroppable>
          )}
          {unfiled.map((a) => (
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              dropFolderId={folders.length > 0 ? '' : undefined}
              dropHighlight={unfiledHot}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ArtifactsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [kindFilter, setKindFilter] = useState<string>('')

  // ── Library folders (Mesh-2720) ──────────────────────────────────────────
  // Any active filter bypasses folder scoping entirely — matches show flat
  // across all folders in the original table.
  const { folders } = useArtifactFolders()
  const filtersActive = !!(filter || tagFilter || kindFilter)

  // Tree expansion — client-local by design (§2.5): collapsed by default,
  // expanded ids persisted per browser.
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem('mc-artifact-folders-expanded') || '[]')
      return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [])
    } catch { return new Set() }
  })
  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      safeSetItem('mc-artifact-folders-expanded', JSON.stringify([...next]))
      return next
    })
  }, [])
  const expandFolder = useCallback((id: string) => {
    setExpandedIds(prev => {
      if (prev.has(id)) return prev
      const next = new Set(prev)
      next.add(id)
      safeSetItem('mc-artifact-folders-expanded', JSON.stringify([...next]))
      return next
    })
  }, [])

  const invalidateFolders = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['artifact-folders'] })
    qc.invalidateQueries({ queryKey: ['artifacts'] })
  }, [qc])
  const createFolderMut = useMutation({
    mutationFn: (body: { name: string; parent_id?: string; color?: string }) => api.createArtifactFolder(body),
    onSuccess: invalidateFolders,
  })
  const updateFolderMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; parent_id?: string; order?: number; icon?: string; color?: string } }) =>
      api.updateArtifactFolder(id, body),
    onSettled: invalidateFolders,
  })
  const [deletingFolder, setDeletingFolder] = useState<ArtifactFolder | null>(null)
  const moveArtifact = useMoveArtifactToFolder()

  const [creatingFolder, setCreatingFolder] = useState(false)
  const [newFolderColor, setNewFolderColor] = useState('')
  const [renamingFolderId, setRenamingFolderId] = useState<string | null>(null)

  // The emoji icon is derived by a background LLM task server-side after
  // create/rename — refetch shortly after so it pops in without a reload.
  const scheduleIconRefetch = useCallback(() => {
    window.setTimeout(() => qc.invalidateQueries({ queryKey: ['artifact-folders'] }), 5000)
    window.setTimeout(() => qc.invalidateQueries({ queryKey: ['artifact-folders'] }), 15000)
  }, [qc])

  const handleNewFolder = useCallback(() => { setNewFolderColor(''); setCreatingFolder(true) }, [])
  const commitNewFolder = useCallback((name: string) => {
    setCreatingFolder(false)
    // The table view creates at root (nest afterwards via drag or the menu).
    createFolderMut.mutate({
      name,
      ...(newFolderColor ? { color: newFolderColor } : {}),
    })
    scheduleIconRefetch()
  }, [createFolderMut, newFolderColor, scheduleIconRefetch])

  const folderActions = useMemo<FolderActions>(() => ({
    // The table-only library has no gallery navigation — "opening" a folder
    // expands it in the tree.
    onOpen: expandFolder,
    onRename: (f) => setRenamingFolderId(f.id),
    onMove: (f, newParentId) => {
      if (isDescendantFolder(folders, f.id, newParentId)) return
      if ((f.parent_id || '') !== newParentId) updateFolderMut.mutate({ id: f.id, body: { parent_id: newParentId } })
    },
    onDelete: (f) => {
      // An empty folder (no artifacts, no subfolders anywhere in its subtree)
      // has nothing at stake — delete it immediately, no choice dialog.
      const stats = folderSubtreeStats(folders, f.id)
      if (stats.artifactCount === 0 && stats.subfolderCount === 0) {
        api.deleteArtifactFolder(f.id, false).finally(invalidateFolders)
        return
      }
      setDeletingFolder(f)
    },
    onSetColor: (f, color) => {
      if ((f.color || '') !== color) updateFolderMut.mutate({ id: f.id, body: { color } })
    },
    renamingId: renamingFolderId,
    onRenameSubmit: (f, name) => {
      setRenamingFolderId(null)
      if (name && name !== f.name) {
        updateFolderMut.mutate({ id: f.id, body: { name } })
        scheduleIconRefetch()
      }
    },
    onRenameCancel: () => setRenamingFolderId(null),
  }), [expandFolder, updateFolderMut, folders, renamingFolderId, scheduleIconRefetch, invalidateFolders])

  const confirmDeleteFolder = useCallback(async (deleteContents: boolean) => {
    if (!deletingFolder) return
    try {
      await api.deleteArtifactFolder(deletingFolder.id, deleteContents)
    } finally {
      setDeletingFolder(null)
      invalidateFolders()
    }
  }, [deletingFolder, invalidateFolders])

  // ── Library drag-and-drop ─────────────────────────────────────────────────
  // One DndContext covers the table. Artifact → folder-drop moves it; folder
  // → folder-drop nests it into the target, cycle-guarded. (Folders sort
  // alphabetically, so there is no manual sibling reorder.) The activation
  // distance keeps clicks working.
  const dndSensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }))
  const [activeDrag, setActiveDrag] = useState<LibraryDrag | null>(null)
  // The folder the drag is currently over (''=unfile target, null=none) —
  // drives group highlighting: hovering anywhere over an expanded folder's
  // region (its rows included) lights the whole folder up as the drop target.
  const [overFolderId, setOverFolderId] = useState<string | null>(null)
  const handleDragOver = useCallback((e: { over: DragEndEvent['over'] }) => {
    const o = e.over?.data.current as { type?: string; folderId?: string } | undefined
    setOverFolderId(o?.type === 'folder-drop' ? (o.folderId ?? '') : null)
  }, [])
  const handleDragStart = useCallback((e: DragStartEvent) => {
    const d = e.active.data.current as LibraryDrag | undefined
    if (d?.type === 'artifact' || d?.type === 'folder') setActiveDrag(d)
  }, [])
  const handleDragEnd = useCallback((e: DragEndEvent) => {
    setActiveDrag(null)
    setOverFolderId(null)
    const a = e.active.data.current as LibraryDrag | undefined
    const o = e.over?.data.current as { type?: string; folderId?: string } | undefined
    if (!a || o?.type !== 'folder-drop') return
    const target = o.folderId ?? ''
    if (a.type === 'artifact') {
      if ((a.folderId || '') !== target) moveArtifact(a.slug, target)
      return
    }
    // Folder drop = nest into the target (cycle-guarded — a folder can never
    // be dropped into itself or its own subtree). Siblings sort
    // alphabetically, so there is no manual reorder: a same-parent drop is a
    // no-op.
    if (a.id === target) return
    if (isDescendantFolder(folders, a.id, target)) return
    const dragged = folders.find(f => f.id === a.id)
    if (!dragged) return
    if ((dragged.parent_id || '') !== target) {
      updateFolderMut.mutate({ id: a.id, body: { parent_id: target } })
    }
  }, [folders, moveArtifact, updateFolderMut])
  const handleDragCancel = useCallback(() => { setActiveDrag(null); setOverFolderId(null) }, [])

  const { data, isLoading, error } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', { tag: tagFilter, kind: kindFilter }],
    queryFn: () =>
      api.artifacts({
        tag: tagFilter || undefined,
        kind: kindFilter || undefined,
      }),
  })

  // Separate unfiltered query that drives the tag dropdown options so users
  // can switch between tags without first resetting to "all tags". Without
  // this, allTags would be derived only from currently-filtered results and
  // co-occurring tags would disappear when one is selected.
  const { data: allTagsData } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', 'all-tags'],
    queryFn: () => api.artifacts({}),
  })

  const artifacts = useMemo(() => data?.artifacts || [], [data])
  const allTags = useMemo(() => {
    const s = new Set<string>()
    for (const a of allTagsData?.artifacts || []) for (const t of a.tags || []) s.add(t)
    return Array.from(s).sort()
  }, [allTagsData])

  const visible = useMemo(() => {
    if (!filter) return artifacts
    const q = filter.toLowerCase()
    return artifacts.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        a.slug.toLowerCase().includes(q) ||
        (a.description || '').toLowerCase().includes(q),
    )
  }, [artifacts, filter])

  const totalVersions = artifacts.reduce((sum, a) => sum + (a.version || 1), 0)
  const widgetCount = artifacts.filter((a) => a.kind === 'widget').length

  const deleteMut = useMutation({
    mutationFn: (slug: string) => api.deleteArtifact(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['artifacts'] }),
  })

  const handleOpen = useCallback((slug: string) => navigate(`/artifacts/${slug}`), [navigate])

  const handleDelete = useCallback((a: Artifact) => {
    if (window.confirm(
      `Remove artifact "${a.slug}" from your library?\n\n` +
      `This deletes the artifact entry and its version history. ` +
      `If this artifact came from a file on disk or a chat widget, ` +
      `the original is NOT touched — you can re-add it later.`
    )) {
      deleteMut.mutate(a.slug)
    }
  }, [deleteMut])

  const errMessage = error ? (error instanceof Error ? error.message : String(error)) : null
  const mutErr = deleteMut.error
    ? deleteMut.error instanceof Error
      ? deleteMut.error.message
      : String(deleteMut.error)
    : null

  if (isLoading) return <div className="p-6 text-muted">Loading…</div>

  return (
    <>
      <PageHeader title="Artifacts" subtitle="Widgets, files, and snippets — live-tracked with version history" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Total" value={artifacts.length} accent />
          <StatCard label="Widgets" value={widgetCount} delay={60} />
          <StatCard label="Tags" value={allTags.length} delay={120} />
          <StatCard label="Total Versions" value={totalVersions} delay={180} />
        </div>

        {(errMessage || mutErr) && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <span className="text-danger text-lg shrink-0"><AlertTriangle className="lucide-inline" /></span>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-danger font-medium">Error</div>
              <div className="text-[13px] text-danger/90 mt-0.5">{errMessage || mutErr}</div>
            </div>
            <Btn onClick={() => deleteMut.reset()} className="text-danger/60 hover:text-danger shrink-0">×</Btn>
          </div>
        )}

        <Card>
          <CardTitle>
            Library{' '}
            <InfoTip text="Artifacts are persistent, versioned widgets. Save one from any rendered <mcwidget> in chat (Bookmark icon), or have the agent call artifact_save. Iterate later via 'iterate on artifact <slug>'." />
          </CardTitle>
          <div className="flex flex-wrap gap-2 items-center mb-3">
            <SearchInput
              placeholder="Filter by name, slug, description…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <select className={sel} value={kindFilter} aria-label="Filter by kind" onChange={(e) => setKindFilter(e.target.value)}>
              {KIND_OPTIONS.map((k) => (
                <option key={k} value={k}>
                  {k ? `kind: ${k}` : 'all kinds'}
                </option>
              ))}
            </select>
            <select className={sel} value={tagFilter} aria-label="Filter by tag" onChange={(e) => setTagFilter(e.target.value)}>
              <option value="">all tags</option>
              {allTags.map((t) => (
                <option key={t} value={t}>
                  tag: {t}
                </option>
              ))}
            </select>
            <Btn onClick={handleNewFolder} className="flex items-center gap-1.5 ml-auto" title="Create a folder to organize your artifacts">
              <FolderPlus size={13} /> New folder
            </Btn>
          </div>

          {creatingFolder && (
            <div className="mb-2 max-w-[360px]">
              <div className="flex items-center gap-2">
                <FolderPlus size={15} className="shrink-0" style={{ color: newFolderColor || 'var(--accent)' }} />
                <div className="min-w-0 flex-1">
                  <FolderNameInput
                    placeholder="New folder name"
                    onCommit={commitNewFolder}
                    onCancel={() => setCreatingFolder(false)}
                  />
                </div>
              </div>
              <div className="mt-1.5 ml-6">
                <FolderColorSwatches size={13} value={newFolderColor} onPick={setNewFolderColor} />
              </div>
            </div>
          )}

          {/* One DndContext spans the table so artifacts and folders can be
              dragged between folder rows and the Unfiled lane. */}
          <DndContext
            sensors={dndSensors}
            collisionDetection={artifactLibraryCollision}
            measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDragEnd={handleDragEnd}
            onDragCancel={handleDragCancel}
          >
            {artifacts.length === 0 && folders.length === 0 && !creatingFolder ? (
              <EmptyState
                icon={<Bookmark className="lucide-inline" />}
                title="No artifacts yet"
                subtitle="Click the bookmark icon on any rendered widget in chat to save it here."
              />
            ) : filtersActive ? (
              visible.length === 0 ? (
                <div className="text-muted italic px-2.5 py-3.5 text-sm">No artifacts match your filters.</div>
              ) : (
                <LibraryTable
                  items={visible}
                  onOpen={handleOpen}
                  onDelete={handleDelete}
                  deletingSlug={deleteMut.isPending ? (deleteMut.variables as string) : null}
                />
              )
            ) : (
              <LibraryTree
                items={visible}
                folders={folders}
                expandedIds={expandedIds}
                onToggleExpand={toggleExpanded}
                folderActions={folderActions}
                onOpen={handleOpen}
                onDelete={handleDelete}
                deletingSlug={deleteMut.isPending ? (deleteMut.variables as string) : null}
                overFolderId={overFolderId}
                dragActive={!!activeDrag}
              />
            )}

            <DragOverlay dropAnimation={null} modifiers={[snapOverlayToCursor]}>
              {activeDrag && (
                <div className="flex items-center gap-2 rounded-lg border border-accent bg-card px-3 py-2 shadow-lg text-sm text-text-strong max-w-[260px]">
                  {activeDrag.type === 'folder' ? (
                    (() => {
                      const gf = folders.find((f) => f.id === activeDrag.id)
                      return gf
                        ? <FolderGlyph folder={gf} size={14} />
                        : <FolderIcon size={14} className="text-accent shrink-0" />
                    })()
                  ) : (
                    <Bookmark size={14} className="text-accent shrink-0" />
                  )}
                  <span className="truncate">{activeDrag.name}</span>
                </div>
              )}
            </DragOverlay>
          </DndContext>

          <ArtifactFolderDeleteDialog
            folder={deletingFolder}
            folders={folders}
            onConfirm={confirmDeleteFolder}
            onClose={() => setDeletingFolder(null)}
          />
        </Card>
      </div>
    </>
  )
}
