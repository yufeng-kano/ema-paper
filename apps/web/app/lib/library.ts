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
import type { IconName } from '~/lib/icons'

export type FolderId = number | null

/** What the API accepts, so a field cannot present as valid what the server will refuse
 *  (`folders.name` and `documents.name`, docs/database.md). */
export const FOLDER_NAME_MAX = 255
export const DOCUMENT_NAME_MAX = 512

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
 * The path from the root down to `id`, root first — the breadcrumb.
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
 * Whether intake would parse this file — a PDF or an image — judged by the server's own two
 * lists (`GET /api/me` § limits). Any other file is taken too, and kept as it is
 * (docs/web.md § Files); this decides the cap, the glyph and whether it converts.
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

/**
 * The cap intake applies to this file, from the server's own numbers (`GET /api/me`
 * § limits): a markdown document's, a parse's for a PDF or an image while the parse agent
 * converts them (`autoConvert`, its switch), a file's for anything else — the PDF or image
 * kept as a file included. Which cap, never whether it is taken, since every kind is
 * (docs/web.md § Files). Null while the limits have not arrived, or where an older API
 * names none.
 */
export function uploadCap(
  file: { name: string; type: string },
  limits: UploadLimits | null,
  autoConvert = true,
): number | null {
  if (!limits) {
    return null
  }
  if (isMarkdownUpload(file, limits)) {
    return limits.markdown_max_bytes ?? null
  }
  if (autoConvert && acceptsUpload(file, limits)) {
    return limits.upload_max_bytes
  }
  return limits.file_max_bytes ?? null
}

/** The glyph before a file's name, by its type (docs/web.md § Files, *Four columns*). */
export type FileGlyph = 'file' | 'file-text' | 'image' | 'film' | 'music' | 'sheet' | 'presentation' | 'archive'

/**
 * What a file is, in the terms the Kind column and the glyph use: the catalog key that
 * names its kind (`files.kinds.<key>`) and the format's name for the key's `{format}` —
 * *JPEG image*, *Excel spreadsheet*, *ZIP archive*, else *<EXT> file*.
 */
export interface FileKind {
  key: 'pdf' | 'image' | 'video' | 'audio' | 'spreadsheet' | 'presentation' | 'word' | 'archive' | 'file' | 'plain'
  format: string
  glyph: FileGlyph
}

type MediaKind = 'image' | 'video' | 'audio'

/** The names of the common media subtypes where the subtype is not already the name. */
const FORMAT_NAMES = new Map<string, string>([
  ['jpeg', 'JPEG'],
  ['png', 'PNG'],
  ['gif', 'GIF'],
  ['webp', 'WebP'],
  ['heic', 'HEIC'],
  ['heif', 'HEIF'],
  ['avif', 'AVIF'],
  ['tiff', 'TIFF'],
  ['bmp', 'BMP'],
  ['svg+xml', 'SVG'],
  ['mp4', 'MPEG-4'],
  ['quicktime', 'QuickTime'],
  ['webm', 'WebM'],
  ['x-matroska', 'Matroska'],
  ['mpeg', 'MPEG'],
  ['x-msvideo', 'AVI'],
  ['mp3', 'MP3'],
  ['wav', 'WAV'],
  ['x-wav', 'WAV'],
  ['flac', 'FLAC'],
  ['x-flac', 'FLAC'],
  ['aac', 'AAC'],
  ['ogg', 'Ogg'],
  ['x-m4a', 'M4A'],
  ['mp4a', 'M4A'],
])

type OfficeKind = { key: 'spreadsheet' | 'presentation' | 'word'; format: string }

const EXCEL: OfficeKind = { key: 'spreadsheet', format: 'Excel' }
const NUMBERS: OfficeKind = { key: 'spreadsheet', format: 'Numbers' }
const ODS: OfficeKind = { key: 'spreadsheet', format: 'OpenDocument' }
const POWERPOINT: OfficeKind = { key: 'presentation', format: 'PowerPoint' }
const KEYNOTE: OfficeKind = { key: 'presentation', format: 'Keynote' }
const ODP: OfficeKind = { key: 'presentation', format: 'OpenDocument' }
const WORD: OfficeKind = { key: 'word', format: 'Word' }
const PAGES: OfficeKind = { key: 'word', format: 'Pages' }
const ODT: OfficeKind = { key: 'word', format: 'OpenDocument' }

/** Office documents by media type, then by extension: the suite's name is the one a reader
 *  knows, and a browser types these generically as often as not. */
const OFFICE = new Map<string, OfficeKind>([
  ['application/vnd.ms-excel', EXCEL],
  ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', EXCEL],
  ['application/vnd.apple.numbers', NUMBERS],
  ['application/vnd.oasis.opendocument.spreadsheet', ODS],
  ['application/vnd.ms-powerpoint', POWERPOINT],
  ['application/vnd.openxmlformats-officedocument.presentationml.presentation', POWERPOINT],
  ['application/vnd.apple.keynote', KEYNOTE],
  ['application/vnd.oasis.opendocument.presentation', ODP],
  ['application/msword', WORD],
  ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', WORD],
  ['application/vnd.apple.pages', PAGES],
  ['application/vnd.oasis.opendocument.text', ODT],
  ['xls', EXCEL],
  ['xlsx', EXCEL],
  ['xlsm', EXCEL],
  ['numbers', NUMBERS],
  ['ods', ODS],
  ['ppt', POWERPOINT],
  ['pptx', POWERPOINT],
  ['key', KEYNOTE],
  ['odp', ODP],
  ['doc', WORD],
  ['docx', WORD],
  ['pages', PAGES],
  ['odt', ODT],
])

/** Archives by media type, then by extension. */
const ARCHIVES = new Map<string, string>([
  ['application/zip', 'ZIP'],
  ['application/x-zip-compressed', 'ZIP'],
  ['application/x-7z-compressed', '7-Zip'],
  ['application/vnd.rar', 'RAR'],
  ['application/x-rar-compressed', 'RAR'],
  ['application/x-tar', 'tar'],
  ['application/gzip', 'gzip'],
  ['application/x-gzip', 'gzip'],
  ['application/x-bzip2', 'bzip2'],
  ['application/x-xz', 'xz'],
  ['zip', 'ZIP'],
  ['7z', '7-Zip'],
  ['rar', 'RAR'],
  ['tar', 'tar'],
  ['gz', 'gzip'],
  ['tgz', 'gzip'],
  ['bz2', 'bzip2'],
  ['xz', 'xz'],
])

const GLYPHS: Readonly<Record<FileKind['key'], FileGlyph>> = {
  pdf: 'file-text',
  image: 'image',
  video: 'film',
  audio: 'music',
  spreadsheet: 'sheet',
  presentation: 'presentation',
  word: 'file-text',
  archive: 'archive',
  file: 'file',
  plain: 'file',
}

/** The extension, lower-cased and without its dot; a leading dot is a name, not an extension. */
function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot <= 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/**
 * What a file kept as it is is — by its media type first, as intake stored it, then by its
 * extension, since a browser types what it has no association for as nothing at all.
 */
export function fileKind(file: { name: string; media_type?: string | null }): FileKind {
  const type = (file.media_type ?? '').toLowerCase()
  const slash = type.indexOf('/')
  const top = slash === -1 ? '' : type.slice(0, slash)
  const subtype = slash === -1 ? '' : type.slice(slash + 1)
  const extension = extensionOf(file.name)
  if (type === 'application/pdf' || extension === 'pdf') {
    return { key: 'pdf', format: 'PDF', glyph: GLYPHS.pdf }
  }
  if (top === 'image' || top === 'video' || top === 'audio') {
    const media: MediaKind = top
    return { key: media, format: FORMAT_NAMES.get(subtype) ?? (extension || subtype).toUpperCase(), glyph: GLYPHS[media] }
  }
  const office = OFFICE.get(type) ?? OFFICE.get(extension)
  if (office) {
    return { ...office, glyph: GLYPHS[office.key] }
  }
  const archive = ARCHIVES.get(type) ?? ARCHIVES.get(extension)
  if (archive) {
    return { key: 'archive', format: archive, glyph: GLYPHS.archive }
  }
  return extension
    ? { key: 'file', format: extension.toUpperCase(), glyph: GLYPHS.file }
    : { key: 'plain', format: '', glyph: GLYPHS.plain }
}

/** The glyph before a row's name: a document's by what it was converted from, a file's by
 *  its type (docs/web.md § Files, *Four columns*). */
export function documentGlyph(document: { kind: string; name: string; media_type?: string | null }): IconName {
  if (document.kind === 'file') {
    return fileKind(document).glyph
  }
  return document.kind === 'image' ? 'image' : 'file-text'
}

/** What the file viewer shows of a file by itself: what a browser renders safely on its own
 *  and the file route serves inline (docs/api.md § `GET /api/library/documents/{id}/file`).
 *  An SVG is served as a download, so it has none. */
export type FilePreview = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'none'

export function filePreview(mediaType: string | null | undefined): FilePreview {
  const type = (mediaType ?? '').toLowerCase()
  if (type === 'image/svg+xml') {
    return 'none'
  }
  for (const media of ['image', 'video', 'audio'] as const) {
    if (type.startsWith(`${media}/`)) {
      return media
    }
  }
  if (type === 'application/pdf') {
    return 'pdf'
  }
  if (type === 'text/plain') {
    return 'text'
  }
  return 'none'
}
