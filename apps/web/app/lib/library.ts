/**
 * The shape of the file library, as pure functions over what `GET /api/library` returned
 * (docs/web.md § Files).
 *
 * The page holds the whole tree in memory and navigates locally, so every question it asks
 * — what is in this folder, where am I, which folders can this one move into — is answered
 * here rather than by a request. Keeping them pure is also what makes them testable: the
 * tree is the part of this screen with real logic in it.
 *
 * `null` is the root everywhere. It is not a folder row, it is the absence of a parent, and
 * treating it as an id would mean inventing a folder the server does not have.
 */
import type { LibraryDocument, LibraryFolder, UploadLimits } from '~/lib/api'

export type FolderId = number | null

/** What the API accepts, so a field cannot present as valid what the server will refuse
 *  (`folders.name` and `documents.name`, docs/database.md). */
export const FOLDER_NAME_MAX = 255
export const DOCUMENT_NAME_MAX = 512

/** One line of the rail's tree: the folder, how deep it sits, and whether it opens. */
export interface TreeRow {
  folder: LibraryFolder
  depth: number
  hasChildren: boolean
}

/**
 * Names sort the way a file manager sorts them: case-insensitively, and with runs of
 * digits compared as numbers so "Page 2" precedes "Page 10".
 */
function byName(a: { name: string }, b: { name: string }): number {
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
}

export function childFolders(folders: LibraryFolder[], parent: FolderId): LibraryFolder[] {
  return folders.filter((folder) => folder.parent_id === parent).sort(byName)
}

export function documentsIn(documents: LibraryDocument[], folder: FolderId): LibraryDocument[] {
  return documents.filter((document) => document.folder_id === folder).sort(byName)
}

/**
 * The tree as a flat list of rows — the shape a rail can render and a keyboard can walk.
 *
 * A collapsed folder contributes its own row and nothing below it, which is what makes
 * `expanded` the only state the tree needs.
 */
export function flattenTree(
  folders: LibraryFolder[],
  expanded: ReadonlySet<number>,
  parent: FolderId = null,
  depth = 0,
): TreeRow[] {
  const rows: TreeRow[] = []
  for (const folder of childFolders(folders, parent)) {
    const children = childFolders(folders, folder.id)
    rows.push({ folder, depth, hasChildren: children.length > 0 })
    if (expanded.has(folder.id)) {
      rows.push(...flattenTree(folders, expanded, folder.id, depth + 1))
    }
  }
  return rows
}

/**
 * The path from the root down to `id`, root first — the breadcrumb, and the set of folders
 * that must be expanded for the open one to be visible.
 *
 * The `seen` guard is not paranoia about the server: it is what stops a cycle that should
 * be impossible from hanging the page instead of showing a short path.
 */
export function folderPath(folders: LibraryFolder[], id: FolderId): LibraryFolder[] {
  const byId = new Map(folders.map((folder) => [folder.id, folder]))
  const path: LibraryFolder[] = []
  const seen = new Set<number>()
  let current = id
  while (current !== null && !seen.has(current)) {
    seen.add(current)
    const folder = byId.get(current)
    if (!folder) {
      break
    }
    path.unshift(folder)
    current = folder.parent_id
  }
  return path
}

/** `root` and every folder under it — the set a folder may not be moved into. */
export function subtreeIds(folders: LibraryFolder[], root: number): Set<number> {
  const found = new Set<number>([root])
  const frontier = [root]
  while (frontier.length) {
    const current = frontier.pop()!
    for (const child of folders.filter((folder) => folder.parent_id === current)) {
      if (!found.has(child.id)) {
        found.add(child.id)
        frontier.push(child.id)
      }
    }
  }
  return found
}

/**
 * Whether dropping `moving` into `target` is a move at all — refused in the client for the
 * same reasons the server refuses it, so a drag that cannot land never highlights.
 */
export function canMoveFolder(
  folders: LibraryFolder[],
  moving: LibraryFolder,
  target: FolderId,
): boolean {
  if (target === moving.parent_id) {
    return false
  }
  return target === null || !subtreeIds(folders, moving.id).has(target)
}

/** A parse still in flight: the two states a file's row keeps polling for. */
export function isPending(document: LibraryDocument): boolean {
  return document.status === 'queued' || document.status === 'running'
}

/** 0..1 of the pages that came back, for the meter on a running row. */
export function parseProgress(document: LibraryDocument): number {
  if (!document.page_count) {
    return 0
  }
  return Math.min(1, Math.max(0, document.pages_done / document.page_count))
}

/**
 * Whether the upload endpoint would take this file, judged by the server's own two lists
 * (`GET /api/me` § limits).
 *
 * The extension is not a fallback for tidiness: a browser reports a generic type — or none
 * at all — for a file it has no association for, which is routine for `.heic` and for
 * anything dragged out of a file manager that does not guess. Intake falls back to the
 * extension in exactly that case, so judging on the media type alone would make this page
 * stricter than the API it posts to and refuse files the server would have parsed.
 */
export function acceptsUpload(
  file: { name: string; type: string },
  limits: UploadLimits | null,
): boolean {
  if (!limits) {
    // The limits have not arrived; the server is the authority either way.
    return true
  }
  if (file.type && limits.accepted_media_types.includes(file.type)) {
    return true
  }
  const dot = file.name.lastIndexOf('.')
  const extension = dot === -1 ? '' : file.name.slice(dot).toLowerCase()
  return extension !== '' && limits.accepted_extensions.includes(extension)
}
