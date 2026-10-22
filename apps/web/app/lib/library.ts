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

/** A re-convert on its way: queued, running, or succeeded and not yet taken by its document
 *  — what the list polls for. A failed one waits for the reader (docs/jobs.md
 *  § Re-converting a document). */
export function isReconverting(document: LibraryDocument): boolean {
  return !!document.reconvert && document.reconvert.status !== 'failed'
}

/** 0..1 of the pages that came back, for the meter on a running row — its parse's, or its
 *  re-convert's. */
export function parseProgress(document: Pick<LibraryDocument, 'page_count' | 'pages_done'>): number {
  if (!document.page_count || document.pages_done === null) {
    return 0
  }
  return Math.min(1, Math.max(0, document.pages_done / document.page_count))
}

/** How many pages the retry dialog draws; the rest are a count (docs/web.md § Files). */
export const RETRY_TILES_MAX = 12

/**
 * Why a page failed, as the API writes it (docs/parsing.md § Vision-only conversion), to the
 * catalog key that names it (`files.retry.reasons.*`). A reason missing here reads as the
 * API wrote it.
 */
export const PAGE_FAILURE_REASONS: Readonly<Record<string, string>> = {
  blocked: 'blocked',
  'cut off': 'cutOff',
  'upstream call failed': 'upstream',
  'rate limited': 'rateLimited',
  payment: 'payment',
  'render failed': 'render',
  'the image could not be decoded': 'image',
}

/** The reason a failed format check gives in the job's error — `the format check failed
 *  (<reason>)` (docs/parsing.md § Format check) — or null for any other error. */
export function checkFailureReason(error: string | null): string | null {
  return /^the format check failed \((.+)\)$/.exec(error ?? '')?.[1] ?? null
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

/** Whether the upload endpoint would file this as a markdown document rather than parse it —
 *  by type or by extension, as the server decides (docs/workspace.md § Markdown documents). */
export function isMarkdownUpload(file: { name: string; type: string }, limits: UploadLimits | null): boolean {
  if (file.type && limits?.markdown_media_types?.includes(file.type)) {
    return true
  }
  const dot = file.name.lastIndexOf('.')
  const extension = dot === -1 ? '' : file.name.slice(dot).toLowerCase()
  return extension !== '' && (limits?.markdown_extensions?.includes(extension) ?? false)
}

/**
 * How much of a name a rename selects when it opens: a document's name up to its extension
 * (`DINO` of `DINO.pdf`), a folder's whole name — Finder's choice, since the extension is the
 * part a rename almost never means to change. A leading dot is a name, not an extension.
 */
export function renameSelectionEnd(name: string, folder: boolean): number {
  const dot = name.lastIndexOf('.')
  return folder || dot <= 0 ? name.length : dot
}

/** The keys from `from` to `to`, both included, in list order — what ⇧-click and ⇧↑↓ select.
 *  With `from` gone from the list, the run is `to` alone. */
export function keyRange(keys: string[], from: string, to: string): string[] {
  const start = keys.indexOf(from)
  const end = keys.indexOf(to)
  if (end === -1) {
    return []
  }
  if (start === -1) {
    return [to]
  }
  return keys.slice(Math.min(start, end), Math.max(start, end) + 1)
}

/**
 * The row typing selects: the first, in list order, whose name starts with what was typed —
 * case-insensitively, as a file manager's type-ahead reads names.
 */
export function typeAheadMatch(rows: { key: string; name: string }[], typed: string): string | null {
  const prefix = typed.toLocaleLowerCase()
  return rows.find((row) => row.name.toLocaleLowerCase().startsWith(prefix))?.key ?? null
}
