<script setup lang="ts">
/**
 * The file library: the folder that is open, and a Trash (docs/web.md § Files).
 *
 * It works the way the desktop's file manager does — Finder's model, with ⌘ read as Ctrl off
 * Apple platforms — because that is the one model every reader already has. Each of these is
 * a decision rather than a detail:
 *
 *  - **One read.** `GET /api/library` returns the whole tree and the Trash, so opening a
 *    folder is a filter over data already in memory. There is no spinner between a click and
 *    a list.
 *  - **A click selects, a double click opens**, and every action takes the selection — the
 *    menus, Finder's keys, a drag. Nothing that can be undone asks first: Move to Trash is
 *    immediate, and ⌘Z takes back any file operation made here.
 *  - **Nothing pops up.** A new item and a rename are named in their row; only the two
 *    deletes nothing can undo ask, in a strip over the Trash's list.
 *  - **Uploading is a queue.** The server caps running jobs per user, so a five-file drop
 *    earns 429s if it fires at once. Files go up one at a time and a refusal is treated as
 *    back-pressure: the file waits and tries again. Every dropped file is a row from the
 *    moment it is dropped, well before the server knows about it.
 *  - **Polling, not streaming.** While anything is queued or running, the same one read
 *    repeats every couple of seconds and refreshes every row's status and progress at once.
 *
 * Nothing on this screen explains itself in prose: the drop target, the empty folder and
 * the menu items say what they are.
 */
import type { ComponentPublicInstance } from 'vue'
import type { LocationQueryRaw } from 'vue-router'
import {
  ApiRequestError,
  copyDocument,
  copyFolder,
  createDocument,
  createFolder,
  deleteDocument,
  deleteFolder,
  dismissReconvert,
  emptyTrash,
  getDocumentResult,
  getLibrary,
  restoreDocument,
  restoreFolder,
  trashDocument,
  trashFolder,
  updateDocument,
  updateFolder,
  uploadDocument,
  creditsRefusalOf,
  type CreditsRefusal,
  type JobResult,
  type JobStatus,
  type LibraryDocument,
  type LibraryFolder,
  type TrashedDocument,
  type TrashedFolder,
} from '~/lib/api'
import {
  carriesFiles,
  claimDrag,
  dragClaim,
  droppedFiles,
  ROWS_DRAG_TYPE,
  setDropEffect,
} from '~/lib/dnd'
import { formatBytes, formatShortDateTime } from '~/lib/format'
import type { MenuItem, Point } from '~/lib/menu'
import {
  acceptsUpload,
  childFolders,
  DOCUMENT_NAME_MAX,
  FOLDER_NAME_MAX,
  documentsIn,
  folderPath,
  isMarkdownUpload,
  isPending,
  isReconverting,
  keyRange,
  parseProgress,
  renameSelectionEnd,
  subtreeIds,
  typeAheadMatch,
  type FolderId,
} from '~/lib/library'
import type { TableColumn } from '~/lib/table'

definePageMeta({ middleware: 'authed' })

/** How often the list re-reads itself while a parse is in flight. */
const POLL_MS = 2000
/** How long a file refused by the per-user job cap waits before trying again. */
const BACKOFF_MS = 3000
/** How long a row reached through `?document=` stays lit. */
const REVEAL_MS = 2400
/** How long a drag rests on a folder before the folder springs open under it. */
const SPRING_MS = 800
/** How long typed letters keep adding to the name being looked for. */
const TYPE_AHEAD_MS = 1000
/** How long the status note shows, and how long one carrying *Undo* waits for the reader
 *  (docs/web.md § Editing, *Undo and the status note*). */
const NOTE_MS = 1600
const NOTE_UNDO_MS = 8000
/** How many file operations ⌘Z can take back. */
const UNDO_MAX = 50

const { t, locale } = useI18n()
const auth = useAuth()
const { resolve } = useApiError()
const { apple, label, matches } = useShortcuts()
const { data, pending, errorMessage, refresh } = useAuthedData(() => getLibrary())

const folders = computed<LibraryFolder[]>(() => data.value?.folders ?? [])
const documents = computed<LibraryDocument[]>(() => data.value?.documents ?? [])
const trash = computed(() => data.value?.trash ?? { folders: [], documents: [] })
const trashTotal = computed(() => trash.value.folders.length + trash.value.documents.length)

/** Which of the two tabs is showing. */
const view = ref<'files' | 'trash'>('files')

// --- where we are ----------------------------------------------------------

/**
 * The open folder and the open document live in the URL — `?folder=<id>`, `?doc=<id>` — so
 * Back and Forward walk them and a refresh or a pasted address opens the same place
 * (docs/web.md § Files). Anything that is not a positive id reads as none.
 */
const route = useRoute()
const router = useRouter()

function queryId(value: unknown): number | null {
  const id = typeof value === 'string' ? Number(value) : Number.NaN
  return Number.isInteger(id) && id > 0 ? id : null
}

/** The query the last navigation asked for, until it lands, and the navigations one after
 *  another: a navigation started before the previous one landed would cancel it, and one
 *  built from the route before it moved would undo it. */
let wantedQuery: LocationQueryRaw | null = null
let navigating: Promise<unknown> = Promise.resolve()

function writeQuery(patch: Record<string, string | undefined>, replace: boolean) {
  const query = { ...(wantedQuery ?? route.query), ...patch }
  wantedQuery = query
  navigating = navigating
    .then(() => (replace ? router.replace({ query }) : router.push({ query })))
    .finally(() => {
      if (wantedQuery === query) {
        wantedQuery = null
      }
    })
}

const currentFolder = ref<FolderId>(queryId(route.query.folder))
const crumbs = computed(() => folderPath(folders.value, currentFolder.value))

/** The folder in the URL: a new history entry, or the current one replaced when the folder
 *  went away on its own. */
function writeFolder(id: FolderId, replace = false) {
  if (queryId((wantedQuery ?? route.query).folder) === id) {
    return
  }
  writeQuery({ folder: id === null ? undefined : String(id) }, replace)
}

/** Back and Forward move the folder; the screen follows. */
watch(
  () => route.query.folder,
  (value) => {
    const id = queryId(value)
    if (id !== currentFolder.value) {
      currentFolder.value = id
      clearSelection()
    }
  },
)

/**
 * Open a folder. `select` is the row to select on arrival — the folder just left, when the
 * way there was up (Esc, ⌘↑, a crumb), so the keys go on from where the reader was.
 */
function openFolder(id: FolderId, select?: number) {
  currentFolder.value = id
  writeFolder(id)
  clearSelection()
  if (select !== undefined) {
    selectOnly(`folder-${select}`)
    scrollToRow(`folder-${select}`)
  }
}

/** Esc and ⌘↑: the folder above, with the one just left selected. */
function goUp() {
  const here = currentFolder.value
  if (here === null) {
    return
  }
  openFolder(folders.value.find((folder) => folder.id === here)?.parent_id ?? null, here)
}

/** A crumb opens its folder with the next folder of the path selected — the way back down. */
function openCrumb(id: FolderId) {
  const path = crumbs.value
  const index = id === null ? -1 : path.findIndex((crumb) => crumb.id === id)
  const below = path[index + 1]
  if (id === currentFolder.value) {
    return
  }
  openFolder(id, below?.id)
}

function showView(next: 'files' | 'trash') {
  if (view.value === next) {
    return
  }
  view.value = next
  confirming.value = null
  clearSelection()
}

/** ← and → between the two tabs, as a tab list is expected to. */
function onTabKeydown(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
    return
  }
  event.preventDefault()
  showView(view.value === 'files' ? 'trash' : 'files')
  void nextTick(() => document.getElementById(`files-tab-${view.value}`)?.focus())
}

/**
 * `/files?document=<id>` — History's *Show in Files* (docs/web.md § Files). Read once and
 * replaced away, so a refresh or a later visit opens at the top level as usual. With
 * `&block=<anchor>` — a block's *Copy link to block* — the document opens too, scrolled to
 * that block.
 */
const revealId = Number(route.query.document) || null
const revealBlock = typeof route.query.block === 'string' ? route.query.block : null
const revealed = ref<number | null>(null)
/** The block link's target, handed to the viewer only while it shows that document. */
const linkedBlock = ref<{ id: number; anchor: string } | null>(null)
let revealTimer: ReturnType<typeof setTimeout> | undefined
if (route.query.document !== undefined) {
  writeQuery({ document: undefined, block: undefined }, true)
}

if (revealId !== null) {
  // `once`: the first change of `data` is the first load; later polls must not pull the
  // reader back to this folder.
  watch(
    data,
    async (library) => {
      const target = library?.documents.find((row) => row.id === revealId)
      if (!target) {
        return
      }
      currentFolder.value = target.folder_id
      writeFolder(target.folder_id, true)
      revealed.value = target.id
      await nextTick()
      selectOnly(`file-${target.id}`)
      document.querySelector('.contents tr.revealed')?.scrollIntoView({ block: 'center' })
      revealTimer = setTimeout(() => (revealed.value = null), REVEAL_MS)
      if (revealBlock !== null) {
        linkedBlock.value = { id: target.id, anchor: revealBlock }
        void showResult(target)
      }
    },
    { once: true },
  )
}

onBeforeUnmount(() => clearTimeout(revealTimer))

/** A folder deleted or trashed in another tab would otherwise leave this screen looking at
 *  nothing, and so would a `?folder` the library does not hold, Back to a folder trashed
 *  since included — checked whenever either moves, once there is a library. */
watch(
  [folders, currentFolder],
  ([list, here]) => {
    if (!data.value) {
      return
    }
    if (here !== null && !list.some((row) => row.id === here)) {
      currentFolder.value = null
      writeFolder(null, true)
    }
  },
  { immediate: true },
)

// --- rows ------------------------------------------------------------------

/** A file being uploaded exists here before the server has heard of it. */
interface PendingUpload {
  localId: number
  name: string
  size: number
  folderId: FolderId
  file: File
  /** `waiting` covers both "not started" and "refused by the job cap, trying again". */
  state: 'waiting' | 'uploading'
}

type Row =
  | { kind: 'folder'; key: string; folder: LibraryFolder }
  | { kind: 'file'; key: string; document: LibraryDocument }
  | { kind: 'upload'; key: string; upload: PendingUpload }

/** A row of the Trash: an item trashed itself, never one inside a trashed folder. */
type TrashRow =
  | { kind: 'folder'; key: string; folder: TrashedFolder }
  | { kind: 'file'; key: string; document: TrashedDocument }

const queue = ref<PendingUpload[]>([])

/** Folders first, then files, then what is still going up — a file manager's order. */
const rows = computed<Row[]>(() => [
  ...childFolders(folders.value, currentFolder.value).map<Row>((folder) => ({
    kind: 'folder',
    key: `folder-${folder.id}`,
    folder,
  })),
  ...documentsIn(documents.value, currentFolder.value).map<Row>((document) => ({
    kind: 'file',
    key: `file-${document.id}`,
    document,
  })),
  ...queue.value
    .filter((upload) => upload.folderId === currentFolder.value)
    .map<Row>((upload) => ({ kind: 'upload', key: `upload-${upload.localId}`, upload })),
])

/** The latest first, folders and files together, as the Trash lists them. */
const trashRows = computed<TrashRow[]>(() =>
  [
    ...trash.value.folders.map<TrashRow>((folder) => ({ kind: 'folder', key: `folder-${folder.id}`, folder })),
    ...trash.value.documents.map<TrashRow>((document) => ({
      kind: 'file',
      key: `file-${document.id}`,
      document,
    })),
  ].sort((a, b) => trashedAt(b).localeCompare(trashedAt(a))),
)

function trashedAt(row: TrashRow): string {
  return row.kind === 'folder' ? row.folder.trashed_at : row.document.trashed_at
}

const columns = computed<TableColumn<Row>[]>(() => [
  { key: 'name', header: t('files.columnName') },
  { key: 'status', header: t('files.columnStatus'), width: '150px' },
  { key: 'pages', header: t('files.columnPages'), numeric: true, width: '84px' },
  { key: 'size', header: t('files.columnSize'), numeric: true, width: '96px' },
  { key: 'added', header: t('files.columnAdded'), numeric: true, width: '148px' },
])

const trashColumns = computed<TableColumn<TrashRow>[]>(() => [
  { key: 'name', header: t('files.columnName') },
  { key: 'origin', header: t('files.columnOrigin'), width: '220px' },
  { key: 'size', header: t('files.columnSize'), numeric: true, width: '110px' },
  { key: 'trashed', header: t('files.columnTrashed'), numeric: true, width: '168px' },
])

const STATUS_TONE: Record<JobStatus, 'neutral' | 'info' | 'ok' | 'danger'> = {
  queued: 'neutral',
  running: 'info',
  succeeded: 'ok',
  failed: 'danger',
}

/** Where Put Back returns an item, as the column says it: the folder's name, or Home. */
function originName(origin: string[]): string {
  return origin.at(-1) ?? t('files.root')
}

function originPath(origin: string[]): string {
  return [t('files.root'), ...origin].join(' › ')
}

// --- the selection ---------------------------------------------------------

/**
 * A row that can be selected, in either tab: everything but a file still uploading, which
 * has nothing to act on yet.
 */
interface Entry {
  kind: 'folder' | 'file'
  id: number
  key: string
  name: string
}

const entries = computed<Entry[]>(() => {
  const list: (Row | TrashRow)[] = view.value === 'files' ? rows.value : trashRows.value
  return list.flatMap<Entry>((row) => {
    if (row.kind === 'folder') {
      return [{ kind: 'folder', id: row.folder.id, key: row.key, name: row.folder.name }]
    }
    if (row.kind === 'file') {
      return [{ kind: 'file', id: row.document.id, key: row.key, name: row.document.name }]
    }
    return []
  })
})
const entryKeys = computed(() => entries.value.map((entry) => entry.key))

/** The selected rows, by key; replaced whole on every change. */
const selected = ref<ReadonlySet<string>>(new Set())
/** The row the arrow keys move from: the last one clicked or reached. */
const cursor = ref<string | null>(null)
/** Where a ⇧-click or a ⇧↑↓ run starts. */
let anchor: string | null = null

/** The selection, in list order — what every action takes. */
const selection = computed(() => entries.value.filter((entry) => selected.value.has(entry.key)))

function selectOnly(key: string) {
  selected.value = new Set([key])
  cursor.value = key
  anchor = key
}

function selectKeys(keys: string[]) {
  selected.value = new Set(keys)
  cursor.value = keys.at(-1) ?? null
  anchor = keys[0] ?? null
}

function toggleSelected(key: string) {
  const next = new Set(selected.value)
  if (!next.delete(key)) {
    next.add(key)
  }
  selected.value = next
  cursor.value = key
  anchor = key
}

/** ⇧: the run from the anchor to `key`, the anchor kept for the next one. */
function extendTo(key: string) {
  selected.value = new Set(keyRange(entryKeys.value, anchor ?? key, key))
  cursor.value = key
  anchor ??= key
}

function clearSelection() {
  selected.value = new Set()
  cursor.value = null
  anchor = null
}

/** A poll, an action or another tab can take rows away; what is gone leaves the selection. */
watch(entryKeys, (keys) => {
  const live = new Set(keys)
  if ([...selected.value].some((key) => !live.has(key))) {
    selected.value = new Set([...selected.value].filter((key) => live.has(key)))
  }
  if (cursor.value !== null && !live.has(cursor.value)) {
    cursor.value = null
  }
  if (anchor !== null && !live.has(anchor)) {
    anchor = null
  }
  // A row renamed elsewhere out of the list takes its field with it.
  if (renaming.value && !live.has(renaming.value.key)) {
    renaming.value = null
  }
})

/** After a row left the list, the one now standing in its place is selected, so the keys
 *  keep a place. */
async function selectAt(index: number) {
  await nextTick()
  const keys = entryKeys.value
  const key = keys[Math.min(index, keys.length - 1)]
  if (key === undefined) {
    clearSelection()
  } else {
    selectOnly(key)
    scrollToRow(key)
  }
}

function rowId(key: string): string {
  return `files-row-${key}`
}

function scrollToRow(key: string) {
  void nextTick(() => document.getElementById(rowId(key))?.scrollIntoView({ block: 'nearest' }))
}

/** What the last press was made with: on touch a tap opens, where a click selects. */
let pointerKind = 'mouse'

function onPointerDown(event: PointerEvent) {
  pointerKind = event.pointerType || 'mouse'
}

/** The platform's command key on a click: ⌘ on Apple platforms, Ctrl elsewhere. */
function modClick(event: MouseEvent): boolean {
  return apple ? event.metaKey : event.ctrlKey
}

function onRowClick(event: MouseEvent, key: string) {
  if (renaming.value?.key === key) {
    return
  }
  if (pointerKind === 'touch') {
    // A coarse pointer has no double click and no modifier keys (docs/web.md § Files).
    selectOnly(key)
    openEntry(key)
    return
  }
  if (event.shiftKey && anchor !== null) {
    extendTo(key)
  } else if (modClick(event)) {
    toggleSelected(key)
  } else {
    selectOnly(key)
  }
}

function onRowDblclick(event: MouseEvent, key: string) {
  if (pointerKind === 'touch' || event.shiftKey || modClick(event) || renaming.value?.key === key) {
    return
  }
  openEntry(key)
}

/** A click on the list's own empty space clears the selection, as a file manager's does. */
function onListClick(event: MouseEvent) {
  if (!(event.target as Element).closest('tbody tr, button, a, input')) {
    clearSelection()
  }
}

/** Open a row: a folder in place, a document in the viewer. Nothing in the Trash opens. */
function openEntry(key: string) {
  if (view.value === 'trash') {
    showNote(t('files.note.inTrash'), false)
    return
  }
  const row = rows.value.find((entry) => entry.key === key)
  if (row?.kind === 'folder') {
    openFolder(row.folder.id)
  } else if (row?.kind === 'file') {
    void showResult(row.document)
  }
}

// --- uploading -------------------------------------------------------------

const fileInput = ref<HTMLInputElement | null>(null)
const mutationError = ref<string | null>(null)
/** The 402 of an upload: the dialog that says what the document needs (docs/web.md § Files). */
const creditsRefusal = ref<CreditsRefusal | null>(null)
const limits = computed(() => auth.me.value?.limits ?? null)
/** Both lists: a file picker filters on either, and a system that types `.heic` as
 *  nothing at all would otherwise grey the file out. */
const accept = computed(() =>
  limits.value
    ? [...limits.value.accepted_media_types, ...limits.value.accepted_extensions].join(',')
    : '',
)

let nextLocalId = 1
let draining = false
/** Set on unmount: a queue retrying against the job cap must not outlive the screen. */
let left = false

/** Where the picker's files will land. The OS dialog answers long after the menu has gone,
 *  and the reader may have opened another folder meanwhile. */
let pickInto: FolderId = null

function pickFiles(into: FolderId) {
  pickInto = into
  fileInput.value?.click()
}

function onPicked(event: Event) {
  const input = event.target as HTMLInputElement
  addToQueue([...(input.files ?? [])], pickInto)
  // Cleared so picking the same file twice in a row still fires `change`.
  input.value = ''
}

/**
 * A file the server would refuse is refused here, from the server's own numbers
 * (`GET /api/me` § limits) — an upload that cannot succeed should not spend the upload.
 */
function rejected(file: File): string | null {
  // A markdown file is not parsed; its cap is the one its text is read under.
  const max = isMarkdownUpload(file, limits.value) ? limits.value?.markdown_max_bytes : limits.value?.upload_max_bytes
  if (max && file.size > max) {
    return t('files.tooLarge', { name: file.name, limit: formatBytes(max, locale.value) })
  }
  // Judged the way intake judges it — media type, then extension — so this page never
  // refuses a file the API would have taken (`lib/library.ts`).
  if (!acceptsUpload(file, limits.value)) {
    return t('files.unsupported', { name: file.name })
  }
  return null
}

function addToQueue(files: File[], folderId: FolderId) {
  const accepted: PendingUpload[] = []
  for (const file of files) {
    const refusal = rejected(file)
    if (refusal) {
      mutationError.value = refusal
      continue
    }
    accepted.push({
      localId: nextLocalId++,
      name: file.name,
      size: file.size,
      folderId,
      file,
      state: 'waiting',
    })
  }
  if (!accepted.length) {
    return
  }
  // One file on its own is "upload this and read it", so its result opens itself when it
  // lands. A batch does not: five dialogs racing each other is not a feature.
  autoPreviewFor = accepted.length === 1 && !queue.value.length ? -1 : null
  queue.value.push(...accepted)
  void drain()
}

/**
 * Upload the queue, one file at a time.
 *
 * Sequential because the per-user cap on running jobs is real (docs/jobs.md § Concurrency):
 * firing five uploads in parallel would just collect 429s. A 429 here is back-pressure, not
 * a failure — the file stays at the head of the queue and tries again once a job finishes.
 */
async function drain() {
  if (draining) {
    return
  }
  draining = true
  try {
    while (queue.value.length && !left) {
      const item = queue.value[0]!
      item.state = 'uploading'
      try {
        const created = await uploadDocument(item.file, item.folderId)
        queue.value.shift()
        if (autoPreviewFor === -1) {
          autoPreviewFor = created.id
        }
        await refresh()
      } catch (error) {
        const api = error instanceof ApiRequestError ? error : null
        // Back-pressure from the job cap and a request that never left the machine are
        // both "not now". Neither is a reason to make someone find the file again, which
        // is the whole promise of having a queue (docs/web.md § Files).
        if (api && (api.type === 'rate_limit' || api.isOffline)) {
          if (api.isOffline) {
            mutationError.value = await resolve(error)
          }
          item.state = 'waiting'
          await new Promise((done) => setTimeout(done, BACKOFF_MS))
          continue
        }
        queue.value.shift()
        // Not enough credits: nothing was stored or charged, and the dialog says how far
        // short the balance is (docs/billing.md § Refusals).
        const refusal = creditsRefusalOf(error)
        if (refusal) {
          creditsRefusal.value = refusal
          continue
        }
        mutationError.value = await resolve(error)
      }
    }
  } finally {
    draining = false
  }
}

// --- live progress ---------------------------------------------------------

// A re-convert on its way polls too: its row shows its progress, and its document takes it
// when it succeeds (docs/web.md § Files).
const anyPending = computed(() => documents.value.some((row) => isPending(row) || isReconverting(row)))
let poller: ReturnType<typeof setInterval> | undefined

function stopPolling() {
  clearInterval(poller)
  poller = undefined
}

watch(anyPending, (running) => {
  if (!running) {
    stopPolling()
    return
  }
  poller ??= setInterval(() => {
    // Skipped while a read is already in flight, so a slow answer cannot pile requests up.
    if (!pending.value) {
      void refresh()
    }
  }, POLL_MS)
})

onBeforeUnmount(() => {
  left = true
  stopPolling()
})

// --- the result viewer -----------------------------------------------------

const openDocument = ref<LibraryDocument | null>(null)
/** The failed parse whose retry dialog is open: opening a failed document opens this. A
 *  failed re-convert's comes as its document with the re-convert's state (`reconvertRetry`). */
const retrying = ref<LibraryDocument | null>(null)
/** The parsed document whose re-convert dialog is open. */
const reconverting = ref<LibraryDocument | null>(null)
// Read once: closing the viewer, or opening another document, spends the link.
watch(
  () => openDocument.value?.id,
  (id) => {
    if (linkedBlock.value && id !== linkedBlock.value.id) {
      linkedBlock.value = null
    }
  },
)

const results = reactive(new Map<number, JobResult>())
const resultErrors = reactive(new Map<number, string>())
/** Ids with a result request in flight, each with the parse it was made for — a map, since
 *  the viewer can be reopened on another file while the first request is still running. */
const loadingResults = reactive(new Map<number, string | null>())
/** The single upload whose result should open itself; `-1` while it has no id yet. */
let autoPreviewFor: number | null = null

/** One read of the document's result. At most one request per document and parse in
 *  flight: one made before a re-convert was taken in answers nothing, since the read for
 *  the new parse replaced it (docs/web.md § Files). */
async function fetchResult(document: LibraryDocument) {
  const job = document.job_id
  if (loadingResults.get(document.id) === job) {
    return
  }
  loadingResults.set(document.id, job)
  resultErrors.delete(document.id)
  try {
    const result = await getDocumentResult(document.id)
    if (loadingResults.get(document.id) === job) {
      results.set(document.id, result)
    }
  } catch (error) {
    const message = await resolve(error)
    if (loadingResults.get(document.id) === job) {
      resultErrors.set(document.id, message)
    }
  } finally {
    if (loadingResults.get(document.id) === job) {
      loadingResults.delete(document.id)
    }
  }
}

async function showResult(document: LibraryDocument) {
  // A parse opens only once every page has succeeded: until then its row's status and
  // progress are what there is to see (docs/web.md § Files).
  if (isPending(document)) {
    showNote(t(document.checking ? 'files.note.checking' : 'files.note.parsing'), false)
    return
  }
  // Reading something by hand settles what the screen is for; a queued auto-preview must
  // not take it away when it finishes.
  autoPreviewFor = null
  // A failed parse has no result to read, ever: opening it is its retry (docs/web.md § Files).
  if (document.status === 'failed') {
    retrying.value = document
    return
  }
  openDocument.value = document
  // A markdown document has no result: the viewer reads its copy (docs/web.md § Files).
  if (document.kind === 'markdown' || results.has(document.id)) {
    return
  }
  await fetchResult(document)
}

/**
 * The open document in the URL (`?doc=<id>`, docs/web.md § Files). Opening one pushes an
 * entry, so Back closes the viewer; closing it here goes back over that entry rather than
 * adding another, or drops `doc` when the viewer was opened from the URL itself.
 */
let docPushed = false
watch(
  () => openDocument.value?.id ?? null,
  (id) => {
    if (queryId((wantedQuery ?? route.query).doc) === id) {
      return
    }
    if (id !== null) {
      docPushed = true
      writeQuery({ doc: String(id) }, false)
    } else if (docPushed) {
      docPushed = false
      navigating = navigating.then(() => router.back())
    } else {
      writeQuery({ doc: undefined }, true)
    }
  },
)

/** A `doc` that arrives with the URL — a refresh, a pasted address, Forward — opens once the
 *  library has it; one that cannot be opened is dropped. */
async function openDocQuery() {
  const id = queryId(route.query.doc)
  if (id === null || id === openDocument.value?.id || !data.value) {
    return
  }
  const target = documents.value.find((row) => row.id === id)
  if (target) {
    await showResult(target)
  }
  // Only while the URL still asks for this one: a reader who has moved on meanwhile keeps
  // what they opened.
  if (openDocument.value?.id !== id && queryId(route.query.doc) === id) {
    writeQuery({ doc: undefined }, true)
  }
}

/** Back taking `doc` away closes the viewer; Forward bringing one opens it. */
watch(
  () => route.query.doc,
  (value) => {
    if (queryId(value) === null) {
      docPushed = false
      openDocument.value = null
      return
    }
    void openDocQuery()
  },
)
watch(data, () => void openDocQuery(), { once: true })
onMounted(() => void openDocQuery())

/** The open viewer follows its own row: a parse only opens finished, so there is nothing
 *  more of it to read, but its name and folder can still change — and a re-convert that
 *  lands makes the document another parse, whose result is read again and whose viewer
 *  starts over on the new version (docs/web.md § Files). */
watch(documents, (list, previous) => {
  const before = new Map((previous ?? []).map((row) => [row.id, row.job_id]))
  for (const row of list) {
    if (before.has(row.id) && before.get(row.id) !== row.job_id) {
      // A read still on its way for the old parse answers nothing (`fetchResult`).
      results.delete(row.id)
      resultErrors.delete(row.id)
      loadingResults.delete(row.id)
    }
  }
  const open = openDocument.value
  const fresh = open && list.find((row) => row.id === open.id)
  if (fresh) {
    openDocument.value = fresh
    if (fresh.job_id !== open.job_id) {
      showNote(t('files.note.reconverted'), false)
      void fetchResult(fresh)
    }
  }
})

/** What the open viewer says while its document is being re-converted. */
const reconvertNote = computed(() => {
  const open = openDocument.value
  return open && isReconverting(open)
    ? t('files.reconvertingNote', { model: open.reconvert?.model ?? '' })
    : null
})

/** The upload that just finished parsing opens itself — its retry, if it failed — unless
 *  the reader is already reading something, in which case taking the screen away would be
 *  rude. A retried document waits here the same way. */
watch(documents, (list) => {
  if (autoPreviewFor === null || autoPreviewFor < 0 || openDocument.value || retrying.value) {
    return
  }
  const target = list.find((row) => row.id === autoPreviewFor)
  if (!target || isPending(target)) {
    return
  }
  if (target.folder_id !== currentFolder.value || view.value !== 'files') {
    // The reader walked off to another folder while it parsed. "Upload it and read it"
    // was about the folder they were in, not about interrupting wherever they are now.
    autoPreviewFor = null
    return
  }
  void showResult(target)
})

/** Queued again: the row says so at once, and the document opens by itself once every page
 *  has succeeded — or this dialog comes back, if a page fails again. A failed re-convert's
 *  document opens already: the re-convert is taken in when it succeeds, and nothing opens. */
async function onRetried() {
  const retried = retrying.value
  autoPreviewFor = retried && retried.reconvert?.status !== 'failed' ? retried.id : null
  retrying.value = null
  await refresh()
}

/** A failed re-convert as the retry dialog reads a document: the pages, the error and the
 *  model are the re-convert's, and so is what the retry sends (docs/api.md). */
function reconvertRetry(document: LibraryDocument): LibraryDocument {
  const reconvert = document.reconvert!
  return {
    ...document,
    status: reconvert.status,
    model: reconvert.model,
    error: reconvert.error,
    failed_pages: reconvert.failed_pages,
  }
}

/** Started: the row shows the re-convert from the next read on. */
async function onReconvertStarted() {
  reconverting.value = null
  await refresh()
}

// --- file operations -------------------------------------------------------

/** An item an operation acted on, as the undo stack keeps it. */
interface Item {
  kind: 'folder' | 'file'
  id: number
}

/** A move as undo needs it: where from and where to, and the name on each side — the
 *  destination suffixes a name it already holds. */
interface Move {
  item: Item
  from: FolderId
  to: FolderId
  fromName: string
  toName: string
}

/**
 * One file operation, as ⌘Z takes it back (docs/web.md § Files, *Undo*). Trashing and
 * putting back are each other's undo; whatever made items appear — a copy, a paste, a
 * duplicate, a new folder or document, a Put Back — is undone by trashing them.
 */
type Step =
  | { type: 'trash'; items: Item[] }
  | { type: 'appear'; items: Item[] }
  | { type: 'move'; moves: Move[] }
  | { type: 'rename'; item: Item; from: string; to: string }

const undoSteps: Step[] = []
const redoSteps: Step[] = []

/** A new operation: the step on the stack, and no redo past it. */
function record(step: Step) {
  undoSteps.push(step)
  if (undoSteps.length > UNDO_MAX) {
    undoSteps.shift()
  }
  redoSteps.length = 0
}

function stepItems(step: Step): Item[] {
  if (step.type === 'move') {
    return step.moves.map((move) => move.item)
  }
  return step.type === 'rename' ? [step.item] : step.items
}

/** Deleted for good: no step can bring these back, so none is kept that would try. */
function forget(gone: Item[]) {
  const ids = new Set(gone.map((item) => `${item.kind}-${item.id}`))
  for (const stack of [undoSteps, redoSteps]) {
    const kept = stack.filter((step) => !stepItems(step).some((item) => ids.has(`${item.kind}-${item.id}`)))
    stack.splice(0, stack.length, ...kept)
  }
}

/** One request in flight at a time: a second key press while one runs does nothing. */
const busy = ref(false)

/**
 * Every operation is the same steps: run it, re-read the library, report what went wrong
 * over the list. The re-read runs on failure too — a batch that stopped halfway has still
 * changed what it changed. A note carrying *Undo* is replaced by the next action.
 */
async function run(action: () => Promise<unknown>): Promise<boolean> {
  if (busy.value) {
    return false
  }
  busy.value = true
  mutationError.value = null
  confirming.value = null
  hideUndoNote()
  try {
    await action()
    return true
  } catch (error) {
    mutationError.value = await resolve(error)
    return false
  } finally {
    await refresh()
    busy.value = false
  }
}

function sendToTrash(item: Item): Promise<unknown> {
  return item.kind === 'folder' ? trashFolder(item.id) : trashDocument(item.id)
}

function putBackItem(item: Item): Promise<unknown> {
  return item.kind === 'folder' ? restoreFolder(item.id) : restoreDocument(item.id)
}

/** Move, rename, or both, by the item's own route; the payload carries the name it got. */
function place(item: Item, body: { folder?: FolderId; name?: string }): Promise<{ name: string }> {
  if (item.kind === 'folder') {
    return updateFolder(item.id, {
      ...(body.folder !== undefined ? { parent_id: body.folder } : {}),
      ...(body.name !== undefined ? { name: body.name } : {}),
    })
  }
  return updateDocument(item.id, {
    ...(body.folder !== undefined ? { folder_id: body.folder } : {}),
    ...(body.name !== undefined ? { name: body.name } : {}),
  })
}

/** The live row behind an item: its folder and its name now, which a stored entry may no
 *  longer have. */
function liveRow(item: Item): { parent: FolderId; name: string } | null {
  if (item.kind === 'folder') {
    const folder = folders.value.find((row) => row.id === item.id)
    return folder ? { parent: folder.parent_id, name: folder.name } : null
  }
  const document = documents.value.find((row) => row.id === item.id)
  return document ? { parent: document.folder_id, name: document.name } : null
}

/**
 * `items` to the Trash, at once, with Undo: the selection on ⌘⌫ (Delete off Apple
 * platforms), or what a drag carried to the Trash's tab — which a folder that sprang open on
 * the way has taken out of the list, and so out of the selection.
 */
async function trashItems(items: Entry[]) {
  if (!items.length || busy.value) {
    return
  }
  // -1 when the rows were left behind in another folder: the open one keeps its selection.
  const index = entryKeys.value.indexOf(items[0]!.key)
  const done: Item[] = []
  await run(async () => {
    try {
      for (const item of items) {
        await sendToTrash(item)
        done.push({ kind: item.kind, id: item.id })
      }
    } finally {
      if (done.length) {
        record({ type: 'trash', items: done })
      }
    }
  })
  if (done.length) {
    if (done.some((item) => item.kind === 'file' && item.id === openDocument.value?.id)) {
      openDocument.value = null
    }
    showNote(t('files.note.trashed', { n: done.length }, done.length), true)
    if (index !== -1) {
      await selectAt(index)
    }
  }
}

/** Put Back, from the Trash: where each came from, or the nearest live folder above. */
async function putBackSelection() {
  const items = selection.value
  if (!items.length || busy.value) {
    return
  }
  const index = entryKeys.value.indexOf(items[0]!.key)
  const done: Item[] = []
  await run(async () => {
    try {
      for (const item of items) {
        await putBackItem(item)
        done.push({ kind: item.kind, id: item.id })
      }
    } finally {
      if (done.length) {
        record({ type: 'appear', items: done })
      }
    }
  })
  if (done.length) {
    showNote(t('files.note.putBack', { n: done.length }, done.length), true)
    await selectAt(index)
  }
}

/** Copies of `items` in `destination` — a paste, a duplicate, an ⌥-drag — selected after. */
async function copyInto(items: Entry[], destination: FolderId) {
  if (!items.length || busy.value) {
    return
  }
  const made: Item[] = []
  await run(async () => {
    try {
      for (const item of items) {
        const copy = item.kind === 'folder' ? await copyFolder(item.id, destination) : await copyDocument(item.id, destination)
        made.push({ kind: item.kind, id: copy.id })
      }
    } finally {
      if (made.length) {
        record({ type: 'appear', items: made })
      }
    }
  })
  if (made.length && view.value === 'files' && destination === currentFolder.value) {
    await nextTick()
    selectKeys(made.map((item) => `${item.kind}-${item.id}`))
    scrollToRow(`${made.at(-1)!.kind}-${made.at(-1)!.id}`)
  }
}

/** `items` into `destination` — a drag, or ⌥⌘V. An item already there stays as it is. */
async function moveInto(items: Entry[], destination: FolderId): Promise<boolean> {
  if (!items.length || busy.value) {
    return false
  }
  const moves: Move[] = []
  const ok = await run(async () => {
    try {
      for (const item of items) {
        const live = liveRow(item)
        if (live?.parent === destination) {
          continue
        }
        const moved = await place(item, { folder: destination })
        moves.push({
          item: { kind: item.kind, id: item.id },
          from: live?.parent ?? null,
          to: destination,
          fromName: live?.name ?? item.name,
          toName: moved.name,
        })
      }
    } finally {
      if (moves.length) {
        record({ type: 'move', moves })
      }
    }
  })
  return ok
}

// --- the clipboard ---------------------------------------------------------

/** What ⌘C copied: the page's own, not the system's, which cannot hold a library row. */
const clipboard = ref<Entry[]>([])

/** A text selection is the browser's to copy, as it is everywhere else. */
function textSelected(): boolean {
  const range = window.getSelection()
  return !!range && !range.isCollapsed && range.toString().trim() !== ''
}

function copySelection() {
  const items = selection.value
  if (!items.length) {
    return
  }
  clipboard.value = items.map((item) => ({ ...item }))
  showNote(t('files.note.copied', { n: items.length }, items.length), false)
}

function pasteHere() {
  void copyInto(clipboard.value, currentFolder.value)
}

/** ⌥⌘V, Finder's *Move Item Here*: the copied rows move, and the clipboard empties. */
async function moveHere() {
  const items = clipboard.value
  if (!items.length) {
    return
  }
  const destination = currentFolder.value
  if (await moveInto(items, destination)) {
    clipboard.value = []
    if (view.value === 'files' && destination === currentFolder.value) {
      await nextTick()
      selectKeys(items.map((item) => item.key).filter((key) => entryKeys.value.includes(key)))
    }
  }
}

function duplicateSelection() {
  void copyInto(selection.value, currentFolder.value)
}

// --- naming ----------------------------------------------------------------

/**
 * The rename in progress: the row's name as a field in its row (docs/web.md § Files,
 * *Nothing pops up for a file operation*). `original` is what Esc puts back.
 */
const renaming = ref<{ key: string; item: Item; original: string; value: string; busy: boolean } | null>(null)
const renameField = ref<ComponentPublicInstance | null>(null)

function renameInput(): HTMLInputElement | null {
  return (renameField.value?.$el as HTMLInputElement | undefined) ?? null
}

/** The field takes focus with the name before its extension selected — Finder's choice. */
async function focusRename() {
  await nextTick()
  const editing = renaming.value
  const input = renameInput()
  if (!editing || !input) {
    return
  }
  input.focus()
  input.setSelectionRange(0, renameSelectionEnd(editing.value, editing.item.kind === 'folder'))
}

function startRename(key: string) {
  const entry = entries.value.find((row) => row.key === key)
  if (!entry || view.value !== 'files' || busy.value) {
    return
  }
  selectOnly(key)
  renaming.value = { key, item: { kind: entry.kind, id: entry.id }, original: entry.name, value: entry.name, busy: false }
  void focusRename()
}

/** The list takes focus back, so the keys go on from the row. */
function focusList() {
  listEl.value?.focus({ preventScroll: true })
}

function cancelRename() {
  renaming.value = null
  focusList()
}

/**
 * Return or a click anywhere else keeps the name. Empty or unchanged sends nothing; a name
 * the server refuses — one already taken — keeps the field open with it selected, and the
 * message over the list.
 */
async function commitRename() {
  const editing = renaming.value
  if (!editing || editing.busy) {
    return
  }
  const name = editing.value.trim()
  if (!name || name === editing.original) {
    renaming.value = null
    return
  }
  editing.busy = true
  mutationError.value = null
  hideUndoNote()
  try {
    const saved = await place(editing.item, { name })
    record({ type: 'rename', item: editing.item, from: editing.original, to: saved.name })
    await refresh()
    if (renaming.value === editing) {
      // Return leaves focus in the field; a click that ended the edit took it where it
      // meant to, and the list must not take it back from there.
      const kept = document.activeElement === renameInput() || document.activeElement === document.body
      renaming.value = null
      if (kept) {
        focusList()
      }
    }
  } catch (error) {
    mutationError.value = await resolve(error)
    if (renaming.value !== editing) {
      return
    }
    editing.busy = false
    // Still on screen: back to the field. Gone — another folder opened meanwhile — the
    // message is all that is left of it.
    if (entryKeys.value.includes(editing.key)) {
      void focusRename()
    } else {
      renaming.value = null
    }
  }
}

/** A click elsewhere keeps the name. The window losing focus — another app, a system
 *  dialog — does not end the edit: the field still holds focus there, and gets it back. */
function onRenameBlur() {
  if (document.activeElement !== renameInput()) {
    void commitRename()
  }
}

function onRenameKeydown(event: KeyboardEvent) {
  if (event.isComposing) {
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    void commitRename()
  } else if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    cancelRename()
  }
}

/** New folder and New document: made at once with the default name, then named in place. */
async function createEntry(kind: 'folder' | 'file') {
  if (busy.value || view.value !== 'files') {
    return
  }
  const into = currentFolder.value
  let made: Item | null = null
  await run(async () => {
    const created =
      kind === 'folder'
        ? await createFolder({ name: t('files.newFolderName'), parent_id: into })
        : await createDocument({ name: t('files.newDocumentName'), folder_id: into })
    made = { kind, id: created.id }
    record({ type: 'appear', items: [made] })
  })
  const item = made as Item | null
  if (item && currentFolder.value === into && view.value === 'files') {
    await nextTick()
    startRename(`${item.kind}-${item.id}`)
  }
}

// --- undo ------------------------------------------------------------------

async function replay(step: Step, direction: 'undo' | 'redo') {
  const back = direction === 'undo'
  if (step.type === 'trash' || step.type === 'appear') {
    const hide = (step.type === 'trash') !== back
    for (const item of back ? [...step.items].reverse() : step.items) {
      await (hide ? sendToTrash(item) : putBackItem(item))
    }
  } else if (step.type === 'move') {
    for (const move of back ? [...step.moves].reverse() : step.moves) {
      // Back under its old name too, when the destination had suffixed it.
      const name = back && move.toName !== move.fromName ? move.fromName : undefined
      await place(move.item, { folder: back ? move.from : move.to, name })
    }
  } else {
    await place(step.item, { name: back ? step.from : step.to })
  }
}

/** ⌘Z and ⇧⌘Z. A step that fails — the library changed elsewhere — is dropped with its
 *  message, never retried. */
async function stepHistory(direction: 'undo' | 'redo') {
  const from = direction === 'undo' ? undoSteps : redoSteps
  if (busy.value || !from.length) {
    return
  }
  const step = from.pop()!
  const ok = await run(() => replay(step, direction))
  if (ok) {
    ;(direction === 'undo' ? redoSteps : undoSteps).push(step)
    showNote(t(direction === 'undo' ? 'viewer.note.undone' : 'viewer.note.redone'), false)
  }
}

// --- deleting for good -----------------------------------------------------

/** The question the strip over the Trash's list asks: these items, or all of it. */
const confirming = ref<{ items: Entry[] } | { all: number } | null>(null)
const confirmCancel = ref<ComponentPublicInstance | null>(null)

const confirmText = computed(() => {
  const ask = confirming.value
  if (!ask) {
    return ''
  }
  if ('all' in ask) {
    return t('files.confirmEmpty', { n: ask.all }, ask.all)
  }
  return ask.items.length === 1
    ? t('files.confirmDelete', { name: ask.items[0]!.name })
    : t('files.confirmDeleteItems', { n: ask.items.length }, ask.items.length)
})

/** The strip opens with Cancel focused: the one answer that loses nothing. */
function ask(question: { items: Entry[] } | { all: number }) {
  confirming.value = question
  void nextTick(() => (confirmCancel.value?.$el as HTMLElement | undefined)?.focus())
}

function askDelete() {
  const items = selection.value
  if (items.length && !busy.value) {
    ask({ items })
  }
}

function askEmpty() {
  if (trashTotal.value && !busy.value) {
    ask({ all: trashTotal.value })
  }
}

function cancelConfirm() {
  confirming.value = null
  focusList()
}

async function confirmDelete() {
  const question = confirming.value
  if (!question || busy.value) {
    return
  }
  const index = 'items' in question ? entryKeys.value.indexOf(question.items[0]!.key) : 0
  const gone: Item[] = []
  const everything = trashRows.value.map<Item>((row) => ({ kind: row.kind, id: row.kind === 'folder' ? row.folder.id : row.document.id }))
  const ok = await run(async () => {
    try {
      if ('all' in question) {
        await emptyTrash()
        gone.push(...everything)
        return
      }
      for (const item of question.items) {
        await (item.kind === 'folder' ? deleteFolder(item.id) : deleteDocument(item.id))
        gone.push({ kind: item.kind, id: item.id })
      }
    } finally {
      forget(gone)
    }
  })
  if (ok) {
    showNote('all' in question ? t('files.note.emptied') : t('files.note.deleted', { n: gone.length }, gone.length), false)
  }
  if ('items' in question) {
    await selectAt(index)
  }
  focusList()
}

// --- the status note -------------------------------------------------------

/** The one line at the list's foot an action with nothing else to show confirms in. */
const note = ref<{ text: string; undo: boolean } | null>(null)
let noteTimer: ReturnType<typeof setTimeout> | undefined
let noteHeld = false

function showNote(text: string, undoable: boolean) {
  note.value = { text, undo: undoable }
  announce(text)
  timeNote()
}

function timeNote() {
  clearTimeout(noteTimer)
  const shown = note.value
  if (shown && !noteHeld) {
    noteTimer = setTimeout(() => (note.value = null), shown.undo ? NOTE_UNDO_MS : NOTE_MS)
  }
}

/** The pointer resting on the note holds it; leaving starts its time again. */
function holdNote(held: boolean) {
  noteHeld = held
  if (held) {
    clearTimeout(noteTimer)
  } else {
    timeNote()
  }
}

/** A note carrying *Undo* stays until the reader's next action. */
function hideUndoNote() {
  if (note.value?.undo) {
    clearTimeout(noteTimer)
    note.value = null
  }
}

onBeforeUnmount(() => clearTimeout(noteTimer))

/** The page's one polite live region, for what its notes and the keys' selection say. */
const liveText = ref('')

function announce(text: string) {
  liveText.value = ''
  void nextTick(() => (liveText.value = text))
}

/** A selection the keys made is said: the row's name, or how many rows. */
function announceSelection() {
  const items = selection.value
  if (items.length === 1) {
    announce(items[0]!.name)
  } else if (items.length > 1) {
    announce(t('files.selected', { n: items.length }, items.length))
  }
}

// --- the keys --------------------------------------------------------------

/** The selection data region: both tabs and the list under them. */
const region = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

let typed = ''
let typedTimer: ReturnType<typeof setTimeout> | undefined

/** ↑ and ↓ select the next row, ⇧ extends; from nothing, they start at an end. */
function moveCursor(step: 1 | -1, extend: boolean) {
  const keys = entryKeys.value
  if (!keys.length) {
    return
  }
  const at = cursor.value === null ? -1 : keys.indexOf(cursor.value)
  const next =
    at === -1 ? keys[step > 0 ? 0 : keys.length - 1]! : keys[Math.max(0, Math.min(keys.length - 1, at + step))]!
  if (extend && anchor !== null) {
    extendTo(next)
  } else {
    selectOnly(next)
  }
  scrollToRow(next)
  announceSelection()
}

function selectEdge(last: boolean) {
  const key = last ? entryKeys.value.at(-1) : entryKeys.value[0]
  if (key !== undefined) {
    selectOnly(key)
    scrollToRow(key)
    announceSelection()
  }
}

/** Letters typed in a row select the first name they start, as a file manager's do. */
function typeAhead(char: string) {
  clearTimeout(typedTimer)
  typed += char
  typedTimer = setTimeout(() => (typed = ''), TYPE_AHEAD_MS)
  const key = typeAheadMatch(entries.value, typed)
  if (key !== null) {
    selectOnly(key)
    scrollToRow(key)
    announceSelection()
  }
}

onBeforeUnmount(() => clearTimeout(typedTimer))

/**
 * Finder's keys, read at the window so they work after a click with nothing holding focus
 * (docs/web.md § Files, *The keyboard is Finder's*). Never from a text field, an open menu,
 * a dialog or the viewer — each owns its keys — nor from anything outside this screen's
 * region, the sidebar's links among them.
 */
function onKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || event.isComposing || openDocument.value || menu.value) {
    return
  }
  if (document.querySelector('[aria-modal="true"]')) {
    return
  }
  const target = event.target as HTMLElement | null
  if (target?.isContentEditable || target?.closest('input, textarea, select')) {
    return
  }
  if (target && target !== document.body && !region.value?.contains(target)) {
    return
  }
  // A focused button or link answers Return and Space itself.
  if ((event.key === 'Enter' || event.key === ' ') && target?.closest('button, a')) {
    return
  }
  const key = (spec: string) => matches(event, spec)
  const files = view.value === 'files'
  const one = selection.value.length === 1
  const act = (handler: () => unknown) => {
    event.preventDefault()
    if (!event.repeat) {
      void handler()
    }
  }

  if (confirming.value) {
    if (key('esc')) {
      event.preventDefault()
      cancelConfirm()
    }
    return
  }
  if (key('down') || key('up')) {
    event.preventDefault()
    moveCursor(key('down') ? 1 : -1, false)
  } else if (key('shift+down') || key('shift+up')) {
    event.preventDefault()
    moveCursor(key('shift+down') ? 1 : -1, true)
  } else if (key('home') || key('end')) {
    event.preventDefault()
    selectEdge(key('end'))
  } else if (key('mod+a')) {
    event.preventDefault()
    selectKeys(entryKeys.value)
    announceSelection()
  } else if (key('mod+down') || (!apple && key('enter'))) {
    act(() => one && openEntry(selection.value[0]!.key))
  } else if (files && ((apple && key('enter')) || key('f2'))) {
    act(() => one && startRename(selection.value[0]!.key))
  } else if (files && (key('esc') || key('mod+up'))) {
    if (currentFolder.value !== null) {
      event.preventDefault()
      goUp()
    }
  } else if (files && key('alt+mod+n')) {
    act(() => createEntry('folder'))
  } else if (files && key('mod+c')) {
    if (selection.value.length && !textSelected()) {
      act(copySelection)
    }
  } else if (files && key('mod+v')) {
    act(pasteHere)
  } else if (files && key('alt+mod+v')) {
    act(moveHere)
  } else if (files && key('mod+d')) {
    act(duplicateSelection)
  } else if (files && (key('mod+backspace') || key('delete'))) {
    act(() => trashItems(selection.value))
  } else if (!files && key('mod+backspace')) {
    act(putBackSelection)
  } else if (!files && (apple ? key('alt+mod+backspace') : key('delete'))) {
    act(askDelete)
  } else if (key('mod+z')) {
    act(() => stepHistory('undo'))
  } else if (key('shift+mod+z') || (!apple && key('mod+y'))) {
    act(() => stepHistory('redo'))
  } else if (event.key.length === 1 && event.key !== ' ' && !event.metaKey && !event.ctrlKey && !event.altKey) {
    typeAhead(event.key)
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// --- the menu --------------------------------------------------------------

/**
 * The menus (docs/web.md § Files, *The menus act on the selection*). A row's acts on the
 * selection — the row is selected first when it was not — and the list's own empty space
 * acts on the folder that is open, or on the whole Trash.
 */
type MenuOn = 'selection' | 'list'

/** `fromKey` is how it was opened: only then does the menu open with a row highlighted. */
const menu = ref<{ on: MenuOn; at: Point; fromKey?: boolean } | null>(null)
const press = useLongPress()

function openMenu(on: MenuOn, at: Point, fromKey = false) {
  // A menu opened by the platform's own long press must not be followed by ours.
  press.cancel()
  menu.value = { on, at, fromKey }
}

function openRowMenu(key: string, at: Point) {
  if (!selected.value.has(key)) {
    selectOnly(key)
  }
  openMenu('selection', at)
}

function openListMenu(at: Point) {
  clearSelection()
  openMenu('list', at)
}

/**
 * The keyboard's route in — the Menu key, or `Shift+F10` where there is no Menu key — with
 * the list focused. It has no pointer, so the menu opens under the row it is about, as
 * Windows and GNOME both do, or under the list with nothing selected.
 */
function onListKeydown(event: KeyboardEvent) {
  if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) {
    return
  }
  event.preventDefault()
  const key = cursor.value !== null && selected.value.has(cursor.value) ? cursor.value : selection.value[0]?.key
  const row = key ? document.getElementById(rowId(key)) : null
  if (row) {
    const box = row.getBoundingClientRect()
    openMenu('selection', { x: box.left, y: box.bottom }, true)
  } else {
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
    clearSelection()
    openMenu('list', { x: box.left, y: box.top }, true)
  }
}

function folderName(id: FolderId): string {
  return folders.value.find((folder) => folder.id === id)?.name ?? t('files.root')
}

const menuItems = computed<MenuItem[]>(() => {
  const on = menu.value?.on
  if (!on) {
    return []
  }
  const items = selection.value
  if (view.value === 'trash') {
    if (on === 'list') {
      return [{ key: 'empty', label: t('files.emptyTrash'), icon: 'trash', danger: true, disabled: !trashTotal.value }]
    }
    return [
      { key: 'putBack', label: t('files.putBack'), icon: 'undo-2', shortcut: label('mod+backspace') },
      {
        key: 'deleteNow',
        label: t('files.deleteNow'),
        icon: 'x',
        danger: true,
        separated: true,
        shortcut: label(apple ? 'alt+mod+backspace' : 'delete'),
      },
    ]
  }
  if (on === 'list') {
    const copied = clipboard.value.length
    return [
      { key: 'upload', label: t('files.upload'), icon: 'upload' },
      { key: 'newDocument', label: t('files.newDocument'), icon: 'file-plus' },
      { key: 'newFolder', label: t('files.newFolder'), icon: 'plus', shortcut: label('alt+mod+n') },
      {
        key: 'paste',
        label: copied ? t('files.pasteItems', { n: copied }, copied) : t('files.paste'),
        icon: 'clipboard-paste',
        disabled: !copied,
        separated: true,
        shortcut: label('mod+v'),
      },
      ...(copied
        ? [
            {
              key: 'moveHere',
              label: t('files.moveHere', { n: copied }, copied),
              icon: 'folder-input' as const,
              shortcut: label('alt+mod+v'),
            },
          ]
        : []),
    ]
  }
  const one = items.length === 1 ? items[0]! : null
  const parse = one?.kind === 'file' ? documents.value.find((row) => row.id === one.id) : undefined
  return [
    ...(one
      ? [
          {
            key: 'open',
            label: t('files.open'),
            icon: one.kind === 'folder' ? ('folder-open' as const) : ('file-text' as const),
            shortcut: label(apple ? 'mod+down' : 'enter'),
            // A parse opens only once every page has succeeded (docs/web.md § Files).
            disabled: !!parse && isPending(parse),
          },
          // With no page missing (its format check failed), the retry is of the whole document.
          ...(parse?.status === 'failed'
            ? [
                {
                  key: 'retry',
                  label: t(parse.failed_pages ? 'files.retryFailed' : 'files.retryWhole'),
                  icon: 'rotate-ccw' as const,
                },
              ]
            : []),
          // A failed re-convert is retried or let go; a new one replaces it as well.
          ...(parse?.status === 'succeeded' && parse.reconvert?.status === 'failed'
            ? [
                {
                  key: 'retryReconvert',
                  label: t(parse.reconvert.failed_pages ? 'files.retryFailed' : 'files.retryWhole'),
                  icon: 'rotate-ccw' as const,
                },
                { key: 'dismissReconvert', label: t('files.dismissReconvert'), icon: 'x' as const },
              ]
            : []),
          ...(parse?.status === 'succeeded' && !isReconverting(parse)
            ? [{ key: 'reconvert', label: t('files.reconvert'), icon: 'refresh' as const }]
            : []),
          { key: 'rename', label: t('files.rename'), icon: 'edit' as const, shortcut: label(apple ? 'enter' : 'f2') },
        ]
      : []),
    { key: 'copy', label: t('files.copy'), icon: 'copy', separated: !!one, shortcut: label('mod+c') },
    { key: 'duplicate', label: t('files.duplicate'), icon: 'copy-plus', shortcut: label('mod+d') },
    {
      key: 'trash',
      label: t('files.moveToTrash'),
      icon: 'trash',
      danger: true,
      separated: true,
      shortcut: label(apple ? 'mod+backspace' : 'delete'),
    },
  ]
})

/** The menu's accessible name is what it acts on — the only place that is ever said. */
const menuLabel = computed(() => {
  const on = menu.value?.on
  if (!on) {
    return ''
  }
  if (on === 'list') {
    return t('files.menuFor', { name: view.value === 'trash' ? t('files.tabTrash') : folderName(currentFolder.value) })
  }
  const items = selection.value
  return items.length === 1
    ? t('files.menuFor', { name: items[0]!.name })
    : t('files.menuForItems', { n: items.length }, items.length)
})

function runMenu(action: string) {
  const items = selection.value
  switch (action) {
    // Retry is what Open does for a failed parse, named in its menu so it can be found.
    case 'open':
    case 'retry':
      return items[0] && openEntry(items[0].key)
    case 'rename':
      return items[0] && startRename(items[0].key)
    case 'reconvert':
    case 'retryReconvert':
    case 'dismissReconvert': {
      const document = items[0] && documents.value.find((row) => row.id === items[0]!.id)
      if (!document) {
        return
      }
      if (action === 'reconvert') {
        reconverting.value = document
      } else if (action === 'retryReconvert') {
        retrying.value = reconvertRetry(document)
      } else {
        return run(() => dismissReconvert(document.id))
      }
      return
    }
    case 'copy':
      return copySelection()
    case 'duplicate':
      return duplicateSelection()
    case 'trash':
      return trashItems(items)
    case 'upload':
      return pickFiles(currentFolder.value)
    case 'newDocument':
      return createEntry('file')
    case 'newFolder':
      return createEntry('folder')
    case 'paste':
      return pasteHere()
    case 'moveHere':
      return moveHere()
    case 'putBack':
      return putBackSelection()
    case 'deleteNow':
      return askDelete()
    case 'empty':
      return askEmpty()
  }
}

// --- dragging --------------------------------------------------------------

/**
 * Drag state, and the one rule that keeps it honest: `dragover` decides everything.
 *
 * It fires continuously on exactly one innermost element and bubbles to the window, so the
 * handler nearest the pointer claims the event and the window clears whatever went
 * unclaimed (lib/dnd.ts). `dragleave` is deliberately not used anywhere here — it fires for
 * every child element a pointer crosses, so a row made of an icon and a name unlights
 * itself halfway across, and browsers disagree about what `relatedTarget` holds on a drag.
 */

/** The rows a drag inside the page carries: the selection when it started. Read only while
 *  the drag carries `ROWS_DRAG_TYPE`, so a drag from elsewhere never finds stale rows here. */
let draggedRows: Entry[] = []
/** The folder a drag is over — the one drawn as the target — or the Trash's tab. */
const dropTarget = ref<FolderId | 'trash' | undefined>(undefined)
/** A drag carrying files is somewhere over the window. */
const fileDrag = ref(false)
/** …and it is over the list, so the overlay names where a drop would land. */
const fileOverList = ref(false)
/** Rows from another folder are over the list, which they would move into. */
const rowsOverList = ref(false)

/** How long the page waits for the next drag event before assuming the drag is gone. */
const DRAG_IDLE_MS = 700
let dragWatchdog: ReturnType<typeof setTimeout> | undefined

/**
 * How long an unlit frame has to last before the target really goes out.
 *
 * A pointer crossing the rule between two rows lands one `dragover` on the table that holds
 * them, and the browser dispatches the odd frame on a parent during a fast move. Both are
 * single events between two that claim the same row, and unlighting on them makes the target
 * blink all the way down the list. So the clear is scheduled, and the next claim cancels it.
 */
const TARGET_CLEAR_MS = 80
let clearTargetTimer: ReturnType<typeof setTimeout> | undefined
let springTimer: ReturnType<typeof setTimeout> | undefined

/** The rows this drag carries, when it is a drag of this page's rows. */
function rowsOf(event: DragEvent): Entry[] {
  return [...(event.dataTransfer?.types ?? [])].includes(ROWS_DRAG_TYPE) ? draggedRows : []
}

/** ⌥ as it drops copies, as in Finder — Ctrl off Apple platforms, as in Explorer. */
function copyDrop(event: DragEvent): boolean {
  return apple ? event.altKey : event.ctrlKey
}

/**
 * Whether these rows can land in `target`. A copy may land beside its original; a move may
 * not, and no folder goes into its own subtree either way. Takes the rows rather than reading
 * them from state, because the drop handler has to ask *after* it has let go of the drag.
 */
function canDropInto(items: Entry[], target: FolderId, copy: boolean): boolean {
  if (!items.length) {
    return false
  }
  return items.every((item) => {
    if (item.kind === 'folder' && target !== null && subtreeIds(folders.value, item.id).has(target)) {
      return false
    }
    return copy || liveRow(item)?.parent !== target
  })
}

function startDrag(event: DragEvent, key: string) {
  if (!selected.value.has(key)) {
    selectOnly(key)
  }
  draggedRows = selection.value
  if (event.dataTransfer) {
    // Firefox starts no drag at all unless a payload is set; the names are what a drop
    // outside the page gets.
    event.dataTransfer.setData('text/plain', draggedRows.map((item) => item.name).join('\n'))
    event.dataTransfer.setData(ROWS_DRAG_TYPE, '')
    event.dataTransfer.effectAllowed = 'copyMove'
  }
}

function holdTarget() {
  clearTimeout(clearTargetTimer)
  clearTargetTimer = undefined
}

function releaseTarget() {
  if (clearTargetTimer !== undefined || dropTarget.value === undefined) {
    return
  }
  clearTargetTimer = setTimeout(() => {
    dropTarget.value = undefined
    clearTargetTimer = undefined
  }, TARGET_CLEAR_MS)
}

/**
 * Folders spring open: a drag resting on a folder row or a crumb opens that folder under it,
 * so a drag goes down any number of levels (docs/web.md § Files, *Moving*). Leaving the
 * target — another target, or none — starts the wait again.
 */
watch(dropTarget, (target) => {
  clearTimeout(springTimer)
  if (target === undefined || target === 'trash' || target === currentFolder.value) {
    return
  }
  springTimer = setTimeout(() => {
    if (dropTarget.value === target) {
      dropTarget.value = undefined
      openFolder(target)
    }
  }, SPRING_MS)
})

/** Everything a finished or abandoned drag leaves behind. */
function endDrag() {
  clearTimeout(dragWatchdog)
  dragWatchdog = undefined
  clearTimeout(springTimer)
  holdTarget()
  draggedRows = []
  dropTarget.value = undefined
  fileDrag.value = false
  fileOverList.value = false
  rowsOverList.value = false
}

/**
 * A drag from the desktop ends with no event this page can rely on — it can be carried out
 * of the window, and `dragend` fires on the source, which is another application. So the
 * flags a drag sets expire on their own if drag events stop arriving. `draggedRows` is not
 * touched: a drag of the page's rows does get its `dragend`, and dropping it after a trip
 * outside the window has to still work.
 */
function dragGoneIdle() {
  fileDrag.value = false
  fileOverList.value = false
  rowsOverList.value = false
  holdTarget()
  dropTarget.value = undefined
}

function onWindowDragOver(event: DragEvent) {
  const claim = dragClaim(event)
  const files = carriesFiles(event.dataTransfer)

  fileDrag.value = files
  fileOverList.value = files && claim === 'list'
  rowsOverList.value = !files && claim === 'list'
  if (claim === 'target') {
    holdTarget()
  } else {
    releaseTarget()
  }
  if (claim === undefined && files) {
    // Nothing on this page wants it. Swallowing it anyway is the point: a file dropped on
    // a page that ignores it becomes a navigation, and the browser would replace the app
    // with the PDF — in the middle of an upload, with a queue still in memory.
    event.preventDefault()
    setDropEffect(event, 'none')
  }

  clearTimeout(dragWatchdog)
  dragWatchdog = setTimeout(dragGoneIdle, DRAG_IDLE_MS)
}

function onWindowDrop(event: DragEvent) {
  // Preventing the default on `dragover` alone is not enough; the drop itself navigates.
  if (!event.defaultPrevented && carriesFiles(event.dataTransfer)) {
    event.preventDefault()
  }
  endDrag()
}

onMounted(() => {
  window.addEventListener('dragover', onWindowDragOver)
  window.addEventListener('drop', onWindowDrop)
  // The row that started the drag can be replaced by a poll, or left behind by a folder
  // springing open, while the drag is in the air; its own `dragend` then never arrives.
  window.addEventListener('dragend', endDrag)
})

onBeforeUnmount(() => {
  window.removeEventListener('dragover', onWindowDragOver)
  window.removeEventListener('drop', onWindowDrop)
  window.removeEventListener('dragend', endDrag)
  clearTimeout(dragWatchdog)
  clearTimeout(clearTargetTimer)
  clearTimeout(springTimer)
})

/** Rows dropped on a folder: moved into it, or copied with the copy key held. */
function dropRows(items: Entry[], target: FolderId, copy: boolean) {
  if (!canDropInto(items, target, copy)) {
    return
  }
  void (copy ? copyInto(items, target) : moveInto(items, target))
}

/**
 * A drop on a folder row or a breadcrumb. Files are not theirs to take: the overlay names
 * the open folder while a file drag is up, and a row quietly landing them somewhere else
 * would make the page lie about where a document went. Letting the event through puts them
 * where the overlay said.
 */
function onTargetDrop(target: FolderId, event: DragEvent) {
  if (carriesFiles(event.dataTransfer)) {
    return
  }
  event.preventDefault()
  event.stopPropagation()
  const items = rowsOf(event)
  const copy = copyDrop(event)
  endDrag()
  dropRows(items, target, copy)
}

function onTargetDragOver(event: DragEvent, target: FolderId) {
  const copy = copyDrop(event)
  if (carriesFiles(event.dataTransfer) || !canDropInto(rowsOf(event), target, copy)) {
    return
  }
  event.preventDefault()
  claimDrag(event, 'target')
  setDropEffect(event, copy ? 'copy' : 'move')
  holdTarget()
  dropTarget.value = target
}

/** The Trash's tab takes the page's rows: a drop there moves them to the Trash. */
function onTrashTabDragOver(event: DragEvent) {
  if (view.value !== 'files' || !rowsOf(event).length) {
    return
  }
  event.preventDefault()
  claimDrag(event, 'target')
  setDropEffect(event, 'move')
  holdTarget()
  dropTarget.value = 'trash'
}

function onTrashTabDrop(event: DragEvent) {
  const items = rowsOf(event)
  if (view.value !== 'files' || !items.length) {
    return
  }
  event.preventDefault()
  endDrag()
  void trashItems(items)
}

/** The list itself: where files from the desktop land, and rows a folder sprang open for. */
function onListDragOver(event: DragEvent) {
  if (view.value !== 'files') {
    return
  }
  if (carriesFiles(event.dataTransfer)) {
    event.preventDefault()
    claimDrag(event, 'list')
    setDropEffect(event, 'copy')
    return
  }
  const copy = copyDrop(event)
  if (dragClaim(event) || !canDropInto(rowsOf(event), currentFolder.value, copy)) {
    return
  }
  event.preventDefault()
  claimDrag(event, 'list')
  setDropEffect(event, copy ? 'copy' : 'move')
}

function onListDrop(event: DragEvent) {
  const files = droppedFiles(event)
  const items = rowsOf(event)
  const copy = copyDrop(event)
  endDrag()
  if (view.value !== 'files') {
    return
  }
  if (files.length) {
    addToQueue(files, currentFolder.value)
    return
  }
  dropRows(items, currentFolder.value, copy)
}

/** What every selectable row carries, in either tab: its selection, its gestures. */
function entryAttrs(key: string): Record<string, unknown> {
  const isSelected = selected.value.has(key)
  return {
    id: rowId(key),
    'aria-selected': isSelected ? 'true' : 'false',
    onClick: (event: MouseEvent) => onRowClick(event, key),
    onDblclick: (event: MouseEvent) => onRowDblclick(event, key),
    onContextmenu: (event: MouseEvent) => {
      event.preventDefault()
      // The list under the row is a target too; the innermost thing under the pointer wins.
      event.stopPropagation()
      openRowMenu(key, { x: event.clientX, y: event.clientY })
    },
    ...press.on((at) => openRowMenu(key, at)),
  }
}

function rowAttrs(row: Row): Record<string, unknown> {
  if (row.kind === 'upload') {
    // A file still going up has nothing to act on. The gesture falls through to the list,
    // whose menu is the right one for it anyway.
    return {}
  }
  const editing = renaming.value?.key === row.key
  const base: Record<string, unknown> = {
    ...entryAttrs(row.key),
    // A field being typed in is not a handle: its text is selected by dragging.
    draggable: editing ? 'false' : 'true',
    onDragstart: (event: DragEvent) => startDrag(event, row.key),
    onDragend: endDrag,
  }
  const lit = { selected: selected.value.has(row.key), editing }
  if (row.kind === 'file') {
    return { ...base, class: { ...lit, file: true, revealed: revealed.value === row.document.id } }
  }
  return {
    ...base,
    class: { ...lit, 'drop-target': dropTarget.value === row.folder.id },
    onDragover: (event: DragEvent) => onTargetDragOver(event, row.folder.id),
    onDrop: (event: DragEvent) => onTargetDrop(row.folder.id, event),
  }
}

function trashRowAttrs(row: TrashRow): Record<string, unknown> {
  return { ...entryAttrs(row.key), class: { selected: selected.value.has(row.key) } }
}

/** The row the keys are on, for the list's `aria-activedescendant`. */
const activeRow = computed(() =>
  cursor.value !== null && entryKeys.value.includes(cursor.value) ? rowId(cursor.value) : undefined,
)

// A reload onto a new build would drop files still waiting to upload, a name being typed or
// a reader's place — so it waits for all three to be over (docs/web.md § Rules).
useBuildHold(() => queue.value.length > 0 || renaming.value !== null || openDocument.value !== null)
</script>

<template>
  <UiScreen full>
    <UiRegion>
      <input
        ref="fileInput"
        class="sr-only"
        type="file"
        multiple
        :accept="accept"
        @change="onPicked"
      >
      <div ref="region" class="library" @pointerdown.capture="onPointerDown">
        <div class="tabs" role="tablist" :aria-label="t('files.tabs')">
          <button
            id="files-tab-files"
            type="button"
            role="tab"
            class="tab"
            :class="{ active: view === 'files' }"
            :aria-selected="view === 'files'"
            aria-controls="files-panel"
            :tabindex="view === 'files' ? 0 : -1"
            @click="showView('files')"
            @keydown="onTabKeydown"
          >
            <UiIcon name="files" />
            <span>{{ t('files.tabFiles') }}</span>
          </button>
          <!-- Also a drop target: rows dragged here go to the Trash. -->
          <button
            id="files-tab-trash"
            type="button"
            role="tab"
            class="tab"
            :class="{ active: view === 'trash', target: dropTarget === 'trash' }"
            :aria-selected="view === 'trash'"
            aria-controls="files-panel"
            :tabindex="view === 'trash' ? 0 : -1"
            @click="showView('trash')"
            @keydown="onTabKeydown"
            @dragover="onTrashTabDragOver"
            @drop="onTrashTabDrop"
          >
            <UiIcon name="trash" />
            <span>{{ t('files.tabTrash') }}</span>
            <span v-if="trashTotal" class="tab-count">{{ trashTotal }}</span>
          </button>
        </div>

        <!-- The whole data region is a drop target for files from the desktop; the overlay
             says which folder they would land in, and nothing else.

             It is also the open folder's own menu and the selection's keyboard home — which
             is why it takes a tab stop: with no buttons left on the screen, this is where a
             keyboard reaches Upload. -->
        <div
          id="files-panel"
          ref="listEl"
          class="contents"
          :class="{ 'rows-target': rowsOverList }"
          role="tabpanel"
          :aria-labelledby="`files-tab-${view}`"
          tabindex="0"
          :aria-activedescendant="activeRow"
          v-bind="press.on((at) => openListMenu(at))"
          @click="onListClick"
          @dragover="onListDragOver"
          @drop.prevent="onListDrop"
          @contextmenu.prevent="openListMenu({ x: $event.clientX, y: $event.clientY })"
          @keydown="onListKeydown"
        >
          <UiPanel>
            <template #title>
              <!-- Each crumb is also a drop target: dropping a row on the folder you came
                   from is how a file manager moves something back up a level. -->
              <h2 v-if="view === 'files'" class="crumbs">
                <button
                  type="button"
                  class="crumb"
                  :class="{ current: !crumbs.length, target: dropTarget === null }"
                  :aria-current="!crumbs.length ? 'page' : undefined"
                  @click="openCrumb(null)"
                  @dragover="onTargetDragOver($event, null)"
                  @drop="onTargetDrop(null, $event)"
                >
                  {{ t('files.root') }}
                </button>
                <template v-for="(crumb, index) in crumbs" :key="crumb.id">
                  <UiIcon class="crumb-sep" name="chevron-right" />
                  <button
                    type="button"
                    class="crumb"
                    :class="{ current: index === crumbs.length - 1, target: dropTarget === crumb.id }"
                    :aria-current="index === crumbs.length - 1 ? 'page' : undefined"
                    @click="openCrumb(crumb.id)"
                    @dragover="onTargetDragOver($event, crumb.id)"
                    @drop="onTargetDrop(crumb.id, $event)"
                  >
                    {{ crumb.name }}
                  </button>
                </template>
              </h2>
              <h2 v-else class="crumbs">
                <span class="crumb current">{{ t('files.tabTrash') }}</span>
              </h2>
            </template>

            <template #meta>
              <template v-if="view === 'files'">
                <span v-if="data">
                  {{
                    selection.length > 1
                      ? t('files.selectedCount', { selected: selection.length, n: rows.length })
                      : t('files.itemCount', { n: rows.length }, rows.length)
                  }}
                </span>
                <UiButton
                  variant="ghost"
                  size="xs"
                  icon-only
                  :label="t('common.refresh')"
                  :loading="pending"
                  @click="refresh"
                >
                  <template #icon><UiIcon name="refresh" /></template>
                </UiButton>
              </template>
              <template v-else>
                <span v-if="data">
                  {{
                    selection.length > 1
                      ? t('files.selectedCount', { selected: selection.length, n: trashTotal })
                      : t('files.itemCount', { n: trashTotal }, trashTotal)
                  }}
                </span>
                <UiButton variant="danger" size="xs" :disabled="!trashTotal || busy" @click="askEmpty">
                  {{ t('files.emptyTrash') }}
                </UiButton>
              </template>
            </template>

            <UiBanner v-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>
            <UiBanner v-if="mutationError" class="state" tone="error">
              {{ mutationError }}
              <template #actions>
                <UiButton variant="ghost" size="xs" @click="mutationError = null">
                  {{ t('common.close') }}
                </UiButton>
              </template>
            </UiBanner>

            <!-- The two deletes nothing can undo ask here, not in a dialog: nothing else on
                 the page is held up while the question stands. -->
            <UiBanner v-if="view === 'trash' && confirming" class="state confirm" tone="error">
              {{ confirmText }}
              <template #actions>
                <UiButton ref="confirmCancel" variant="ghost" size="xs" :disabled="busy" @click="cancelConfirm">
                  {{ t('common.cancel') }}
                </UiButton>
                <UiButton variant="danger" size="xs" :loading="busy" @click="confirmDelete">
                  {{ t('files.deleteForGood') }}
                </UiButton>
              </template>
            </UiBanner>

            <UiSkeleton v-if="!data && !errorMessage" class="state" :rows="6" />

            <template v-else-if="data && view === 'files'">
              <UiEmptyState
                v-if="!rows.length"
                :title="currentFolder === null ? t('files.empty') : t('files.emptyFolder')"
              />

              <UiDataTable
                v-else
                role="grid"
                aria-multiselectable="true"
                :columns="columns"
                :rows="rows"
                :row-key="(row) => row.key"
                :row-attrs="rowAttrs"
                :caption="t('files.headTitle')"
              >
                <template #cell-name="{ row }">
                  <span
                    v-if="row.kind === 'upload'"
                    class="entry pendingup"
                    :title="row.upload.name"
                  >
                    <UiSpinner />
                    <span class="entry-name">{{ row.upload.name }}</span>
                  </span>

                  <span
                    v-else
                    class="entry"
                    :class="{ failed: row.kind === 'file' && row.document.status === 'failed' }"
                    :title="row.kind === 'folder' ? row.folder.name : row.document.name"
                  >
                    <UiIcon
                      :name="row.kind === 'folder' ? 'folder' : row.document.kind === 'image' ? 'image' : 'file-text'"
                    />
                    <UiTextInput
                      v-if="renaming?.key === row.key"
                      ref="renameField"
                      v-model="renaming.value"
                      class="rename"
                      :aria-label="t('files.nameLabel')"
                      :maxlength="row.kind === 'folder' ? FOLDER_NAME_MAX : DOCUMENT_NAME_MAX"
                      :readonly="renaming.busy"
                      @keydown="onRenameKeydown"
                      @blur="onRenameBlur"
                      @click.stop
                      @dblclick.stop
                      @contextmenu.stop
                    />
                    <span v-else class="entry-name">
                      {{ row.kind === 'folder' ? row.folder.name : row.document.name }}
                    </span>
                  </span>
                </template>

                <!-- A markdown document has no parse: no status, pages or size to report. A
                     re-convert is what a parsed row says while it is on its way or failed:
                     the version the document has opens all the same. -->
                <template #cell-status="{ row }">
                  <span v-if="row.kind === 'file' && row.document.reconvert" class="state-cell">
                    <UiStatusDot
                      v-if="row.document.reconvert.status === 'failed'"
                      tone="danger"
                      :label="t('files.reconvertFailed')"
                    />
                    <template v-else>
                      <UiStatusDot
                        :tone="row.document.reconvert.status === 'queued' ? 'neutral' : 'info'"
                        :label="t(row.document.reconvert.checking ? 'files.checking' : 'files.reconverting')"
                      />
                      <UiUsageBar
                        v-if="row.document.reconvert.status === 'running'"
                        class="meter"
                        :share="parseProgress(row.document.reconvert)"
                        :label="
                          t('files.progress', {
                            done: row.document.reconvert.pages_done,
                            total: row.document.reconvert.page_count ?? 0,
                          })
                        "
                      />
                    </template>
                  </span>
                  <span v-else-if="row.kind === 'file' && row.document.status" class="state-cell">
                    <!-- A failed parse says what a retry would send: its pages without a result;
                         a running one past its last page, that it is being checked. -->
                    <UiStatusDot
                      :tone="STATUS_TONE[row.document.status]"
                      :label="
                        row.document.failed_pages
                          ? t('files.failedPages', { n: row.document.failed_pages }, row.document.failed_pages)
                          : row.document.checking
                            ? t('files.checking')
                            : t(`status.${row.document.status}`)
                      "
                    />
                    <UiUsageBar
                      v-if="row.document.status === 'running'"
                      class="meter"
                      :share="parseProgress(row.document)"
                      :label="
                        t('files.progress', {
                          done: row.document.pages_done,
                          total: row.document.page_count ?? 0,
                        })
                      "
                    />
                  </span>
                  <span v-else-if="row.kind === 'upload'" class="muted">
                    {{ row.upload.state === 'uploading' ? t('files.uploading') : t('files.waiting') }}
                  </span>
                </template>

                <template #cell-pages="{ row }">
                  <template v-if="row.kind === 'file'">
                    {{
                      row.document.page_count
                        ? `${row.document.pages_done}/${row.document.page_count}`
                        : row.document.pages_done || ''
                    }}
                  </template>
                </template>

                <template #cell-size="{ row }">
                  <span class="muted">
                    <template v-if="row.kind === 'file' && row.document.size_bytes !== null">
                      {{ formatBytes(row.document.size_bytes, locale) }}
                    </template>
                    <template v-else-if="row.kind === 'upload'">
                      {{ formatBytes(row.upload.size, locale) }}
                    </template>
                  </span>
                </template>

                <template #cell-added="{ row }">
                  <span class="muted">
                    <template v-if="row.kind === 'folder'">
                      {{ formatShortDateTime(row.folder.created_at, locale) }}
                    </template>
                    <template v-else-if="row.kind === 'file'">
                      {{ formatShortDateTime(row.document.created_at, locale) }}
                    </template>
                  </span>
                </template>
              </UiDataTable>
            </template>

            <template v-else-if="data">
              <UiEmptyState v-if="!trashRows.length" :title="t('files.trashEmpty')" />

              <UiDataTable
                v-else
                role="grid"
                aria-multiselectable="true"
                :columns="trashColumns"
                :rows="trashRows"
                :row-key="(row) => row.key"
                :row-attrs="trashRowAttrs"
                :caption="t('files.trashRegion')"
              >
                <template #cell-name="{ row }">
                  <span class="entry" :title="row.kind === 'folder' ? row.folder.name : row.document.name">
                    <UiIcon
                      :name="row.kind === 'folder' ? 'folder' : row.document.kind === 'image' ? 'image' : 'file-text'"
                    />
                    <span class="entry-name">{{ row.kind === 'folder' ? row.folder.name : row.document.name }}</span>
                  </span>
                </template>

                <template #cell-origin="{ row }">
                  <span
                    class="entry origin"
                    :title="originPath(row.kind === 'folder' ? row.folder.origin : row.document.origin)"
                  >
                    <UiIcon name="folder" />
                    <span class="entry-name">
                      {{ originName(row.kind === 'folder' ? row.folder.origin : row.document.origin) }}
                    </span>
                  </span>
                </template>

                <template #cell-size="{ row }">
                  <span class="muted">
                    <template v-if="row.kind === 'folder'">
                      {{ t('files.itemCount', { n: row.folder.items }, row.folder.items) }}
                    </template>
                    <template v-else-if="row.document.size_bytes !== null">
                      {{ formatBytes(row.document.size_bytes, locale) }}
                    </template>
                  </span>
                </template>

                <template #cell-trashed="{ row }">
                  <span class="muted">{{ formatShortDateTime(trashedAt(row), locale) }}</span>
                </template>
              </UiDataTable>
            </template>
          </UiPanel>

          <!-- Where the files would land. A destination, not an instruction. -->
          <div v-if="fileOverList" class="dropzone">
            <div class="dropzone-mark">
              <UiIcon name="folder-open" />
              <span>{{ crumbs.length ? crumbs[crumbs.length - 1]!.name : t('files.root') }}</span>
            </div>
          </div>

          <PaneFoot :hint="false" :back="null" :note="note" @undo="stepHistory('undo')" @hold="holdNote" />
          <p class="sr-only" role="status" aria-live="polite">{{ liveText }}</p>
        </div>
      </div>
    </UiRegion>

    <UiContextMenu
      v-if="menu"
      :at="menu.at"
      :items="menuItems"
      :label="menuLabel"
      :focus-first="menu.fromKey"
      @select="runMenu"
      @close="menu = null"
    />

    <RetryDialog
      v-if="retrying"
      :key="retrying.id"
      :document="retrying"
      @retried="onRetried"
      @close="retrying = null"
    />

    <ReconvertDialog
      v-if="reconverting"
      :key="reconverting.id"
      :document="reconverting"
      @started="onReconvertStarted"
      @close="reconverting = null"
    />

    <CreditsDialog
      v-if="creditsRefusal"
      :refusal="creditsRefusal"
      action="parse"
      @close="creditsRefusal = null"
    />

    <!-- Keyed per document, and per parse: reopening on another file, or a re-convert taken
         in, resets scroll and page state and reads the copy again. Only a finished parse
         opens, so `pending` is its one read of the result. -->
    <ResultViewer
      v-if="openDocument"
      :key="`${openDocument.id}:${openDocument.job_id}`"
      :title="openDocument.name"
      :error="openDocument.error"
      :result="results.get(openDocument.id) ?? null"
      :pending="!results.has(openDocument.id) && loadingResults.has(openDocument.id)"
      :error-message="resultErrors.get(openDocument.id) ?? null"
      :figure-base="openDocument.kind === 'markdown' ? null : `/api/library/documents/${openDocument.id}/figures`"
      :image-base="`/api/library/documents/${openDocument.id}/images`"
      :document-id="openDocument.id"
      :markdown-document="openDocument.kind === 'markdown'"
      :initial-block="linkedBlock?.id === openDocument.id ? linkedBlock.anchor : null"
      :reconvert-note="reconvertNote"
      @close="openDocument = null"
    />
  </UiScreen>
</template>

<style scoped>
/* The tabs over the list, then the list: the list's row is the one that scrolls. */
.library {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
}

/* --- Tabs ----------------------------------------------------------------- */

/* Two words over a hairline, the open one inked and underlined: a tab is a place, not a
   control to commit, so the accent stays out of it. */
.tabs {
  display: flex;
  gap: var(--space-6);
  padding: var(--space-6) var(--panel-x) 0;
  border-bottom: 1px solid var(--line-soft);
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height);
  margin-bottom: -1px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    color var(--duration-fast) var(--ease),
    box-shadow var(--duration-fast) var(--ease);
}

.tab :deep(svg) {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}

.tab:hover {
  color: var(--ink);
}

.tab.active {
  color: var(--ink);
  font-weight: var(--weight-medium);
  box-shadow: inset 0 -2px 0 var(--ink);
}

/* Lit only while rows are dragged over it — a drop target, so the accent, as a crumb's. */
.tab.target {
  color: var(--accent);
  box-shadow: inset 0 -2px 0 var(--accent);
}

.tab-count {
  color: var(--faint);
  font-weight: var(--weight-normal);
  font-variant-numeric: tabular-nums;
}

/* The drop surface: it has to be the box the panel fills, so the overlay can cover exactly
   the list and nothing else. It is also the list's menu target and a tab stop, so it takes
   the app's focus ring rather than the browser's. */
.contents {
  position: relative;
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
}

.contents:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* Rows from another folder over the list they would move into: its edge, as a drop target's. */
.contents.rows-target {
  box-shadow: inset 0 0 0 1px var(--accent);
}

.state {
  margin-top: var(--space-4);
}

/* --- Breadcrumb ----------------------------------------------------------- */

/* The panel's heading, made of the places you can go back to. Same serif and size as any
   other panel title — it is a title that happens to be clickable. */
.crumbs {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  min-width: 0;
  overflow: hidden;
  font-size: var(--display-sm);
  letter-spacing: normal;
}

.crumb {
  max-width: 22ch;
  padding: 0;
  border: none;
  background: none;
  color: var(--muted);
  font: inherit;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease);
}

.crumb:hover {
  color: var(--accent);
}

.crumb.current {
  color: var(--ink);
}

span.crumb {
  cursor: default;
}

/* Lit only while a drag is over it. Underlined rather than boxed: a crumb is a word in a
   heading, and a box around one word would jump the line. */
.crumb.target {
  color: var(--accent);
  box-shadow: inset 0 -2px 0 var(--accent);
}

.crumb-sep {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  color: var(--faint);
}

/* --- Rows ----------------------------------------------------------------- */

/* Not `cell-block`: that is `UiDataTable`'s ellipsis helper for a cell whose content is one
   block, and its `display: block` would flatten this row of icon-and-name. The clipping it
   provides is done here instead, on the name itself. */
.entry {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  max-width: 100%;
  min-width: 0;
  color: var(--ink);
}

.entry.pendingup {
  color: var(--muted);
}

.entry.origin {
  color: var(--muted);
}

.entry :deep(svg) {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--faint);
}

.entry-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.entry.failed .entry-name {
  color: var(--danger);
}

/* The name as a field in its row, at the row's own height so naming does not move the list:
   the field is 8px taller than the row's 20px line, and the margins give those 8px back. */
.entry .rename {
  height: calc(var(--control-height-xs) + 2px);
  max-width: 40ch;
  margin-block: -4px;
  padding: 0 var(--space-2);
}

.state-cell {
  display: inline-flex;
  align-items: center;
  gap: var(--space-3);
}

/* Short on purpose: it sits beside the word it qualifies, not instead of it. */
.meter {
  width: 44px;
  flex-shrink: 0;
}

.muted {
  color: var(--muted);
}

/* A row is held, not selected as text: a long press that starts a text selection cannot
   become a menu, and on iOS it raises the callout instead. Same reason a file manager's rows
   do not select their text either. */
.contents :deep(tbody tr) {
  user-select: none;
  -webkit-touch-callout: none;
  cursor: default;
}

/* The selected rows: the sidebar's current-row fill, never the accent — selecting commits
   nothing (docs/web.md § Files). */
.contents :deep(tr.selected) td {
  background: var(--rail-active);
}

/* The row `?document=` pointed at: lit on arrival, then fading back to an ordinary row. */
.contents :deep(tr.file) td {
  transition: background var(--duration-slow) var(--ease);
}

.contents :deep(tr.revealed) td {
  background: var(--rail-active);
}

/* A folder row lit up as a drop target. Inset, so accepting a drop does not move the row. */
.contents :deep(tr.drop-target) td {
  background: var(--rail-active);
  box-shadow:
    inset 0 1px 0 var(--accent),
    inset 0 -1px 0 var(--accent);
}

/* --- Dropping files from the desktop -------------------------------------- */

.dropzone {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: grid;
  place-items: center;
  /* Transparent to the pointer: the drop is handled by the region under it, and a solid
     overlay would swallow the `dragover` that keeps the drag alive. */
  pointer-events: none;
  background: var(--paper);
  border: 1px dashed var(--accent);
}

.dropzone-mark {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  color: var(--ink);
  font-size: var(--display-xs);
  font-family: var(--font-display);
}

.dropzone-mark :deep(svg) {
  width: 28px;
  height: 28px;
  color: var(--accent);
}

@media (max-width: 900px) {
  /* The content region scrolls as a whole below the breakpoint, so the list must not be a
     bounded grid row inside it. */
  .library,
  .contents {
    display: block;
  }
}
</style>
