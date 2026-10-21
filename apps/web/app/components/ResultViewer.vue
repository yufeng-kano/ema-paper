<script lang="ts">
// Both script blocks compile into one module, so the imports belong to the block that comes
// first — the whole component reads them, `<script setup>` included.
import type { ComponentPublicInstance } from 'vue'
import type { ToolbarRect, ToolbarSkill } from '~/components/ViewerToolbar.vue'
import {
  ApiRequestError,
  getDocumentContent,
  listSkills,
  putDocumentContent,
  putDocumentNotes,
  uploadDocumentImage,
  type JobResult,
  type Skill,
} from '~/lib/api'
import {
  blockKey,
  blockName,
  blocksInRanges,
  changedBlocks,
  followBlocks,
  followFolds,
  foldsOf,
  foldsOver,
  moveTarget,
  readFoldMemory,
  rememberFolds,
  rowsOf,
  translatable,
  UndoHistory,
  type FoldMemory,
  type NameText,
  type UndoFile,
} from '~/lib/blocks'
import { formatBytes, isImageFile, type AskRequest } from '~/lib/composer'
import { cellGroupsToMarkdown, nodesToMarkdown, selectionText, type OrphanListContext } from '~/lib/copy'
import { ANSWER_DRAG_TYPE, BLOCK_DRAG_TYPE, carriesFiles, claimDrag, dragClaim, droppedFiles, setDropEffect } from '~/lib/dnd'
import {
  appendBlocks,
  appendExcerpt,
  droppedBlocksMarkdown,
  excerptMarkdown,
  excerptMarkers,
  excerptPassage,
  findPassage,
  insertNoteBlocks,
  linkResolves,
  locatePassage,
  matchExcerpts,
  passageText,
  rewriteBlockLinks,
  sectionOf,
  type ExcerptMatch,
} from '~/lib/excerpts'
import {
  appendBlockSource,
  blockAnchor,
  blockPlain,
  decodeBlockAnchor,
  documentOutline,
  findHeading,
  findPlace,
  imageLine,
  insertBlocks,
  moveBlocks,
  parseResultMarkdown,
  placeNear,
  placedBlocks,
  replaceBlockSource,
  resolveAnchor,
  sectionSource,
  serializeDocument,
  type BlockAt,
  type OutlineEntry,
  type PlaceNear,
  type PlacedBlock,
  type ResultBlock,
} from '~/lib/markdown'
import type { MenuFact, MenuItem, Point } from '~/lib/menu'
import type { Caret, LeaveReason, RichOptions, SectionLinkState, SectionLinkTarget, TextPoint } from '~/lib/rich-extensions'
import { blockIndexAt, blockKindName, captionLine, convertBlock, type BlockKindName } from '~/lib/richdoc'
import { SLASH_CONVERSIONS, skillIcon, type SlashSkill } from '~/lib/slash'

/** Where the viewer's layout is kept in the browser between visits. */
const LAYOUT_KEY = 'sightread.viewer.layout'

type Layout = { notes: boolean; agent: boolean; notesWidth: number; agentWidth: number }

function storedLayout(): Partial<Layout> {
  if (!import.meta.client) {
    return {}
  }
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? '{}')
    return stored && typeof stored === 'object' ? stored : {}
  } catch {
    return {}
  }
}

/** The side columns' widths, dragged by the rule on their left edge (docs/web.md § Column
 *  widths). */
const COLUMN_WIDTHS = {
  notes: { initial: 320, min: 240, max: 560 },
  agent: { initial: 360, min: 300, max: 640 },
} as const

function storedWidth(column: 'notes' | 'agent', width: unknown): number {
  const { initial, min, max } = COLUMN_WIDTHS[column]
  return typeof width === 'number' && Number.isFinite(width) ? Math.min(max, Math.max(min, width)) : initial
}

/**
 * Module scope, not component state: the notes column, the agent panel and the columns'
 * widths are reading preferences, so opening the next document keeps the choice you just made
 * instead of asking for it again — and so does the next visit, from the browser.
 */
const layout = storedLayout()
const notesOpen = ref(layout.notes === true)
const agentOpen = ref(layout.agent === true)
const columnWidth = {
  notes: ref<number>(storedWidth('notes', layout.notesWidth)),
  agent: ref<number>(storedWidth('agent', layout.agentWidth)),
}

/** The widths the reader last set, which is what is stored: a width the window clamped is
 *  not a width the reader chose. */
const chosenWidth = { notes: columnWidth.notes.value, agent: columnWidth.agent.value }

function saveLayout() {
  const next: Layout = {
    notes: notesOpen.value,
    agent: agentOpen.value,
    notesWidth: chosenWidth.notes,
    agentWidth: chosenWidth.agent,
  }
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(next))
  } catch {
    // Storage full or blocked: the layout still holds for as long as the page is loaded.
  }
}

watch([notesOpen, agentOpen], saveLayout)
/** The document pane never gets narrower than this to make room for a dragged column. */
const PANE_MIN_WIDTH = 360

/** The block kinds the rich editor opens in place; the rest have their own path. */
const RICH_KINDS = new Set<ResultBlock['kind']>(['p', 'h2', 'h3', 'list', 'table', 'math', 'code', 'quote'])
/** The kinds Backspace can join: text blocks, one into the one before. */
const JOINABLE = new Set<ResultBlock['kind']>(['p', 'h2', 'h3'])

/** How long after the last change a save is sent (docs/web.md § Editing). */
const SAVE_DEBOUNCE_MS = 1200

type SaveState = 'saved' | 'editing' | 'saving' | 'failed'

/** Where the collapsed headings are remembered in the browser: per document on Files, per job
 *  on History (docs/web.md § Result viewer, *Outline, collapse and the way back*). */
const FOLDS_KEY = 'sightread.viewer.folds'

/** How long the status note shows, and how long one carrying *Undo* waits for the reader
 *  (docs/web.md § Editing, *Undo and the status note*). */
const NOTE_MS = 1600
const NOTE_UNDO_MS = 8000

/** How long a keyboard selection rests before the selection toolbar shows. */
const TOOLBAR_SETTLE_MS = 300

/** A block drag's own type: what tells it from text or files dragged over the pane. */
/** How near the pane's top or bottom a drag scrolls it, and how far each step goes. */
const AUTOSCROLL_EDGE_PX = 48
const AUTOSCROLL_STEP_PX = 16

/** How long after the last change to the notes or the document the excerpts are matched
 *  again — markers follow typing, a keystroke behind. */
const EXCERPTS_DEBOUNCE_MS = 300

/** The gutter the outline needs left of the column: its ticks and about 40px past a heading's
 *  own controls — on Files the chevron and the handle, on History the chevron. */
const OUTLINE_GUTTER_FILES_PX = 128
const OUTLINE_GUTTER_HISTORY_PX = 100
/** Where the outline's ticks start under the pane's top (`ViewerOutline`). */
const OUTLINE_TOP_PX = 64

/** How far under the pane's top a located heading lands: the line above it shows. */
const LOCATE_MARGIN_PX = 48
/** How long a located heading or passage keeps its wash. */
const FLASH_MS = 700
/** How long the wash then takes to fade — `--duration-slow`, which the style fades it over. */
const FADE_MS = 240

/** A press on a handle that closed that handle's own menu: its click must not open it again. */
const REOPEN_GUARD_MS = 300

/** What *Turn into…* offers, in the submenu's order (docs/web.md § Editing). */
const TURN_KINDS: readonly BlockKindName[] = ['paragraph', 'heading', 'subheading', 'bulleted', 'numbered', 'quote', 'formula', 'code']

/** A run of visible rows, by index into the rows. */
type RowRange = { from: number; to: number }

/** What an open rich editor exposes to the viewer. */
interface EditorControls {
  focus: (where?: 'start' | 'end') => void
  wash: (first: number, count: number) => void
  focusBlock: (index: number) => void
  refreshLinks: () => void
  selectedMarkdown: () => { markdown: string; before: string } | null
}

/** A rendered block's text slots, in reading order: the textblocks the editor will hold, and a
 *  display formula or raw block as one slot of its own. `element` is null for a table cell a
 *  short row lacks, which the editor fills in. */
type Slot = { element: Element | null; atom: boolean }
</script>

<script setup lang="ts">
/**
 * A finished parse, as a document rather than as a blob of JSON — and, on Files, the
 * document's editable copy with its notes and the agent's thread beside it.
 *
 * The dialog is the reason a filename is a link on both screens that list parses — Files
 * and History: what a reader actually wants to check is whether the markdown came back
 * right, and a `<pre>` of the whole envelope makes that a reading exercise. The JSON is
 * still one tab away — it is the contract, and nothing here replaces it.
 *
 * It takes a title and a job error rather than a job row, because the two callers hold
 * different shapes of the same thing: a `JobSummary` on History, a `LibraryDocument` on
 * Files. Neither is this component's business — it renders a result. Files also hands it
 * the document's id, which is what turns the viewer into an editor: the markdown tab then
 * shows the workspace copy (`GET …/content`, docs/web.md § Editing) rather than the parse
 * result, every block opens as its source on a click, saves are automatic, and the notes
 * and agent columns become available. History, with no document, keeps the read-only
 * viewer with the reading aids alone.
 *
 * The markdown tab is one continuous scroll, and **the document carries no page labels and
 * the viewer no page navigation**: what a reader checks is the transcription, and which page
 * a passage came from is the pipeline's fact rather than the page's content. The grouping is
 * real all the same — a figure's crop is addressed by the page it came from — but a reader
 * finds a passage with Find, the outline and the way back (docs/web.md § Result viewer).
 *
 * The header is the filename and its controls. What the parse was — model, pages, bbox
 * format — and the JSON view sit in the More menu: read once, not every time the document
 * is open.
 *
 * The find bar, the outline, the selection toolbar and the pieces at the pane's foot **float
 * over** the document: opening one leaves every line of text where it was. The notes and the
 * agent columns take their width from the document instead, so the column stays centered in
 * whatever room they leave.
 *
 * Every rule here that can be pure is in `app/lib` — the blocks' names, folds, rows, moves,
 * undo (`blocks.ts`), excerpts (`excerpts.ts`), conversions (`richdoc.ts`) — and this
 * component measures, draws and routes events (docs/web.md § Result viewer).
 */
const props = withDefaults(
  defineProps<{
    /** The document's name — the dialog's heading and its accessible name. */
    title: string
    result: JobResult | null
    /** Why the parse itself failed, from the job. Outranks everything below it. */
    error?: string | null
    /** The result is still in flight, or failed — the body says so instead of the document. */
    pending?: boolean
    /** Why fetching the result failed, from the client. */
    errorMessage?: string | null
    /** Where this caller's figure crops live (`…/figures`); null hides images entirely. */
    figureBase?: string | null
    /** Where the document's inserted images live (`…/images`), on Files: what lets a picture
     *  be drawn and inserted (docs/web.md § Editing, *Images*). */
    imageBase?: string | null
    /** The library document, when there is one: what makes the viewer editable. */
    documentId?: number | null
    /** History's job, when there is no document: whose collapsed sections are remembered. */
    jobId?: string | null
    /** A block link's anchor (`/files?document=…&block=…`): followed once the copy loads. */
    initialBlock?: string | null
    /** A markdown document: no parse result to wait for, its copy is the document
     *  (docs/web.md § Result viewer). */
    markdownDocument?: boolean
  }>(),
  {
    error: null,
    errorMessage: null,
    figureBase: null,
    imageBase: null,
    documentId: null,
    jobId: null,
    initialBlock: null,
    markdownDocument: false,
  },
)

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const { resolve } = useApiError()
const localePath = useLocalePath()
const { apple, label: keyLabel, matches } = useShortcuts()
const auth = useAuth()

const tab = ref<'markdown' | 'json'>('markdown')

/** A running job's snapshot: pages keep arriving under this one (docs/api.md § Partial). */
const partial = computed(() => props.result?.meta.partial === true)

// --- the editable copy -------------------------------------------------------------

/** The working markdown — the workspace copy once it has loaded, edited in place. */
const markdown = ref('')
const revision = ref('')
const notes = ref('')
const notesRevision = ref('')
const contentLoaded = ref(false)
const contentPending = ref(false)
const contentError = ref<string | null>(null)
/** The one-line notice a 409 leaves: the document moved under the reader. */
const notice = ref<string | null>(null)

/** The viewer edits when it has a document and that document's copy has been read. */
const editable = computed(() => props.documentId !== null && contentLoaded.value)

/** The copy exists only once the parse is final: a running job has nothing to edit yet. A
 *  markdown document has no parse: its copy is all there is. */
const contentWanted = computed(
  () =>
    props.documentId !== null &&
    (props.markdownDocument || (props.result !== null && !partial.value && !props.error)),
)

let contentSeq = 0

/** The block actions' own undo, over the document and the notes alike, gone when the viewer
 *  closes (docs/web.md § Editing, *Undo and the status note*). */
const undo = new UndoHistory()

/** Which unsaved work a reload may overwrite: nothing, one file, or both. */
type Replace = false | 'markdown' | 'notes' | true

/**
 * Reads the copy. A reload (a 409, an agent edit) replaces the working text wholesale —
 * what is on disk is the truth, and the reader is told. A load while an edit is still
 * unsaved keeps the edit: its save will 409 and that reload will take over.
 *
 * Text the reload brings is a change no block action made: every undo step on that file is
 * stale, and whatever was open over the blocks — the block menu, the toolbar, a drag — named
 * blocks that may have moved (docs/web.md § Editing).
 */
async function loadContent(replaceDirty: Replace = false) {
  const id = props.documentId
  if (id === null) {
    return
  }
  const seq = ++contentSeq
  contentPending.value = true
  contentError.value = null
  try {
    const content = await getDocumentContent(id)
    if (seq !== contentSeq) {
      return
    }
    // An open block editor whose text differs from its block is unsaved work too: it
    // keeps the revision it was opened against, so its save meets the 409 that reloads.
    const dirtyEditor = editing.value !== null && draft.value !== openBlockSource()
    const takeMarkdown = replaceDirty === true || replaceDirty === 'markdown'
    const takeNotes = replaceDirty === true || replaceDirty === 'notes'
    // `failed` is unsaved work too: the text is still the reader's and its next save
    // retries it, so only an explicit replacement may overwrite it.
    if (takeMarkdown || (saveState.value === 'saved' && !dirtyEditor)) {
      reloadAfterSave = false
      // A block editor open across a forced replacement is closed, its draft dropped:
      // the block it was editing may no longer sit at those indices, and a commit
      // against the fresh text would land on whatever does now.
      if (editing.value !== null) {
        editing.value = null
        span.value = 1
        joined.value = false
        draft.value = ''
      }
      if (content.markdown !== markdown.value) {
        undo.clear('document')
        closeOverBlocks()
      }
      markdown.value = content.markdown
      revision.value = content.revision
      saveState.value = 'saved'
    } else if (saveState.value === 'saving') {
      // The save in flight was made against an older revision than what was just read:
      // its answer settles the viewer on a revision the disk has moved past, and nothing
      // after it reloads. So this reload is repeated once the save has settled.
      reloadAfterSave = true
    }
    if (takeNotes || notesState.value === 'saved') {
      reloadNotesAfterSave = false
      if (content.notes !== notes.value) {
        undo.clear('notes')
      }
      notes.value = content.notes
      notesRevision.value = content.notes_revision
      notesState.value = 'saved'
    } else if (notesState.value === 'saving') {
      reloadNotesAfterSave = true
    }
    contentLoaded.value = true
  } catch (error) {
    if (seq === contentSeq) {
      contentError.value = await resolve(error)
    }
  } finally {
    if (seq === contentSeq) {
      contentPending.value = false
    }
  }
}

const pages = computed(() => {
  if (editable.value) {
    return parseResultMarkdown(markdown.value)
  }
  return props.result ? parseResultMarkdown(props.result.markdown) : []
})

const outline = computed(() => documentOutline(pages.value))

/** The document in reading order, page markers crossed, and where each block sits in it. */
const placed = computed(() => placedBlocks(pages.value))
const placedIndex = computed(() => new Map(placed.value.map((entry, index) => [blockKey(entry), index])))
/** Each heading's outline entry, by its block's key. */
const headingAt = computed(() => new Map(outline.value.map((entry) => [blockKey(entry), entry])))

/** The blocks of each page with their keys, as the template draws them. */
const pageBlocks = computed(() =>
  pages.value.map((page, pageIndex) => page.blocks.map((block, index) => ({ block, key: blockKey({ page: pageIndex, index }) }))),
)

// --- saving ------------------------------------------------------------------------

const saveState = ref<SaveState>('saved')
const saveError = ref<string | null>(null)
let saveTimer: ReturnType<typeof setTimeout> | null = null
/** The save in flight, if any: one at a time. */
let saving: Promise<void> | null = null
/** A save asked for while one was in flight, sent once it settles. */
let saveAgain = false
/** How many saves met a 409: a sending ask goes only when none did while it waited. */
let contentConflicts = 0
/** A reload that arrived while a save was in flight, to be made again after it. */
let reloadAfterSave = false

function scheduleSave() {
  saveState.value = 'editing'
  if (saveTimer !== null) {
    clearTimeout(saveTimer)
  }
  saveTimer = setTimeout(() => void saveContent(), SAVE_DEBOUNCE_MS)
}

/**
 * One save at a time. A change made while one is in flight is sent after it, from the
 * revision that save produced — never from the one it was made against, which is stale
 * the moment the first save lands. The promise settles with the save in flight.
 */
function saveContent(): Promise<void> {
  const id = props.documentId
  if (id === null) {
    return Promise.resolve()
  }
  if (saving) {
    saveAgain = true
    return saving
  }
  if (saveTimer !== null) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  saving = writeContent(id)
  return saving
}

async function writeContent(id: number) {
  saveState.value = 'saving'
  saveError.value = null
  // The open block's draft goes too: a reader can type in one block for minutes without
  // leaving it, and a reload in that time must not lose it.
  const snapshot = documentWithDraft()
  try {
    const saved = await putDocumentContent(id, { markdown: snapshot, base_revision: revision.value })
    revision.value = saved.revision
    if (documentWithDraft() === snapshot) {
      saveState.value = 'saved'
    } else if (saveAgain) {
      saveState.value = 'editing'
    } else {
      scheduleSave()
    }
  } catch (error) {
    if (error instanceof ApiRequestError && error.type === 'revision_conflict') {
      // Another tab, or the agent: what is on disk wins, and the reader is told. Only
      // this 409: one for a workspace that is recovering, or a revision that could not
      // be recorded, left the draft unsaved, and it stays the reader's to retry.
      contentConflicts += 1
      saveState.value = 'saved'
      notice.value = t('viewer.changedUnderneath')
      // Only the file that conflicted; an unsaved notes draft is still the reader's.
      await loadContent('markdown')
    } else {
      saveState.value = 'failed'
      saveError.value = await resolve(error)
      retryAfterClose('markdown', () => saveContent())
    }
  } finally {
    saving = null
    if (reloadAfterSave) {
      reloadAfterSave = false
      void loadContent()
    }
    if (saveAgain) {
      saveAgain = false
      if (saveState.value === 'editing') {
        void saveContent()
      }
    }
  }
}

/** Waits for the document's saves to settle: true once what the reader sees is saved and no
 *  save met a 409 meanwhile. */
async function settleContent(): Promise<boolean> {
  const conflicts = contentConflicts
  for (let round = 0; round < 5; round += 1) {
    if (saveState.value === 'saved' && !saving) {
      return contentConflicts === conflicts
    }
    if (saveState.value === 'failed') {
      return false
    }
    await saveContent()
  }
  return false
}

let closed = false
const retriedAfterClose = { markdown: false, notes: false }

/** A save that fails once the viewer is gone has no reader to retry it: it gets one more
 *  try on its own, after the failed request has settled — one per file, so the document
 *  and the notes each get theirs. */
function retryAfterClose(file: 'markdown' | 'notes', save: () => Promise<void>) {
  if (!closed || retriedAfterClose[file]) {
    return
  }
  retriedAfterClose[file] = true
  setTimeout(() => void save(), 0)
}

/** A block action's document: recorded for the viewer's undo, then saved at once rather than
 *  after the typing debounce — one deliberate change, which the less it waits, the less room
 *  an agent writing meanwhile has to turn into a 409 (docs/web.md § Editing). */
function applyDocument(next: string) {
  // From the file as the action writes it: a copy spaced otherwise would make the first
  // step span the whole file, and its undo select every block (docs/web.md § Editing).
  undo.record('document', serializeDocument(parseResultMarkdown(markdown.value)), next)
  markdown.value = next
  saveState.value = 'editing'
  void saveContent()
}

// --- notes ---------------------------------------------------------------------------

const notesState = ref<SaveState>('saved')
const notesError = ref<string | null>(null)
let notesTimer: ReturnType<typeof setTimeout> | null = null
let savingNotes: Promise<void> | null = null
let saveNotesAgain = false
let notesConflicts = 0
let reloadNotesAfterSave = false

function scheduleNotesSave() {
  notesState.value = 'editing'
  if (notesTimer !== null) {
    clearTimeout(notesTimer)
  }
  notesTimer = setTimeout(() => void saveNotes(), SAVE_DEBOUNCE_MS)
}

function saveNotes(): Promise<void> {
  const id = props.documentId
  if (id === null) {
    return Promise.resolve()
  }
  if (savingNotes) {
    saveNotesAgain = true
    return savingNotes
  }
  if (notesTimer !== null) {
    clearTimeout(notesTimer)
    notesTimer = null
  }
  savingNotes = writeNotes(id)
  return savingNotes
}

async function writeNotes(id: number) {
  notesState.value = 'saving'
  notesError.value = null
  const snapshot = notes.value
  try {
    const saved = await putDocumentNotes(id, { text: snapshot, base_revision: notesRevision.value })
    notesRevision.value = saved.revision
    if (notes.value === snapshot) {
      notesState.value = 'saved'
    } else if (saveNotesAgain) {
      notesState.value = 'editing'
    } else {
      scheduleNotesSave()
    }
  } catch (error) {
    if (error instanceof ApiRequestError && error.type === 'revision_conflict') {
      notesConflicts += 1
      notesState.value = 'saved'
      notice.value = t('viewer.notesChanged')
      await loadContent('notes')
    } else {
      notesState.value = 'failed'
      notesError.value = await resolve(error)
      retryAfterClose('notes', () => saveNotes())
    }
  } finally {
    savingNotes = null
    if (reloadNotesAfterSave) {
      reloadNotesAfterSave = false
      void loadContent()
    }
    if (saveNotesAgain) {
      saveNotesAgain = false
      if (notesState.value === 'editing') {
        void saveNotes()
      }
    }
  }
}

async function settleNotes(): Promise<boolean> {
  const conflicts = notesConflicts
  for (let round = 0; round < 5; round += 1) {
    if (notesState.value === 'saved' && !savingNotes) {
      return notesConflicts === conflicts
    }
    if (notesState.value === 'failed') {
      return false
    }
    await saveNotes()
  }
  return false
}

/** What an ask that sends waits for: the open editor written back, and both files saved
 *  (docs/web.md § The agent panel, *An ask that sends waits for the save*). */
async function flushSaves(): Promise<boolean> {
  commitEdit()
  const [content, notesSaved] = await Promise.all([settleContent(), settleNotes()])
  return content && notesSaved
}

/** The notes editor's change: the file's new markdown, saved on the debounce. Typing is not
 *  a block action, so every undo step on the notes is stale. */
function onNotesChange(markdown: string) {
  notes.value = markdown
  undo.clear('notes')
  scheduleNotesSave()
}

/** A block action's notes — an excerpt quoted — recorded for undo and saved at once. */
function applyNotes(next: string) {
  undo.record('notes', notes.value, next)
  notes.value = next
  notesState.value = 'editing'
  void saveNotes()
}

const notesEditor = ref<EditorControls | null>(null)

/** A click below the last line of the notes still means "write here" — a click inside the
 *  editor, its TeX field included, is the editor's own. */
function onNotesClick(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.rich')) {
    notesEditor.value?.focus('end')
  }
}

/** The header's one word: the busier of the two files' states. */
const statusWord = computed<SaveState>(() => {
  const states = [saveState.value, notesState.value]
  for (const state of ['saving', 'editing', 'failed'] as const) {
    if (states.includes(state)) {
      return state
    }
  }
  return 'saved'
})

/** The one state the header draws: either file's save failed and its text is unsaved work.
 *  Every other state is announced, not shown (docs/web.md § Editing). */
const unsaved = computed(() => saveState.value === 'failed' || notesState.value === 'failed')

/** Closing with an edit still in the debounce — or still in an open block — sends it now
 *  rather than losing it. */
onBeforeUnmount(() => {
  closed = true
  commitEdit()
  // Images already uploaded and waiting on that editor go in with it.
  landWaitingImages()
  // A failed save left its text unsaved with no timer: it gets one more try too.
  if (saveTimer !== null || saveState.value === 'failed') {
    if (saveTimer !== null) {
      clearTimeout(saveTimer)
    }
    void saveContent()
  }
  if (notesTimer !== null || notesState.value === 'failed') {
    if (notesTimer !== null) {
      clearTimeout(notesTimer)
    }
    void saveNotes()
  }
})

// --- block editing -------------------------------------------------------------------

/** The open block, by position; null while every block is rendered. */
const editing = ref<{ page: number; index: number } | null>(null)
/** The open block's markdown as it now stands — its `src` until the reader changes it. */
const draft = ref('')
/** What the open editor was loaded with, and where its caret goes. Set once per opening:
 *  the editor reads them on mount, and `editorKey` remounts it for the next block. */
const editorSource = ref('')
const editorCaret = ref<Caret | null>(null)
const editorKey = ref(0)
/** How many blocks the open editor holds, from the open one on: more than one after a
 *  selection across blocks or a Backspace join. They are written back together when it
 *  closes, and until then the document keeps them all, so a reload that drops the draft
 *  never drops a block that was saved. */
const span = ref(1)
/** The open editor's draft is already a change — Backspace joined two blocks. */
const joined = ref(false)
/** A contents page opens as its lines (docs/web.md § Editing): the textarea for that one
 *  kind. A plain variable set through a function ref — there is only ever one. */
let lineEditor: HTMLTextAreaElement | null = null
/** The open rich editor, whichever of the three it is: the selection toolbar reads its
 *  selection's markdown. */
let blockEditor: EditorControls | null = null

function setLineEditor(el: Element | ComponentPublicInstance | null) {
  lineEditor = el instanceof HTMLTextAreaElement ? el : null
}

function setBlockEditor(el: Element | ComponentPublicInstance | null) {
  if (el && 'selectedMarkdown' in el) {
    blockEditor = el as unknown as EditorControls
  } else if (!el && editing.value === null) {
    // Unmounted with no editor open. One replacing it mounts after this, and an editor
    // unmounting after its replacement mounted must not clear the new one.
    blockEditor = null
  }
}

function isEditing(pageIndex: number, blockIndex: number): boolean {
  return editing.value?.page === pageIndex && editing.value.index === blockIndex
}

/** A block after the open one that the open editor holds too: still in the document until
 *  the editor is written back, but drawn only once — in the editor. */
function isHeldByEditor(pageIndex: number, blockIndex: number): boolean {
  const open = editing.value
  return open?.page === pageIndex && blockIndex > open.index && blockIndex < open.index + span.value
}

/** The open block, or undefined for the editor past the last block of a page. */
const openBlock = computed(() => {
  const open = editing.value
  return open ? pages.value[open.page]?.blocks[open.index] : undefined
})

/** The first line of a figure or image block: its placeholder, which the caption editor keeps. */
function figurePlaceholder(block: ResultBlock): string {
  return block.src.split('\n')[0] ?? ''
}

/** Opens a block — or, for a selection across blocks, the run of `count` blocks from it. */
async function startEdit(pageIndex: number, blockIndex: number, caret: Caret = 'end', count = 1) {
  if (!editable.value || isEditing(pageIndex, blockIndex)) {
    return
  }
  // Leaving the open block re-parses the document, which can move the one asked for —
  // the caller passes indices that already account for that.
  commitEdit()
  const block = pages.value[pageIndex]?.blocks[blockIndex]
  if (!block || block.kind === 'comment') {
    return
  }
  blockSelection.value = null
  editing.value = { page: pageIndex, index: blockIndex }
  span.value = count
  joined.value = false
  draft.value = openBlockSource() ?? block.src
  editorSource.value = block.kind === 'fig' || block.kind === 'img' ? (block.caption ?? '') : draft.value
  editorCaret.value = caret
  editorKey.value += 1
  // The textarea — a contents page, a rule — is focused here, the caret at the end the
  // arrows came in from; the rich editors do both themselves as they mount.
  if (!RICH_KINDS.has(block.kind) && block.kind !== 'fig' && block.kind !== 'img') {
    await nextTick()
    const at = caret === 'start' ? 0 : draft.value.length
    lineEditor?.setSelectionRange(at, at)
    lineEditor?.focus()
  }
}

/** A page emptied of its blocks, or a document with none: text comes back through an
 *  editor opened past the last block — index `blocks.length`, which no block holds. */
function startNewBlock(pageIndex: number) {
  if (!editable.value) {
    return
  }
  commitEdit()
  editing.value = { page: pageIndex, index: pages.value[pageIndex]?.blocks.length ?? 0 }
  span.value = 1
  joined.value = false
  draft.value = ''
  editorSource.value = ''
  editorCaret.value = 'end'
  editorKey.value += 1
}

/**
 * The block a press landed on, and where in it, read on mousedown — before the press blurs
 * the open editor. That blur writes the open block back, which can delete it or split it,
 * shifting every block after it on the page; by the click, the indices and the layout the
 * reader aimed at are gone. Every other block keeps its source, so the source finds it.
 */
let pressed: { page: number; index: number; src: string; dx: number; dy: number } | null = null

/** Every press starts clean; a press on a block then records itself. */
function forgetPress() {
  pressed = null
}

function onBlockPress(event: MouseEvent, pageIndex: number, blockIndex: number) {
  const block = pages.value[pageIndex]?.blocks[blockIndex]
  // The primary button only: a right-click is the browser's menu, a middle-click a scroll.
  if (event.button !== 0 || !block || !(event.currentTarget instanceof HTMLElement) || isEditing(pageIndex, blockIndex)) {
    pressed = null
    return
  }
  const rect = event.currentTarget.getBoundingClientRect()
  // Where the block will be once the open one is written back: a block after it on its
  // page moves by however many blocks the draft becomes.
  const open = editing.value
  const shift = open && open.page === pageIndex && blockIndex > open.index ? draftBlockCount() - span.value : 0
  pressed = { page: pageIndex, index: blockIndex + shift, src: block.src, dx: event.clientX - rect.left, dy: event.clientY - rect.top }
}

/** A click opens the pressed block with the caret where the press landed in it; a drag that
 *  selected text opens what it selected. Opened on the release, listened for on the whole
 *  pane: when the write-back re-renders the pressed block between press and release, the
 *  browser sends no click at all. */
function onBlockClick() {
  const press = pressed
  pressed = null
  const selection = window.getSelection()
  if (selection && !selection.isCollapsed && selection.rangeCount > 0) {
    openSelection(selection)
    return
  }
  if (!press) {
    return
  }
  const blocks = pages.value[press.page]?.blocks ?? []
  let index = -1
  for (let at = 0; at < blocks.length; at += 1) {
    if (blocks[at]?.src === press.src && (index === -1 || Math.abs(at - press.index) < Math.abs(index - press.index))) {
      index = at
    }
  }
  if (index === -1) {
    return
  }
  const element = scroller.value?.querySelector(`[data-block="${press.page}-${index}"]`)
  const rect = element?.getBoundingClientRect()
  void startEdit(press.page, index, rect ? { x: rect.left + press.dx, y: rect.top + press.dy } : 'end')
}

/**
 * A selection on the rendered blocks opens them as one editor with the same text selected,
 * so Delete, typing or Ctrl+B act on it (docs/web.md § Editing, *Selecting is editing*). Only
 * a run of rich-text blocks on one page that no fold hides: a page marker, a contents page and
 * a figure have no place inside one rich editor, and an editor would show and change text the
 * reader folded away, so a selection over any of them stays a selection.
 *
 * The selection is carried by text position — which slot of the run, how far into it — and
 * the editor resolves the same numbers in its own document, built from the same parse.
 */
function openSelection(selection: Selection) {
  const first = selection.getRangeAt(0)
  const last = selection.getRangeAt(selection.rangeCount - 1)
  const from = selectionEdge(first.startContainer, first.startOffset)
  const to = selectionEdge(last.endContainer, last.endOffset)
  if (!from || !to || from.page !== to.page || to.index < from.index) {
    return
  }
  const blocks = pages.value[from.page]?.blocks.slice(from.index, to.index + 1) ?? []
  const hidden = folds.value.hidden
  const opens = blocks.every((block, at) => RICH_KINDS.has(block.kind) && !hidden.has(blockKey({ page: from.page, index: from.index + at })))
  if (!blocks.length || !opens) {
    return
  }
  // The end's slot counts from the run's first block: the editor holds them all as one.
  let before = 0
  for (let index = from.index; index < to.index; index += 1) {
    const element = scroller.value?.querySelector(`[data-block="${from.page}-${index}"]`)
    before += element ? blockSlots(element).length : 0
  }
  const end: TextPoint = { slot: to.point.slot + before, offset: to.point.offset }
  void startEdit(from.page, from.index, { from: from.point, to: end }, blocks.length)
}

/** The rendered block a selection boundary lies in, and the boundary as a text position in
 *  it. Null inside the open editor, whose selection is its own. */
function selectionEdge(node: Node, offset: number): { page: number; index: number; point: TextPoint } | null {
  const element = node instanceof Element ? node : node.parentElement
  const block = element?.closest<HTMLElement>('[data-block]')
  if (!block || !scroller.value?.contains(block) || block.classList.contains('open')) {
    return null
  }
  const [page, index] = (block.dataset.block ?? '').split('-').map(Number)
  if (page === undefined || index === undefined || !Number.isInteger(page) || !Number.isInteger(index)) {
    return null
  }
  return { page, index, point: textPoint(blockSlots(block), node, offset) }
}

/** A block's slots: its rendering's, the viewer's controls among them passed over. */
function blockSlots(block: Element): Slot[] {
  return [...block.children].filter((child) => !child.matches('[data-chrome]')).flatMap(slotsOf)
}

/**
 * The textblocks an element draws, as the editor holds them: a list's items, a table's
 * cells row by row (each row as wide as the widest, as the editor makes them), a quote's
 * blocks, a code block's text. A typeset display formula, a formula KaTeX could not set, a
 * figure and a contents page are one slot each.
 */
function slotsOf(element: Element): Slot[] {
  if (element.matches('.md-table-wrap, table')) {
    const rows = [...element.querySelectorAll('tr')]
    const width = Math.max(1, ...rows.map((row) => row.cells.length))
    return rows.flatMap((row) => Array.from({ length: width }, (_, column) => ({ element: row.cells[column] ?? null, atom: false })))
  }
  if (element.matches('ul, ol')) {
    return [...element.children].filter((child) => child.tagName === 'LI').map((item) => ({ element: item, atom: false }))
  }
  if (element.matches('blockquote')) {
    const inner = [...element.children].flatMap(slotsOf)
    return inner.length ? inner : [{ element: null, atom: false }]
  }
  if (element.matches('pre')) {
    const code = element.querySelector(':scope > code')
    return [code ? { element: code, atom: false } : { element, atom: true }]
  }
  if (element.matches('.md-math-block, figure, .md-toc')) {
    return [{ element, atom: true }]
  }
  return [{ element, atom: false }]
}

/** A boundary as a text position among a block's slots: the slot it is in and how many
 *  characters precede it there — a boundary between slots is the start of the next one. */
function textPoint(slots: readonly Slot[], node: Node, offset: number): TextPoint {
  const boundary = document.createRange()
  boundary.setStart(node, offset)
  for (let slot = 0; slot < slots.length; slot += 1) {
    const element = slots[slot]!.element
    if (!element) {
      continue
    }
    if (element === node || element.contains(node)) {
      if (slots[slot]!.atom) {
        return { slot, offset: nearerEnd(element, node, offset) ? 1 : 0 }
      }
      return { slot, offset: unitsBefore(element, node, offset) }
    }
    if (boundary.comparePoint(element, 0) > 0) {
      return { slot, offset: 0 }
    }
  }
  // Past every slot: the end of the last one.
  for (let slot = slots.length - 1; slot >= 0; slot -= 1) {
    const { element, atom } = slots[slot]!
    if (element) {
      return { slot, offset: atom ? 1 : unitsBefore(element, element, element.childNodes.length) }
    }
  }
  return { slot: 0, offset: 0 }
}

/** Whether a boundary inside an element is nearer its end than its start. */
function nearerEnd(element: Element, node: Node, offset: number): boolean {
  const before = document.createRange()
  before.setStart(element, 0)
  before.setEnd(node, offset)
  return before.toString().length * 2 > (element.textContent ?? '').length
}

/** The characters before a boundary in a textblock, counted as the editor counts them: an
 *  inline formula and a line break one each, the viewer's controls none. A boundary inside a
 *  formula snaps to its nearer side. */
function unitsBefore(slot: Element, node: Node, offset: number): number {
  const range = document.createRange()
  range.setStart(slot, 0)
  const formula = (node instanceof Element ? node : node.parentElement)?.closest('.md-inline-math')
  if (formula && slot.contains(formula)) {
    if (nearerEnd(formula, node, offset)) {
      range.setEndAfter(formula)
    } else {
      range.setEndBefore(formula)
    }
  } else {
    range.setEnd(node, offset)
  }
  const count = (current: Node): number => {
    if (current.nodeType === Node.TEXT_NODE) {
      return current.textContent?.length ?? 0
    }
    if (current instanceof Element) {
      if (current.matches('[data-chrome]')) {
        return 0
      }
      if (current.matches('.md-inline-math, br')) {
        return 1
      }
    }
    let total = 0
    current.childNodes.forEach((child) => {
      total += count(child)
    })
    return total
  }
  return count(range.cloneContents())
}

/** The open editor's markdown, saved on the debounce while the block stays open. A caption
 *  is one line under the figure's or the image's placeholder. */
function onEditorChange(markdown: string) {
  const block = openBlock.value
  if (block?.kind === 'fig' || block?.kind === 'img') {
    const caption = captionLine(markdown)
    draft.value = caption ? `${figurePlaceholder(block)}\n${caption}` : figurePlaceholder(block)
  } else {
    draft.value = markdown
  }
  scheduleSave()
}

/** The source of the blocks under the open editor, or null when none is open. Never the
 *  draft while two blocks are joined: that draft is always unsaved work. */
function openBlockSource(): string | null {
  const open = editing.value
  if (!open || joined.value) {
    return null
  }
  const blocks = pages.value[open.page]?.blocks.slice(open.index, open.index + span.value) ?? []
  return blocks.length ? blocks.map((block) => block.src).join('\n\n') : null
}

/** The document with the open editor's draft written in — what leaving the block would
 *  make it, and what a save sends while the block is still open. */
function documentWithDraft(): string {
  const open = editing.value
  if (!open) {
    return markdown.value
  }
  const block = pages.value[open.page]?.blocks[open.index]
  if (!block) {
    // The editor past the last block (`startNewBlock`): text typed there is a new block,
    // nothing typed is nothing.
    return draft.value.trim() !== '' ? appendBlockSource(markdown.value, open.page, draft.value) : markdown.value
  }
  if (draft.value === openBlockSource()) {
    return markdown.value
  }
  // The later blocks first, so the open one's index still holds.
  let next = markdown.value
  for (let index = open.index + span.value - 1; index > open.index; index -= 1) {
    next = replaceBlockSource(next, open.page, index, '')
  }
  return replaceBlockSource(next, open.page, open.index, draft.value)
}

/** Renders the block again, and if its source changed, writes it into the document. Typing
 *  is not a block action: every undo step on the document is stale once it lands. */
function commitEdit() {
  if (!editing.value) {
    return
  }
  const next = documentWithDraft()
  editing.value = null
  span.value = 1
  joined.value = false
  if (next !== markdown.value) {
    undo.clear('document')
    markdown.value = next
    scheduleSave()
  }
}

/** The nearest block before or after one, across page markers, that a reader can open —
 *  a comment is drawn as nothing and a folded block not at all, and both are stepped over. */
function neighbour(pageIndex: number, blockIndex: number, step: -1 | 1): { page: number; index: number } | null {
  let page = pageIndex
  let index = blockIndex + step
  const hidden = folds.value.hidden
  while (page >= 0 && page < pages.value.length) {
    const blocks = pages.value[page]?.blocks ?? []
    if (index < 0) {
      page -= 1
      index = (pages.value[page]?.blocks.length ?? 0) - 1
    } else if (index >= blocks.length) {
      page += 1
      index = 0
    } else if (blocks[index]?.kind === 'comment' || hidden.has(blockKey({ page, index }))) {
      index += step
    } else {
      return { page, index }
    }
  }
  return null
}

/** How many blocks the open draft becomes once it is written back. */
function draftBlockCount(): number {
  if (draft.value === openBlockSource()) {
    return span.value
  }
  return parseResultMarkdown(draft.value).reduce((total, page) => total + page.blocks.length, 0)
}

/** Where a block will be once the open editor is written back: one after it on its page
 *  moves by however many blocks the draft becomes. */
function afterCommit(at: BlockAt): BlockAt {
  const open = editing.value
  if (open && open.page === at.page && at.index >= open.index + span.value) {
    return { page: at.page, index: at.index + draftBlockCount() - span.value }
  }
  return at
}

/** Set while the arrows move the editor to the next block, until the page has redrawn: a
 *  textarea taken down blurs as it goes, and that blur is the viewer moving on, not the
 *  reader leaving. */
let movingOn = false

/** ArrowUp on the first line or ArrowDown on the last: into the block beside it, caret
 *  and all (docs/web.md § Editing). */
function navigateFrom(direction: 'up' | 'down'): boolean {
  const open = editing.value
  if (!open || !openBlock.value) {
    return false
  }
  const target = neighbour(open.page, open.index + (direction === 'up' ? 0 : span.value - 1), direction === 'up' ? -1 : 1)
  if (!target) {
    return false
  }
  // Written back, the open block may have become more blocks or none: a target after it
  // on its page moves by the difference.
  if (direction === 'down' && target.page === open.page) {
    target.index += draftBlockCount() - span.value
  }
  movingOn = true
  commitEdit()
  void startEdit(target.page, target.index, direction === 'up' ? 'end' : 'start')
  void nextTick(() => (movingOn = false))
  return true
}

/** Backspace at the very start of a block: join it to the text block before it on the
 *  same page, or — emptied — remove it and continue at the end of the visible one before.
 *  A fold that hides the block before opens first, so the join happens in sight. */
function mergeBack(current: string): boolean {
  const open = editing.value
  const block = openBlock.value
  if (!open || !block || open.index === 0) {
    return false
  }
  const previous = pages.value[open.page]?.blocks[open.index - 1]
  const empty = current.trim() === ''
  // An editor holding several blocks joins nothing more; joining a third is a second
  // Backspace's job once this one is written back.
  if (span.value > 1 || !previous || (!empty && !(JOINABLE.has(block.kind) && JOINABLE.has(previous.kind)))) {
    return false
  }
  if (empty) {
    // An emptied block is deleted, as leaving it would; the caret goes to the visible
    // block before it, never into a fold. The deletion moves nothing before it.
    const target = neighbour(open.page, open.index, -1)
    draft.value = ''
    commitEdit()
    if (target) {
      void startEdit(target.page, target.index, 'end')
    }
    return true
  }
  const previousAt = placedIndex.value.get(blockKey({ page: open.page, index: open.index - 1 }))
  if (previousAt !== undefined) {
    openFoldsOver([previousAt])
  }
  // The open block's text travels into the one before: both stay in the document until
  // the joined editor is written back (`joined`).
  editing.value = { page: open.page, index: open.index - 1 }
  span.value = 2
  joined.value = true
  // Both blocks, so a reader who leaves at once still keeps every word; the editor joins
  // them at the seam as it opens.
  draft.value = `${previous.src}\n\n${current}`
  editorSource.value = draft.value
  editorCaret.value = 'join'
  editorKey.value += 1
  return true
}

/**
 * The reader left the open editor. A blur that follows a press on one of the viewer's own
 * controls among the blocks is not a leave: that control writes the editor back when it acts.
 * Escape closes the editor and selects the blocks its text became, so the keyboard keeps its
 * place in the document (docs/web.md § Editing, *Keyboard block selection*).
 */
function leaveEditor(reason: LeaveReason) {
  if (reason === 'blur' && chromePressed) {
    chromePressed = false
    return
  }
  if (reason === 'blur' && movingOn) {
    return
  }
  const open = editing.value
  if (reason !== 'escape' || !open) {
    commitEdit()
    return
  }
  const count = draftBlockCount()
  commitEdit()
  let keys = rowKeysIn(open.page, open.index, count)
  if (!keys.length) {
    // Emptied, the run is gone: the block now in its place, or the one before it.
    const near = rowIndexNear(open.page, open.index)
    keys = near === null ? [] : [rows.value[near]!.key]
  }
  if (keys.length) {
    selectKeys(keys, true)
    void nextTick(() => focusBlock(keys[0]!))
  } else {
    void nextTick(() => scroller.value?.querySelector<HTMLElement>('.block-add')?.focus())
  }
}

function onLineEditorKey(event: KeyboardEvent) {
  if (event.key === 'Escape' || (event.key === 'Enter' && (event.ctrlKey || event.metaKey))) {
    event.stopPropagation()
    event.preventDefault()
    leaveEditor(event.key === 'Escape' ? 'escape' : 'enter')
    return
  }
  // Arrowing past the first or last line moves to the next block, as in the rich editor.
  const box = event.target instanceof HTMLTextAreaElement ? event.target : null
  if (!box || box.selectionStart !== box.selectionEnd) {
    return
  }
  const firstLine = box.value.lastIndexOf('\n', box.selectionStart - 1) === -1
  const lastLine = box.value.indexOf('\n', box.selectionEnd) === -1
  if ((event.key === 'ArrowUp' && firstLine) || (event.key === 'ArrowDown' && lastLine)) {
    if (navigateFrom(event.key === 'ArrowUp' ? 'up' : 'down')) {
      event.preventDefault()
    }
  }
}

/** Enough rows for the lines plus one to type into — the textarea does not scroll. */
const lineEditorRows = computed(() => Math.max(2, draft.value.split('\n').length + 1))

// --- collapsed sections ------------------------------------------------------------------

/** Whose folds the browser remembers: the document on Files, the job on History. */
const foldId = computed(() => (props.documentId !== null ? `doc:${props.documentId}` : props.jobId ? `job:${props.jobId}` : null))

function readFolds(): FoldMemory {
  if (!import.meta.client) {
    return {}
  }
  try {
    return readFoldMemory(localStorage.getItem(FOLDS_KEY))
  } catch {
    return {}
  }
}

/** The folds the reader set — the chevron, the fold note, the block menu — as remembered;
 *  what a find or a jump opens is the view's alone (docs/web.md § Result viewer, *What leads
 *  into a fold opens it*). */
let rememberedFolds = new Set<string>(foldId.value ? (readFolds()[foldId.value]?.slugs ?? []) : [])
/** The collapsed headings drawn now, by slug. */
const collapsed = ref(new Set(rememberedFolds))

/** The reader's folds, written to the browser when they changed. */
function keepFolds(slugs: ReadonlySet<string>) {
  const id = foldId.value
  const same = slugs.size === rememberedFolds.size && [...slugs].every((slug) => rememberedFolds.has(slug))
  if (!id || same) {
    return
  }
  rememberedFolds = new Set(slugs)
  try {
    localStorage.setItem(FOLDS_KEY, JSON.stringify(rememberFolds(readFolds(), id, slugs, Date.now())))
  } catch {
    // Storage full or blocked: the folds still hold for as long as the viewer is open.
  }
}

/** A fold the reader set or opened: the view and the memory both. */
function setFold(slug: string, fold: boolean) {
  const view = new Set(collapsed.value)
  const memory = new Set(rememberedFolds)
  for (const set of [view, memory]) {
    if (fold) {
      set.add(slug)
    } else {
      set.delete(slug)
    }
  }
  collapsed.value = view
  keepFolds(memory)
}

/** Opens every fold holding any of the blocks at `indices` of `placed` — the view only. */
function openFoldsOver(indices: readonly number[]) {
  const open = foldsOver(placed.value, outline.value, collapsed.value, indices)
  if (open.length) {
    collapsed.value = new Set([...collapsed.value].filter((slug) => !open.includes(slug)))
  }
}

const folds = computed(() => foldsOf(placed.value, outline.value, collapsed.value))

/** The visible items of the document: what the keyboard steps over and a move carries. */
const rows = computed(() => rowsOf(placed.value, folds.value))
const rowIndexOf = computed(() => new Map(rows.value.map((row, index) => [row.key, index])))

/** The rows covering the blocks at `start`–`end` of `placed`. */
function rowsCovering(start: number, end: number): string[] {
  return rows.value.filter((row) => row.last >= start && row.first <= end).map((row) => row.key)
}

/** The rows a run of `count` blocks from a position covers. */
function rowKeysIn(page: number, index: number, count: number): string[] {
  const start = placedIndex.value.get(blockKey({ page, index }))
  return start === undefined || count < 1 ? [] : rowsCovering(start, start + count - 1)
}

/** The row at a position once its blocks are gone: the one now standing there, else the one
 *  before it. */
function rowIndexNear(page: number, index: number): number | null {
  const at = placed.value.findIndex((entry) => entry.page > page || (entry.page === page && entry.index >= index))
  const start = at === -1 ? placed.value.length : at
  const after = rows.value.findIndex((row) => row.last >= start)
  if (after !== -1) {
    return after
  }
  return rows.value.length ? rows.value.length - 1 : null
}

/** The document loaded whole: a partial result lacks pages still to come. */
const wholeDocument = computed(
  () => (props.result !== null || props.markdownDocument) && !partial.value && (props.documentId === null || editable.value),
)

function onFindExpand(key: string) {
  const index = placedIndex.value.get(key)
  if (index !== undefined) {
    openFoldsOver([index])
  }
}

/** The chevron: the heading's fold, set by the reader. */
function toggleFold(key: string) {
  const at = afterCommit(parseKey(key))
  commitEdit()
  const heading = headingAt.value.get(blockKey(at))
  if (heading) {
    setFold(heading.slug, !collapsed.value.has(heading.slug))
  }
}

/** The fold note expands what it names; focus goes to the heading — its block on Files, its
 *  chevron on History — since the note goes away as it expands. */
function expandFromNote(key: string) {
  const at = afterCommit(parseKey(key))
  commitEdit()
  const heading = headingAt.value.get(blockKey(at))
  if (!heading) {
    return
  }
  setFold(heading.slug, false)
  void nextTick(() => {
    const block = scroller.value?.querySelector<HTMLElement>(`[data-block="${blockKey(at)}"]`)
    const target = editable.value ? block : block?.querySelector<HTMLElement>(':scope > .block-chevron')
    target?.focus({ preventScroll: true })
  })
}

function parseKey(key: string): BlockAt {
  const [page = 0, index = 0] = key.split('-').map(Number)
  return { page, index }
}

// --- keyboard block selection ----------------------------------------------------------------

/** A run of whole blocks, by the keys of its two ends: `head` moves, `anchor` stays. Made by
 *  the keyboard — with the hint bar — or by a block menu, for as long as it is open. */
interface BlockSelection {
  anchor: string
  head: string
  keyboard: boolean
}

const blockSelection = ref<BlockSelection | null>(null)

const selectedRange = computed<RowRange | null>(() => {
  const selection = blockSelection.value
  const anchor = selection ? rowIndexOf.value.get(selection.anchor) : undefined
  const head = selection ? rowIndexOf.value.get(selection.head) : undefined
  return anchor === undefined || head === undefined ? null : { from: Math.min(anchor, head), to: Math.max(anchor, head) }
})

const selectedKeys = computed(() => {
  const range = selectedRange.value
  return new Set(range ? rows.value.slice(range.from, range.to + 1).map((row) => row.key) : [])
})

/** The legend shows while a selection the keyboard made exists. */
const showHint = computed(() => blockSelection.value?.keyboard === true && selectedRange.value !== null)

function selectKeys(keys: readonly string[], keyboard: boolean) {
  blockSelection.value = keys.length ? { anchor: keys[keys.length - 1]!, head: keys[0]!, keyboard } : null
}

/** The rows of a run of keys, in order. */
function rangeOf(keys: readonly string[]): RowRange | null {
  const indices = keys.map((key) => rowIndexOf.value.get(key)).filter((index): index is number => index !== undefined)
  return indices.length ? { from: Math.min(...indices), to: Math.max(...indices) } : null
}

/** A selection survives a change to the document only where every one of its blocks is found
 *  again by its source (`followBlocks`). */
function followSelection(before: readonly PlacedBlock[], after: readonly PlacedBlock[]) {
  const selection = blockSelection.value
  if (!selection) {
    return
  }
  const map = followBlocks(before, after)
  const beforeAt = new Map(before.map((entry, index) => [blockKey(entry), index]))
  const follow = (key: string): string | null => {
    const index = beforeAt.get(key)
    const moved = index === undefined ? null : (map[index] ?? null)
    return moved === null ? null : blockKey(after[moved]!)
  }
  const anchorAt = beforeAt.get(selection.anchor)
  const headAt = beforeAt.get(selection.head)
  if (anchorAt === undefined || headAt === undefined) {
    blockSelection.value = null
    return
  }
  for (let index = Math.min(anchorAt, headAt); index <= Math.max(anchorAt, headAt); index += 1) {
    if (before[index]!.block.kind !== 'comment' && (map[index] ?? null) === null) {
      blockSelection.value = null
      return
    }
  }
  const anchor = follow(selection.anchor)
  const head = follow(selection.head)
  blockSelection.value = anchor && head ? { ...selection, anchor, head } : null
}

/** Focus on a block, scrolled into view only as far as it must be. */
function focusBlock(key: string) {
  const element = scroller.value?.querySelector<HTMLElement>(`[data-block="${key}"]`)
  if (element) {
    element.focus({ preventScroll: true })
    element.scrollIntoView({ block: 'nearest' })
  }
}

/** The keys with focus on a block and no editor open (docs/web.md § Editing, *Keyboard block
 *  selection*). Each layer's Escape stops there: editor, selection, dialog. */
function onBlockKey(event: KeyboardEvent) {
  if (event.target !== event.currentTarget || !editable.value || editing.value) {
    return
  }
  const key = (event.currentTarget as HTMLElement).dataset.block ?? ''
  const row = rowIndexOf.value.get(key)
  if (row === undefined) {
    return
  }
  const selected = selectedKeys.value.has(key)
  // A block focused with Tab becomes a selection of one at its first key, which then acts.
  const ensure = () => {
    if (!selected) {
      selectKeys([key], true)
    } else if (blockSelection.value && !blockSelection.value.keyboard) {
      blockSelection.value = { ...blockSelection.value, keyboard: true }
    }
  }
  const arrow = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0
  if (arrow && matches(event, arrow < 0 ? 'mod+shift+up' : 'mod+shift+down')) {
    event.preventDefault()
    ensure()
    moveSelection(arrow < 0 ? 'up' : 'down')
  } else if (arrow && event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault()
    const selection = blockSelection.value
    if (!selected || !selection) {
      selectKeys([key], true)
      return
    }
    const next = rows.value[row + arrow]
    if (next) {
      blockSelection.value = { anchor: selection.anchor, head: next.key, keyboard: true }
      focusBlock(next.key)
    }
  } else if (arrow && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault()
    if (!selected) {
      selectKeys([key], true)
      return
    }
    const next = rows.value[row + arrow]
    if (next) {
      selectKeys([next.key], true)
      focusBlock(next.key)
    }
  } else if (matches(event, 'mod+/') || (event.key === 'F10' && event.shiftKey) || event.key === 'ContextMenu') {
    event.preventDefault()
    ensure()
    void openBlockMenu([...selectedKeys.value], key, true, true)
  } else if (event.key === 'Enter' && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault()
    const at = parseKey(key)
    void startEdit(at.page, at.index, 'end')
  } else if ((event.key === 'Delete' || event.key === 'Backspace') && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault()
    ensure()
    const range = selectedRange.value
    if (!range) {
      return
    }
    const keys = deleteRows(range)
    selectKeys(keys, true)
    void nextTick(() => (keys.length ? focusBlock(keys[0]!) : scroller.value?.querySelector<HTMLElement>('.block-add')?.focus()))
  } else if (matches(event, 'mod+shift+c') || (matches(event, 'mod+c') && (window.getSelection()?.isCollapsed ?? true))) {
    // ⌘C too, for a browser that keeps ⌘⇧C for its developer tools — but only with no text
    // selected, which ⌘C copies as it always has.
    event.preventDefault()
    ensure()
    const range = selectedRange.value
    if (range) {
      void writeClipboard(rowsMarkdown(range), t('viewer.note.copied'))
    }
  } else if (event.key === 'Escape' && blockSelection.value) {
    event.preventDefault()
    event.stopPropagation()
    blockSelection.value = null
  }
}

/** ⌘⇧↑ / ⌘⇧↓: the selection moves one visible item, and stays selected and in view. */
function moveSelection(direction: 'up' | 'down') {
  const range = selectedRange.value
  if (!range) {
    return
  }
  const keys = moveRows(range, direction)
  if (keys.length) {
    selectKeys(keys, true)
    void nextTick(() => focusBlock(keys[0]!))
  }
}

// --- the handle, hover and focus --------------------------------------------------------------

/** The block under the pointer and the block holding focus: where a handle is drawn. */
const hoveredKey = ref<string | null>(null)
const focusedKey = ref<string | null>(null)
/** The handle whose menu is open: drawn pressed while it is. */
const menuHandleKey = ref<string | null>(null)

/** A handle exists only where it can be used now (docs/web.md § Editing, *The block handle
 *  and its menu*). */
function handleShown(key: string): boolean {
  return (
    editable.value &&
    (key === hoveredKey.value ||
      key === focusedKey.value ||
      key === menuHandleKey.value ||
      selectedKeys.value.has(key) ||
      dragKeys.value.has(key))
  )
}

let hoverFrame = 0
let hoverAt: { x: number; y: number; target: EventTarget | null } | null = null

function onDocumentPointerMove(event: PointerEvent) {
  if (event.pointerType === 'touch' || !editable.value) {
    return
  }
  hoverAt = { x: event.clientX, y: event.clientY, target: event.target }
  if (!hoverFrame) {
    hoverFrame = requestAnimationFrame(readHover)
  }
}

/** The block the pointer is over — or level with, in the gutters, so the pointer on its way
 *  to a handle keeps it. In the gap between two blocks the last one stays. */
function readHover() {
  hoverFrame = 0
  const at = hoverAt
  const box = scroller.value
  if (!at || !box) {
    return
  }
  let block = at.target instanceof Element ? at.target.closest<HTMLElement>('[data-block]') : null
  if (!block) {
    const column = box.querySelector('.prose')?.getBoundingClientRect()
    if (column && (at.x < column.left || at.x > column.right)) {
      block = document.elementFromPoint(column.left + 1, at.y)?.closest<HTMLElement>('[data-block]') ?? null
    }
  }
  if (block && box.contains(block)) {
    hoveredKey.value = block.dataset.block ?? null
  }
}

function onDocumentPointerLeave() {
  hoveredKey.value = null
}

function onDocumentFocusIn(event: FocusEvent) {
  const block = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-block]') : null
  focusedKey.value = block?.dataset.block ?? null
}

function onDocumentFocusOut(event: FocusEvent) {
  if (!(event.relatedTarget instanceof Node) || !scroller.value?.contains(event.relatedTarget)) {
    focusedKey.value = null
  }
}

/**
 * A press on one of the viewer's controls among the blocks (`data-chrome`), noted on
 * `pointerdown` — before the blur it causes, since Safari focuses no button on a click and
 * the blur's `relatedTarget` cannot be trusted. The open editor's blur that follows it is not
 * a leave.
 */
let chromePressed = false

function onDocumentPointerDown(event: PointerEvent) {
  chromePressed = event.target instanceof Element && event.target.closest('[data-chrome]') !== null
}

function forgetChromePress() {
  if (chromePressed) {
    setTimeout(() => (chromePressed = false), 0)
  }
}

/** Block kinds as the menu's header and the handle's name say them. */
function kindLabel(block: ResultBlock): string {
  return t(`viewer.block.kinds.${block.kind === 'h2' || block.kind === 'h3' ? 'h' : block.kind}`)
}

/** The words a block's name is built from (`blockName`), in the reader's language. */
const nameText = computed<NameText>(() => ({
  formula: t('viewer.names.formula'),
  formulaTag: (tag) => t('viewer.names.formulaTag', { tag }),
  quoted: (words) => t('viewer.names.quoted', { words }),
  figure: (words) => t('viewer.names.figure', { name: t('viewer.names.quoted', { words }) }),
  figureBare: t('viewer.names.figureBare'),
  image: (words) => t('viewer.names.image', { name: t('viewer.names.quoted', { words }) }),
  imageBare: t('viewer.names.imageBare'),
}))

function isHeading(block: ResultBlock): boolean {
  return block.kind === 'h2' || block.kind === 'h3'
}

/** Each block stays one tab stop on Files; on History a block takes focus only from the
 *  viewer, when the way back hands it over. */
function blockTabindex(block: ResultBlock, pageIndex: number, index: number): number | undefined {
  if (block.kind === 'comment' || isEditing(pageIndex, index)) {
    return undefined
  }
  return editable.value ? 0 : -1
}

// --- the block menu -----------------------------------------------------------------------

interface BlockMenu {
  at: Point
  /** The rows it acts on, by key, in order. */
  keys: string[]
  header: string
  items: MenuItem[]
  /** Opened from the keyboard: its first item highlighted, and focus after it chosen by it. */
  fromKey: boolean
  /** Leaves its blocks selected when it closes: opened from the keyboard, or over a
   *  selection the keyboard made. */
  keep: boolean
}

const blockMenu = ref<BlockMenu | null>(null)
/** What the chosen action leaves for the menu's close: the blocks to select or focus, and
 *  whether focus is the close's to move — an ask moves it itself. */
let menuAfter: { keys: string[]; focus: boolean } | null = null
let menuClosedAt = 0
let menuClosedBy: EventTarget | null = null

/** The handle's click: the menu for its block — or the whole selection when the block is in
 *  one. The blocks an open editor holds have one handle; it writes the editor back first and
 *  acts on the blocks the text became. */
function onHandleClick(event: MouseEvent, key: string) {
  const handle = event.currentTarget
  if (
    performance.now() - menuClosedAt < REOPEN_GUARD_MS &&
    handle instanceof Element &&
    menuClosedBy instanceof Node &&
    handle.contains(menuClosedBy)
  ) {
    return
  }
  const open = editing.value
  if (open && key === blockKey(open)) {
    const count = draftBlockCount()
    commitEdit()
    const keys = rowKeysIn(open.page, open.index, count)
    void openBlockMenu(keys, keys[0] ?? key, false, false)
    return
  }
  const at = blockKey(afterCommit(parseKey(key)))
  commitEdit()
  const selection = blockSelection.value
  if (selection && selectedKeys.value.has(at)) {
    void openBlockMenu([...selectedKeys.value], at, false, selection.keyboard)
  } else {
    void openBlockMenu([at], at, false, false)
  }
}

/**
 * Opens the menu beside the handle, to its right and top-aligned with the block. While it is
 * open its blocks are the selection — washed, without the hint bar unless the keyboard made
 * it — so it is plain what the menu acts on. The skills are read again for the next menu; this
 * one keeps the list it opened with.
 */
async function openBlockMenu(keys: string[], handleKey: string, fromKey: boolean, keep: boolean) {
  if (!keys.length) {
    return
  }
  if (!keep) {
    selectKeys(keys, false)
  }
  void loadSkills()
  menuHandleKey.value = handleKey
  await nextTick()
  const block = scroller.value?.querySelector<HTMLElement>(`[data-block="${handleKey}"]`)
  if (!block) {
    menuHandleKey.value = null
    return
  }
  const handle = block.querySelector<HTMLElement>(':scope > .block-handle')?.getBoundingClientRect()
  const top = block.getBoundingClientRect().top
  blockMenu.value = {
    at: { x: handle ? handle.right + 4 : block.getBoundingClientRect().left, y: top },
    keys,
    header: menuHeader(keys),
    items: blockMenuItems(keys),
    fromKey,
    keep,
  }
}

function rowHead(key: string): PlacedBlock | null {
  const row = rows.value[rowIndexOf.value.get(key) ?? -1]
  return row ? (placed.value[row.first] ?? null) : null
}

/** What the menu acts on: the block's kind, or how many blocks. */
function menuHeader(keys: readonly string[]): string {
  const only = keys.length === 1 ? rowHead(keys[0]!) : null
  return only ? kindLabel(only.block) : t('viewer.block.count', { n: keys.length }, keys.length)
}

/** What *Ask* and *Translate* say they ask about. */
function askWhich(blocks: readonly ResultBlock[]): 'paragraph' | 'section' | 'formula' | 'block' | 'blocks' {
  const only = blocks.length === 1 ? blocks[0]! : null
  if (!only) {
    return 'blocks'
  }
  return only.kind === 'p' ? 'paragraph' : isHeading(only) ? 'section' : only.kind === 'math' ? 'formula' : 'block'
}

/** The heads of a run's rows: a collapsed heading stands for itself alone, to everything but
 *  a move (docs/web.md § Editing, *A collapsed section is its heading, except to a move*). */
function rowHeads(range: RowRange): PlacedBlock[] {
  return rows.value.slice(range.from, range.to + 1).map((row) => placed.value[row.first]!)
}

function rowsMarkdown(range: RowRange): string {
  return rowHeads(range)
    .map((entry) => entry.block.src)
    .join('\n\n')
}

/** The menu's items, in four groups (docs/web.md § Editing, *The block handle and its menu*). */
function blockMenuItems(keys: readonly string[]): MenuItem[] {
  const range = rangeOf(keys)
  if (!range) {
    return []
  }
  const blocks = rowHeads(range).map((entry) => entry.block)
  const which = askWhich(blocks)
  const kinds = blocks.map(blockKindName)
  const single = keys.length === 1 ? keys[0]! : null
  const items: MenuItem[] = [{ key: 'ask', label: t(`viewer.block.ask.${which}`), icon: 'message-circle' }]
  if (translateSkill.value && blocks.some(translatable)) {
    items.push({
      key: 'translate',
      label: t(`viewer.block.translate.${which === 'formula' ? 'block' : which}`),
      icon: 'languages',
      shortcut: '/translate',
      shortcutMono: true,
    })
  }
  items.push({
    key: 'quote',
    label: t('viewer.block.quote'),
    icon: 'notebook-pen',
    separated: true,
    disabled: excerptPassage(blocks.map((block) => block.src).join('\n\n')) === null,
  })
  if (single !== null && markers.value.has(single)) {
    items.push({ key: 'excerpt', label: t('viewer.block.excerpt'), icon: 'quote' })
  }
  items.push(
    { key: 'copy', label: t('viewer.block.copy'), icon: 'copy', shortcut: keyLabel('mod+shift+c') },
    { key: 'link', label: t('viewer.block.link'), icon: 'link' },
    {
      key: 'turn',
      label: t('viewer.block.turnInto'),
      icon: 'repeat',
      separated: true,
      disabled: kinds.every((kind) => kind === null),
      children: TURN_KINDS.map((kind) => ({
        key: `turn:${kind}`,
        label: t(`viewer.convert.${kind}`),
        checked: kinds.every((current) => current === kind),
        shortcut: SLASH_CONVERSIONS.find((item) => item.key === kind)?.markdown || undefined,
        shortcutMono: true,
      })),
    },
    {
      key: 'up',
      label: t('viewer.block.moveUp'),
      icon: 'arrow-up',
      shortcut: keyLabel('mod+shift+up'),
      disabled: moveTarget(rows.value, range.from, range.to, 'up') === null,
    },
    {
      key: 'down',
      label: t('viewer.block.moveDown'),
      icon: 'arrow-down',
      shortcut: keyLabel('mod+shift+down'),
      disabled: moveTarget(rows.value, range.from, range.to, 'down') === null,
    },
  )
  const heading = single !== null ? headingAt.value.get(single) : undefined
  if (heading) {
    const shut = collapsed.value.has(heading.slug)
    items.push({
      key: 'fold',
      label: t(shut ? 'viewer.fold.expand' : 'viewer.fold.collapse'),
      icon: shut ? 'chevron-right' : 'chevron-down',
    })
  }
  items.push({ key: 'delete', label: t('viewer.block.delete'), icon: 'trash', danger: true, separated: true })
  return items
}

function onBlockMenuSelect(choice: string) {
  const menu = blockMenu.value
  const range = menu ? rangeOf(menu.keys) : null
  if (!menu || !range) {
    return
  }
  const heads = rowHeads(range)
  const keep = { keys: menu.keys, focus: true }
  if (choice === 'ask' || choice === 'translate') {
    const request = askAbout(heads)
    menuAfter = { keys: menu.keys, focus: false }
    void askAgent(choice === 'translate' ? { ...request, command: 'translate', send: true } : request, menu.keys[0]!)
  } else if (choice === 'quote') {
    quoteInNotes(rowsMarkdown(range), heads[0]!)
    menuAfter = keep
  } else if (choice === 'excerpt') {
    // From the keyboard focus goes into the notes, at the excerpt, so what was reached is
    // read out; from the pointer it stays, as the marker leaves it.
    revealExcerpt(menu.keys[0]!, menu.fromKey)
    menuAfter = { keys: menu.keys, focus: !menu.fromKey }
  } else if (choice === 'copy') {
    void writeClipboard(rowsMarkdown(range), t('viewer.note.copied'))
    menuAfter = keep
  } else if (choice === 'link') {
    copyLink(heads[0]!)
    menuAfter = keep
  } else if (choice.startsWith('turn:')) {
    const turned = turnRows(range, choice.slice('turn:'.length) as BlockKindName)
    menuAfter = { keys: turned.length ? turned : menu.keys, focus: true }
  } else if (choice === 'up' || choice === 'down') {
    const moved = moveRows(range, choice)
    menuAfter = { keys: moved.length ? moved : menu.keys, focus: true }
  } else if (choice === 'fold') {
    const heading = headingAt.value.get(menu.keys[0]!)
    if (heading) {
      setFold(heading.slug, !collapsed.value.has(heading.slug))
    }
    menuAfter = keep
  } else if (choice === 'delete') {
    menuAfter = { keys: deleteRows(range), focus: true }
  }
}

/**
 * Focus after the menu (docs/web.md § Editing, *Focus after the block menu*): the first of the
 * blocks it acted on — the block now in place after a delete, the moved or converted blocks,
 * the heading after a fold — or *Add text* when no block is left. A menu the keyboard opened,
 * or opened over a selection the keyboard made, leaves its blocks selected; one the pointer
 * opened on a lone block leaves it focused and nothing selected. A press elsewhere, a scroll
 * or Tab is the reader moving on: the selection goes and focus stays where they took it.
 */
function onBlockMenuClose(cause?: Event) {
  const menu = blockMenu.value
  const after = menuAfter
  blockMenu.value = null
  menuHandleKey.value = null
  menuAfter = null
  menuClosedAt = performance.now()
  menuClosedBy = cause?.target ?? null
  if (!menu) {
    return
  }
  const tabbed = cause instanceof KeyboardEvent && cause.key === 'Tab'
  if (cause && (tabbed || !(cause instanceof KeyboardEvent))) {
    if (!(tabbed && menu.keep)) {
      blockSelection.value = null
    }
    return
  }
  void nextTick(() => {
    const keys = (after?.keys ?? menu.keys).filter((key) => rowIndexOf.value.has(key))
    if (menu.keep && keys.length) {
      selectKeys(keys, true)
    } else {
      blockSelection.value = null
    }
    if (after && !after.focus) {
      return
    }
    if (keys.length) {
      focusBlock(keys[0]!)
    } else {
      scroller.value?.querySelector<HTMLElement>('.block-add')?.focus()
    }
  })
}

// --- block actions ------------------------------------------------------------------------

/** What *Ask* puts in the composer: a lone heading as its section's chip, anything else as the
 *  blocks' markdown. */
function askAbout(heads: readonly PlacedBlock[]): AskRequest {
  const only = heads.length === 1 ? heads[0]! : null
  if (only && isHeading(only.block)) {
    const text = only.block.kind === 'h2' || only.block.kind === 'h3' ? only.block.text : ''
    return { section: { heading: text, text: sectionSource(pages.value, only.page, only.index) } }
  }
  return { selection: heads.map((entry) => entry.block.src).join('\n\n') }
}

/** Moves a run one visible item up or down; the keys of the rows it now covers, or none. */
function moveRows(range: RowRange, direction: 'up' | 'down'): string[] {
  const target = moveTarget(rows.value, range.from, range.to, direction)
  return target ? moveRun(range, target.target, target.place) : []
}

/**
 * Moves rows `from`–`to` — a collapsed heading with its whole section — before or after the
 * block at `target` of `placed` (`moveBlocks`). A fold the run lands in opens, so the reader
 * keeps sight of it; the moved headings' own folds follow them. The keys of the rows it now
 * covers, or none when nothing moved.
 */
function moveRun(range: RowRange, target: number, place: 'before' | 'after'): string[] {
  const run = rows.value.slice(range.from, range.to + 1)
  const first = placed.value[run[0]!.first]
  const last = placed.value[run[run.length - 1]!.last]
  const aim = placed.value[target]
  if (!first || !last || !aim) {
    return []
  }
  const moved = moveBlocks(markdown.value, { from: first, to: last }, aim, place)
  if (!moved) {
    return []
  }
  // Where each row's own block lands, counted from the run's first.
  const offsets = run.map((row) => row.first - run[0]!.first)
  applyDocument(moved.markdown)
  const start = placedIndex.value.get(blockKey(moved.first))
  const end = placedIndex.value.get(blockKey(moved.last))
  if (start === undefined || end === undefined) {
    return []
  }
  openFoldsOver(offsets.map((offset) => start + offset))
  showNote(t('viewer.note.moved'), false)
  return rowsCovering(start, end)
}

/** Deletes the rows' blocks — a collapsed heading alone, its section then shown — and says how
 *  many, with *Undo*. The key of the row now standing in their place, or none. */
function deleteRows(range: RowRange): string[] {
  const heads = rowHeads(range)
  let next = markdown.value
  // The last first, so the earlier ones' indices still hold.
  for (const at of [...heads].reverse()) {
    next = replaceBlockSource(next, at.page, at.index, '')
  }
  if (next === markdown.value) {
    return []
  }
  applyDocument(next)
  showNote(t('viewer.note.deleted', { n: heads.length }, heads.length), true)
  const now = rows.value[range.from] ?? rows.value[range.from - 1]
  return now ? [now.key] : []
}

/** Turns each block of the run that can be into `kind` (`convertBlock`); the keys of the rows
 *  the converted run now covers, or none when nothing changed. */
function turnRows(range: RowRange, kind: BlockKindName): string[] {
  const heads = rowHeads(range)
  let next = markdown.value
  /** Blocks a conversion added on each page: what the run's last block moved by. */
  const grown = new Map<number, number>()
  for (const at of [...heads].reverse()) {
    const converted = convertBlock(at.block.src, kind)
    if (converted === null || converted === at.block.src) {
      continue
    }
    next = replaceBlockSource(next, at.page, at.index, converted)
    const count = parseResultMarkdown(converted).reduce((total, page) => total + page.blocks.length, 0)
    grown.set(at.page, (grown.get(at.page) ?? 0) + count - 1)
  }
  if (next === markdown.value) {
    return []
  }
  const first = heads[0]!
  const last = heads[heads.length - 1]!
  applyDocument(next)
  const start = placedIndex.value.get(blockKey(first))
  const end = placedIndex.value.get(blockKey({ page: last.page, index: last.index + (grown.get(last.page) ?? 0) }))
  return start === undefined || end === undefined ? [] : rowsCovering(start, end)
}

/** *Copy link to block*: a reference to the block — its name linking to the app's own origin,
 *  in the current locale — as markdown and as HTML (docs/web.md § Editing, *The block handle
 *  and its menu*; § Files, *A block can be linked to*). */
function copyLink(at: BlockAt) {
  const block = pages.value[at.page]?.blocks[at.index]
  if (!block) {
    return
  }
  const anchor = blockAnchor(pages.value, at, outline.value)
  const path = localePath({ path: '/files', query: { document: String(props.documentId), block: anchor } })
  const href = new URL(path, window.location.origin).href
  const name = blockName(block, nameText.value)
  // The destination's parentheses escaped, or a `)` in the block's words would end the link.
  const markdown = `[${name.replace(/[[\]]/g, '\\$&')}](${href.replace(/\(/g, '%28').replace(/\)/g, '%29')})`
  const anchorEl = document.createElement('a')
  anchorEl.href = href
  anchorEl.textContent = name
  void writeClipboard(markdown, t('viewer.note.linkCopied'), anchorEl.outerHTML)
}

/** Writes to the clipboard — with an HTML form too, where the browser takes one — and says
 *  so; or says it could not, never *Copied* for a write the browser refused. */
async function writeClipboard(text: string, note: string, html?: string) {
  try {
    if (html && typeof ClipboardItem === 'function') {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/plain': new Blob([text], { type: 'text/plain' }),
          'text/html': new Blob([html], { type: 'text/html' }),
        }),
      ])
    } else {
      await navigator.clipboard.writeText(text)
    }
    showNote(note, false)
  } catch {
    showNote(t('viewer.note.copyFailed'), false)
  }
}

// --- undo and the status note ----------------------------------------------------------------

function currentText(file: UndoFile): string {
  return file === 'document' ? markdown.value : notes.value
}

/**
 * Undo or redo the last block action: its file put back and saved like any edit. In the
 * document what came back is selected and scrolled into view, since an undone move can land
 * off screen; in the notes it is scrolled to and washed.
 */
function stepUndo(direction: 'undo' | 'redo', keyboard: boolean) {
  const step = direction === 'undo' ? undo.undo(currentText) : undo.redo(currentText)
  if (!step) {
    return
  }
  if (step.file === 'document') {
    markdown.value = step.text
    saveState.value = 'editing'
    void saveContent()
    const restored = blocksInRanges(step.text, placed.value, step.ranges)
    openFoldsOver(restored)
    const keys = restored.length ? rowsCovering(Math.min(...restored), Math.max(...restored)) : []
    selectKeys(keys, keyboard)
    void nextTick(() => {
      if (keys.length) {
        focusBlock(keys[0]!)
      }
    })
  } else {
    notes.value = step.text
    notesState.value = 'editing'
    void saveNotes()
    const range = step.ranges[0] ?? { start: 0, end: 0 }
    const first = blockIndexAt(step.text, range.start)
    const last = blockIndexAt(step.text, Math.max(range.start, range.end - 1))
    void revealNotes(range.start, last - first + 1, false)
  }
  showNote(t(direction === 'undo' ? 'viewer.note.undone' : 'viewer.note.redone'), false)
}

function isTextField(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || target.matches('input, textarea, select'))
}

/** ⌘Z and ⇧⌘Z (Ctrl+Y elsewhere) with focus in the document pane and no editor open. Any
 *  other key is the reader's next action, which a note carrying *Undo* waits for. */
function onPaneKey(event: KeyboardEvent) {
  const undoKey = matches(event, 'mod+z')
  const redoKey = matches(event, 'mod+shift+z') || (!apple && matches(event, 'mod+y'))
  if (!undoKey && !redoKey) {
    if (!['Shift', 'Meta', 'Control', 'Alt'].includes(event.key)) {
      hideUndoNote()
    }
    return
  }
  if (!editable.value || editing.value || isTextField(event.target)) {
    return
  }
  event.preventDefault()
  hideUndoNote()
  stepUndo(undoKey ? 'undo' : 'redo', true)
}

/** The one line at the pane's foot an action with nothing else to show confirms in. */
const statusNote = ref<{ text: string; undo: boolean } | null>(null)
let noteTimer: ReturnType<typeof setTimeout> | undefined
let noteHeld = false

function showNote(text: string, undoable: boolean) {
  statusNote.value = { text, undo: undoable }
  announce(text)
  timeNote()
}

function timeNote() {
  clearTimeout(noteTimer)
  const note = statusNote.value
  if (note && !noteHeld) {
    noteTimer = setTimeout(() => (statusNote.value = null), note.undo ? NOTE_UNDO_MS : NOTE_MS)
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
  if (statusNote.value?.undo) {
    clearTimeout(noteTimer)
    statusNote.value = null
  }
}

/** The viewer's one polite live region for what its notes, the selection and a drag say. */
const liveText = ref('')

function announce(text: string) {
  liveText.value = ''
  void nextTick(() => (liveText.value = text))
}

watch(
  () => (showHint.value ? selectedKeys.value.size : 0),
  (count, before) => {
    if (count && count !== before) {
      announce(t('viewer.select.count', { n: count }, count))
    }
  },
)

// --- dragging blocks ----------------------------------------------------------------------

/** The rows a drag carries, by key, and their run, while it is in the air. */
const dragKeys = ref<ReadonlySet<string>>(new Set())
let dragRun: RowRange | null = null
/** Where the drop would land, and the line that marks it, in the pane's coordinates. */
let dropAt: { target: number; place: 'before' | 'after' } | null = null
const dropLine = ref<{ top: number; left: number; width: number } | null>(null)
let dropSaid = ''
const dragCard = ref<HTMLElement | null>(null)

/**
 * The handle's drag carries its block, or the whole selection when its block is in one
 * (docs/web.md § Editing, *Moving blocks*). An open editor is written back first, so the run
 * is the document as it stands. The drag holds the blocks' markdown as text, so dropped on
 * another text field it inserts what a paste would.
 */
function onHandleDragStart(event: DragEvent, key: string) {
  const transfer = event.dataTransfer
  if (!transfer || !editable.value) {
    event.preventDefault()
    return
  }
  chromePressed = false
  let keys: string[]
  const open = editing.value
  if (open && key === blockKey(open)) {
    const count = draftBlockCount()
    commitEdit()
    keys = rowKeysIn(open.page, open.index, count)
  } else {
    const at = blockKey(afterCommit(parseKey(key)))
    commitEdit()
    keys = selectedKeys.value.has(at) ? [...selectedKeys.value] : [at]
  }
  const range = rangeOf(keys)
  if (!range) {
    event.preventDefault()
    return
  }
  dragRun = range
  dragKeys.value = new Set(rows.value.slice(range.from, range.to + 1).map((row) => row.key))
  transfer.effectAllowed = 'copyMove'
  transfer.setData(BLOCK_DRAG_TYPE, JSON.stringify([...dragKeys.value]))
  transfer.setData('text/plain', rowsMarkdown(range))
  // A small card rather than the browser's picture of the block: written here, since the
  // drag image is taken before the next render.
  const card = dragCard.value
  const head = rowHeads(range)[0]
  if (card && head) {
    const text = card.querySelector('.drag-card-text')
    if (text) {
      text.textContent = blockPlain(head.block) || blockName(head.block, nameText.value)
    }
    transfer.setDragImage(card, 12, 14)
  }
}

/** Image files dragged over the document, where it can hold them: theirs to land between
 *  blocks on the dragged block's drop line (docs/web.md § Editing, *Images*). */
function carriesImageFiles(event: DragEvent): boolean {
  return !dragRun && Boolean(editorImages.value) && carriesFiles(event.dataTransfer)
}

/** Blocks dragged from an answer in the agent panel: a copy to land on the drop line, with no
 *  run of the document's to move (docs/web.md § The agent panel, *An answer is blocks*). */
function carriesAnswerBlocks(event: DragEvent): boolean {
  return !dragRun && editable.value && [...(event.dataTransfer?.types ?? [])].includes(ANSWER_DRAG_TYPE)
}

/** The last `dragover` the pane took a copy for — image files or an answer's blocks: any
 *  other means the pointer left it. */
let paneCopyEvent: DragEvent | null = null

/** `dragover` decides (`app/lib/dnd.ts`): over the pane a block drag, an answer's blocks or
 *  image files are claimed, the pane scrolls near its edges, and the drop line follows the
 *  pointer. */
function onPaneDragOver(event: DragEvent) {
  const images = carriesImageFiles(event)
  const copy = images || carriesAnswerBlocks(event)
  if (!copy && (!dragRun || ![...(event.dataTransfer?.types ?? [])].includes(BLOCK_DRAG_TYPE))) {
    return
  }
  claimDrag(event, images ? 'image' : 'block')
  paneCopyEvent = copy ? event : null
  if (copy) {
    window.addEventListener('pointermove', endCopyDrag, { once: true })
  }
  event.preventDefault()
  setDropEffect(event, copy ? 'copy' : 'move')
  const box = scroller.value
  if (box) {
    const rect = box.getBoundingClientRect()
    if (event.clientY < rect.top + AUTOSCROLL_EDGE_PX) {
      box.scrollTop -= AUTOSCROLL_STEP_PX
    } else if (event.clientY > rect.bottom - AUTOSCROLL_EDGE_PX) {
      box.scrollTop += AUTOSCROLL_STEP_PX
    }
  }
  placeDropLine(event.clientY)
}

/**
 * The drop line, in the gap the pointer is nearest: before or after the hovered row by its
 * vertical middle, a collapsed section one row. A drop inside the moving run, or where it
 * would move nothing, draws no line; files have no run, and land in any gap. A screen reader
 * hears the target as it changes.
 */
function placeDropLine(y: number) {
  const box = scroller.value
  const pane = paneEl.value
  const run = dragRun
  const list = rows.value
  if (!box || !pane || !list.length) {
    return
  }
  const rectAt = (index: number) => box.querySelector(`[data-block="${list[index]!.key}"]`)?.getBoundingClientRect() ?? null
  // The last row whose top is above the pointer: rows run down the pane in order.
  let low = 0
  let high = list.length - 1
  let found = 0
  while (low <= high) {
    const middle = (low + high) >> 1
    const rect = rectAt(middle)
    if (rect && rect.top <= y) {
      found = middle
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  const rect = rectAt(found)
  const after = rect !== null && y >= (rect.top + rect.bottom) / 2
  // The drop goes before row `gap`.
  const gap = after ? found + 1 : found
  if (!rect || (run && gap >= run.from && gap <= run.to + 1)) {
    clearDropLine()
    return
  }
  const above = gap > 0 ? rectAt(gap - 1) : null
  const below = gap < list.length ? rectAt(gap) : null
  const lineY = above && below ? (above.bottom + below.top) / 2 : above ? above.bottom + 10 : below ? below.top - 10 : y
  const column = box.querySelector('.prose')?.getBoundingClientRect()
  const paneRect = pane.getBoundingClientRect()
  dropAt = after ? { target: list[found]!.last, place: 'after' } : { target: list[found]!.first, place: 'before' }
  dropLine.value = {
    top: lineY - paneRect.top,
    left: (column?.left ?? paneRect.left) - paneRect.left,
    width: column?.width ?? paneRect.width,
  }
  const said = `${list[found]!.key}:${after}`
  if (said !== dropSaid) {
    dropSaid = said
    const name = blockName(placed.value[list[found]!.first]!.block, nameText.value)
    announce(t(after ? 'viewer.block.dropAfter' : 'viewer.block.dropBefore', { name }))
  }
}

function clearDropLine() {
  dropAt = null
  dropLine.value = null
}

function onPaneDrop(event: DragEvent) {
  if (carriesImageFiles(event)) {
    event.preventDefault()
    const at = dropAt
    endDrag()
    const files = droppedFiles(event).filter(isImageFile)
    if (files.length) {
      void dropImages(files, at)
    }
    return
  }
  if (carriesAnswerBlocks(event)) {
    event.preventDefault()
    const at = dropAt
    endDrag()
    const blocks = droppedBlocksMarkdown(event.dataTransfer?.getData('text/plain') ?? '')
    const { target, place } = commitDropAt(at)
    if (blocks !== null && landBlocks(blocks, target, place)) {
      showNote(t('viewer.note.blockAdded'), false)
    }
    return
  }
  if (!dragRun || ![...(event.dataTransfer?.types ?? [])].includes(BLOCK_DRAG_TYPE)) {
    return
  }
  event.preventDefault()
  const run = dragRun
  const at = dropAt
  endDrag()
  if (at) {
    blockSelection.value = null
    moveRun(run, at.target, at.place)
  }
}

/** Writes an open editor back and answers where the drop line was in the document that
 *  leaves: the draft may become more blocks or fewer, and a block after it moves by the
 *  difference (`afterCommit`). */
function commitDropAt(at: { target: number; place: 'before' | 'after' } | null): { target: BlockAt | null; place: 'before' | 'after' } {
  const aimed = at ? placed.value[at.target] : undefined
  const open = editing.value
  let target: BlockAt | null = null
  let place = at?.place ?? 'after'
  if (aimed && open && aimed.page === open.page && aimed.index >= open.index && aimed.index < open.index + span.value) {
    // On the open draft's own edge: after its last block, or before its first — or before
    // what follows it, when it becomes no block at all.
    const count = draftBlockCount()
    const after = place === 'after' && count > 0
    target = { page: open.page, index: after ? open.index + count - 1 : open.index }
    place = after ? 'after' : 'before'
  } else if (aimed) {
    target = afterCommit({ page: aimed.page, index: aimed.index })
  }
  commitEdit()
  return { target, place }
}

/** Image files dropped on the document: each uploaded, then all of them inserted where the
 *  line was — at the end without one — as one block action. An open editor is written back
 *  first, and again once the files are in, so no block under an editor moves out from under
 *  it. The line's place is held by the blocks around it (`placeNear`): the reader may edit
 *  while the files upload. */
async function dropImages(files: File[], at: { target: number; place: 'before' | 'after' } | null) {
  const { target, place } = commitDropAt(at)
  const near = placeNear(markdown.value, target, place)
  const id = props.documentId
  const lines: string[] = []
  for (const file of files) {
    const line = await uploadImage(file)
    // The viewer closed, or turned to another document, while it uploaded: nothing lands,
    // and the rest are not sent (docs/web.md § Editing, *Images*).
    if (closed || id !== props.documentId) {
      return
    }
    if (line) {
      lines.push(line)
    }
  }
  commitEdit()
  const found = findPlace(markdown.value, near)
  if (lines.length && landBlocks(lines.join('\n\n'), found.target, found.place)) {
    showNote(t('viewer.note.imagesAdded', { n: lines.length }, lines.length), false)
  }
}

/** Blocks inserted before or after `target` — at the end without one — as one block action,
 *  a fold over them opened. False when nothing was inserted. */
function landBlocks(blocks: string, target: BlockAt | null, place: 'before' | 'after'): boolean {
  const inserted = insertBlocks(markdown.value, target, place, blocks)
  if (!inserted) {
    return false
  }
  applyDocument(inserted.markdown)
  const start = placedIndex.value.get(blockKey(inserted.first))
  if (start !== undefined) {
    openFoldsOver([start])
  }
  return true
}

/** Anything not claimed for a block drag — or, for files, any `dragover` but the pane's own —
 *  takes the line away: the pointer left the pane. */
function onWindowDragOver(event: DragEvent) {
  if (dragRun ? dragClaim(event) !== 'block' : dropLine.value && paneCopyEvent !== event) {
    clearDropLine()
  }
}

/** A copy's drop line outliving its drag — the drag's source taken off the page, as an
 *  answer's block is when the answer is stored, so its `dragend` never reached the window —
 *  goes at the next pointer move, which no drag fires (docs/web.md § Editing, *Moving
 *  blocks*). */
function endCopyDrag() {
  if (!dragRun) {
    endDrag()
  }
}

/** Escape, a drop outside the document, or a reload: the drag is over. */
function endDrag() {
  dragRun = null
  paneCopyEvent = null
  dragKeys.value = new Set()
  dropSaid = ''
  clearDropLine()
  scheduleWaitingImages()
}

// --- the selection toolbar ------------------------------------------------------------------

interface ToolbarState {
  first: ToolbarRect
  last: ToolbarRect
  bounds: ToolbarRect
  translate: boolean
}

const toolbar = ref<ToolbarState | null>(null)
const paneEl = ref<HTMLElement | null>(null)
/** A press in the pane is down: the toolbar waits for its release (docs/web.md § Editing,
 *  *When it shows*). */
let paneHeld = false
let toolbarTimer: ReturnType<typeof setTimeout> | undefined
let toolbarFrame = 0
/** A coarse pointer: the toolbar sits under the selection, where the platform's callout is not. */
const coarse = ref(false)
let coarseQuery: MediaQueryList | null = null

function onCoarseChange(change: MediaQueryListEvent) {
  coarse.value = change.matches
}

function onPanePointerDown(event: PointerEvent) {
  if (event.target instanceof Element && event.target.closest('[role="toolbar"]')) {
    return
  }
  paneHeld = true
  clearTimeout(toolbarTimer)
  toolbar.value = null
}

function onWindowPointerUp() {
  forgetChromePress()
  if (!paneHeld) {
    return
  }
  paneHeld = false
  // After the release's own work: a drag over rendered blocks opens an editor on mouseup.
  void nextTick(() => requestAnimationFrame(readToolbar))
}

function onSelectionChange() {
  if (paneHeld) {
    return
  }
  clearTimeout(toolbarTimer)
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed) {
    toolbar.value = null
    return
  }
  toolbarTimer = setTimeout(readToolbar, TOOLBAR_SETTLE_MS)
}

function screenRect(rect: DOMRect): ToolbarRect {
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
}

/** The key of the block a node is in, inside the document pane. */
function blockOfNode(node: Node): string | null {
  const element = node instanceof Element ? node : node.parentElement
  const block = element?.closest<HTMLElement>('[data-block]')
  return block && scroller.value?.contains(block) ? (block.dataset.block ?? null) : null
}

/** Shows the toolbar over a non-collapsed selection in the pane — in the open editor or over
 *  rendered blocks — or hides it. Files only, and only the Markdown view. */
function readToolbar() {
  const selection = window.getSelection()
  const box = scroller.value
  const pane = paneEl.value
  if (!editable.value || tab.value !== 'markdown' || !selection || selection.isCollapsed || !selection.rangeCount || !box || !pane) {
    toolbar.value = null
    return
  }
  const firstRange = selection.getRangeAt(0)
  const lastRange = selection.getRangeAt(selection.rangeCount - 1)
  if (!box.contains(firstRange.commonAncestorContainer) || !box.contains(lastRange.commonAncestorContainer)) {
    toolbar.value = null
    return
  }
  const first = [...firstRange.getClientRects()].find((rect) => rect.width > 0 && rect.height > 0)
  const last = [...lastRange.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0).pop()
  if (!first || !last) {
    toolbar.value = null
    return
  }
  toolbar.value = {
    first: screenRect(first),
    last: screenRect(last),
    bounds: screenRect(pane.getBoundingClientRect()),
    translate: translateSkill.value && selectionTranslates(selection),
  }
}

/** Whether the selection touches a block `/translate` translates. */
function selectionTranslates(selection: Selection): boolean {
  const open = editing.value
  const start = blockOfNode(selection.getRangeAt(0).startContainer)
  if (open && start === blockKey(open)) {
    const picked = blockEditor?.selectedMarkdown()
    return picked !== null && picked !== undefined && parseResultMarkdown(picked.markdown).some((page) => page.blocks.some(translatable))
  }
  const end = blockOfNode(selection.getRangeAt(selection.rangeCount - 1).endContainer)
  const from = start === null ? undefined : placedIndex.value.get(start)
  const to = end === null ? undefined : placedIndex.value.get(end)
  return from !== undefined && to !== undefined && placed.value.slice(from, to + 1).some((entry) => translatable(entry.block))
}

/** The passage the toolbar acts on, as markdown, and the block it starts in once an open
 *  editor is written back: inside the editor its own serializer over its selection, over
 *  rendered blocks the walk Ctrl+C makes. */
function toolbarPassage(): { markdown: string; at: BlockAt } | null {
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !selection.rangeCount) {
    return null
  }
  const start = blockOfNode(selection.getRangeAt(0).startContainer)
  const open = editing.value
  if (open && start === blockKey(open)) {
    const picked = blockEditor?.selectedMarkdown()
    if (!picked) {
      return null
    }
    // Counted on their own, the blocks before can come to one more than the whole draft holds
    // there — a list right before a list reads as one — so the index stays within the draft.
    const before = parseResultMarkdown(picked.before).reduce((total, page) => total + page.blocks.length, 0)
    return { markdown: picked.markdown, at: { page: open.page, index: open.index + Math.min(before, draftBlockCount() - 1) } }
  }
  const text = selectionMarkdown(selection)
  return text && start ? { markdown: text, at: afterCommit(parseKey(start)) } : null
}

function onToolbarAction(action: 'ask' | 'quote' | 'copy') {
  const passage = toolbarPassage()
  if (!passage) {
    return
  }
  if (action === 'copy') {
    void writeClipboard(passage.markdown, t('viewer.note.copied'))
    return
  }
  toolbar.value = null
  if (action === 'quote') {
    // The excerpt names the section as the document has it with the draft written in.
    const focus = commitOpenEditor(null)
    quoteInNotes(passage.markdown, passage.at)
    if (focus !== null) {
      void nextTick(() => focusBlock(focus))
    }
  } else {
    void askAgent({ selection: passage.markdown }, blockKey(passage.at))
  }
}

/** The toolbar's skill list: every user-invocable skill, `translate` only where the selection
 *  touches something it translates (docs/web.md § Editing, *The selection toolbar*). */
const toolbarSkills = computed((): ToolbarSkill[] =>
  skills.value
    .filter((skill) => skill.name !== 'translate' || toolbar.value?.translate)
    .map((skill) => ({ name: skill.name, description: skill.description, icon: skillIcon(skill.name) })),
)

/** A skill picked from the toolbar's list, with the passage read at the press. */
function onToolbarSkill(name: string) {
  const passage = toolbarPassage()
  if (!passage) {
    return
  }
  toolbar.value = null
  void askAgent({ selection: passage.markdown, command: name, send: true }, blockKey(passage.at))
}

/** The toolbar follows its selection as the pane scrolls. */
function repositionToolbar() {
  if (toolbar.value && !toolbarFrame) {
    toolbarFrame = requestAnimationFrame(() => {
      toolbarFrame = 0
      readToolbar()
    })
  }
}

// --- skills and asking the agent ---------------------------------------------------------------

/** The user-invocable skills, read when the copy becomes editable and as each block or slash
 *  menu opens; the latest read is the one shown. */
const skills = ref<Skill[]>([])
const skillsLoaded = ref(false)
let skillsSeq = 0

async function loadSkills() {
  const seq = ++skillsSeq
  try {
    const { skills: list } = await listSkills()
    if (seq === skillsSeq) {
      skills.value = list.filter((skill) => skill.user_invocable)
      skillsLoaded.value = true
    }
  } catch {
    // The menus keep the list last read; a skill that cannot be listed is not offered.
  }
}

const translateSkill = computed(() => skills.value.some((skill) => skill.name === 'translate'))

/** The slash menu's skills for an editor's text: null until the first read answers, and
 *  `translate` only where the text holds something it translates. */
function slashSkills(text: string): SlashSkill[] | null {
  if (!skillsLoaded.value) {
    return null
  }
  const translates = parseResultMarkdown(text).some((page) => page.blocks.some(translatable))
  return skills.value
    .filter((skill) => skill.name !== 'translate' || translates)
    .map((skill) => ({ name: skill.name, description: skill.description }))
}

/** The agent panel: the reader's view asks it (`ask`) to take a passage, a section or a
 *  command (docs/web.md § The agent panel). */
const chatPanel = ref<{ ask: (request: AskRequest) => Promise<'sent' | 'focused' | 'kept' | 'full'> } | null>(null)

const limits = computed(() => auth.me.value?.limits ?? null)

/** Writes an open editor back for an action that leaves the reader in the document, and gives
 *  where focus then goes: the first block the editor became, else `focusKey`. */
function commitOpenEditor(focusKey: string | null): string | null {
  const open = editing.value
  if (!open) {
    return focusKey
  }
  const count = draftBlockCount()
  commitEdit()
  return rowKeysIn(open.page, open.index, count)[0] ?? focusKey
}

/**
 * Asks the agent (docs/web.md § The agent panel, *Asked from the document*): the column opens,
 * an open editor is written back first, and a sending ask waits for both files' saves — one
 * that fails or meets a 409 leaves the ask in the composer unsent. Focus goes to the composer
 * when the ask waits there; otherwise it stays in the document, on `focusKey` or on the first
 * block an open editor became.
 */
async function askAgent(request: AskRequest, focusKey: string | null) {
  const focus = commitOpenEditor(focusKey)
  agentOpen.value = true
  let next = request
  if (request.send && !(await flushSaves())) {
    next = { ...request, send: false }
  }
  await nextTick()
  const outcome = await chatPanel.value?.ask(next)
  if (outcome === 'full') {
    const n = limits.value?.sections_per_message ?? 0
    showNote(t('viewer.note.sectionsMax', { n }, n), false)
  }
  if (outcome !== 'focused' && focus !== null) {
    focusBlock(focus)
  }
}

/** A skill picked from the slash menu, with the editor's text as the passage. */
function runSkill(name: string, text: string) {
  void askAgent({ selection: text, command: name, send: true }, null)
}

// --- excerpts --------------------------------------------------------------------------------

/**
 * *Quote in notes* (docs/web.md § Notes, *Excerpts*): the passage as an excerpt at the end of
 * the notes' Excerpts section, which is made when there is none — or, when it is there
 * already, that one shown instead. The notes open on it, washed; the reader stays in the
 * document. A block action: the viewer's undo takes it back.
 */
function quoteInNotes(text: string, at: BlockAt) {
  const passage = excerptPassage(text)
  if (passage === null) {
    return
  }
  const heading = sectionOf(outline.value, at)
  const excerpt = excerptMarkdown(passage, heading ? { text: heading.text, slug: heading.slug } : null, t('viewer.excerpt.beginning'))
  const appended = appendExcerpt(notes.value, passage, excerpt, `## ${t('viewer.excerpt.heading')}`)
  if (appended.duplicate) {
    void revealNotes(appended.at, 2, false)
    showNote(t('viewer.note.alreadyInNotes'), false)
    return
  }
  applyNotes(appended.notes)
  void revealNotes(appended.at, 2, false)
}

/** An answer block's *Add to notes* (docs/web.md § The agent panel, *An answer is blocks*):
 *  at the end of the notes as blocks, the notes opened on it, washed. A block action. */
function addToNotes(text: string) {
  const appended = appendBlocks(notes.value, text)
  if (!appended) {
    return
  }
  applyNotes(appended.notes)
  void revealNotes(appended.at, appended.count, false)
}

/** Opens the notes on the blocks starting at `at`, washed once — and with `focus`, the caret
 *  at their start, so what the keyboard reached is read out. */
async function revealNotes(at: number, count: number, focus: boolean) {
  notesOpen.value = true
  await nextTick()
  const index = blockIndexAt(notes.value, at)
  notesEditor.value?.wash(index, Math.max(1, count))
  if (focus) {
    notesEditor.value?.focusBlock(index)
  }
}

/** The excerpts the notes hold, matched against the document: the markers, and how each
 *  section link in the notes is drawn. Matched a beat after the last change to either. */
const excerptMatches = shallowRef<ExcerptMatch[]>([])
let excerptsTimer: ReturnType<typeof setTimeout> | undefined

function matchNow() {
  clearTimeout(excerptsTimer)
  excerptMatches.value = editable.value ? matchExcerpts(notes.value, pages.value, outline.value) : []
  notesEditor.value?.refreshLinks()
}

watch([notes, pages], () => {
  clearTimeout(excerptsTimer)
  excerptsTimer = setTimeout(matchNow, EXCERPTS_DEBOUNCE_MS)
})

/** The backlink markers, by the block each passage ends in. */
const markers = computed(() => excerptMarkers(excerptMatches.value))
const matchByLink = computed(() => new Map(excerptMatches.value.map((match) => [`${match.anchor}\u0000${passageText(match.passage)}`, match])))

/** Each marker's next excerpt: a block quoted by several leads to each in turn. */
const markerTurns = new Map<string, number>()

/** The marker writes an open editor back, then opens the notes on its excerpt; focus stays.
 *  It leads into the notes, not around the document, so it sets no way back. */
function onMarkerClick(key: string) {
  const list = markers.value.get(key)
  commitEdit()
  if (!list?.length) {
    return
  }
  const turn = markerTurns.get(key) ?? 0
  markerTurns.set(key, turn + 1)
  void revealNotes(list[turn % list.length]!.at, 2, false)
}

/** *See the excerpt in notes*, from the block menu. */
function revealExcerpt(key: string, focus: boolean) {
  const match = markers.value.get(key)?.[0]
  if (match) {
    void revealNotes(match.at, 2, focus)
  }
}

/** The link the document now shows what it located for: drawn pressed. */
let pressedLink: SectionLinkTarget | null = null
let pressTimer: ReturnType<typeof setTimeout> | undefined

function pressLink(target: SectionLinkTarget | null) {
  clearTimeout(pressTimer)
  pressedLink = target
  notesEditor.value?.refreshLinks()
  if (target) {
    pressTimer = setTimeout(() => pressLink(null), FLASH_MS + FADE_MS)
  }
}

/** How the notes draw their section links, and what a press on one does: read as the notes
 *  editor is built, so both read the viewer's state when they are called. */
const notesLinks = {
  state: (target: SectionLinkTarget): SectionLinkState => {
    const pressed = pressedLink
    if (pressed && pressed.label === target.label && pressed.anchor === target.anchor && pressed.passage === target.passage) {
      return 'pressed'
    }
    if (target.anchor === '') {
      return 'chip'
    }
    if (target.passage !== null) {
      const match = matchByLink.value.get(`${target.anchor}\u0000${passageText(target.passage)}`)
      if (match) {
        return match.found || match.heading ? 'chip' : 'text'
      }
    }
    return linkResolves(pages.value, outline.value, target) ? 'chip' : 'text'
  },
  locate: (target: SectionLinkTarget) => void locateLink(target),
  rewrite: (markdown: string) => rewriteBlockLinks(markdown, blockLinkAnchor),
}

/**
 * The block anchor an address carries when it is this document's *Copy link to block* — the
 * app's own origin, the files page in any locale, this document — else null (docs/web.md
 * § Notes, *Quotes and section links*). The anchor as the address wrote it, still escaped;
 * empty for the top of the document.
 */
function blockLinkAnchor(address: string): string | null {
  let url: URL
  try {
    url = new URL(address)
  } catch {
    return null
  }
  if (url.origin !== window.location.origin || !/\/files\/?$/.test(url.pathname)) {
    return null
  }
  if (url.searchParams.get('document') !== String(props.documentId)) {
    return null
  }
  // Present but empty is an anchor too: a wordless block before the first heading, the top.
  const block = /[?&]block=([^&#]*)/.exec(url.search)
  return block ? block[1]! : null
}

/**
 * A section link in the notes, followed (docs/web.md § Notes, *Locate*): an excerpt's passage
 * is found by its text within the linked section, then anywhere, and its own text washed; a
 * passage no longer there leads to its heading, washed like a located heading; `#` is the top.
 * An open editor is written back first, a fold holding the target opens, and the jump sets the
 * way back.
 */
async function locateLink(target: SectionLinkTarget) {
  if (!editable.value) {
    return
  }
  if (tab.value !== 'markdown') {
    tab.value = 'markdown'
    await nextTick()
    await new Promise((done) => requestAnimationFrame(done))
  }
  commitEdit()
  await nextTick()
  if (target.anchor.includes('~')) {
    // A block reference: revealed as opening its address does.
    const at = resolveAnchor(pages.value, decodeBlockAnchor(target.anchor), outline.value)
    if (at && placedIndex.value.has(blockKey(at))) {
      setWayBack()
      pressLink(target)
      await revealBlock(at)
      armWayBack()
    }
    return
  }
  const heading = target.anchor === '' ? null : findHeading(outline.value, target.label, target.anchor)
  const found = target.passage === null ? null : locatePassage(pages.value, target.passage, heading)
  const box = scroller.value
  if (!box || (!found && !heading && target.anchor !== '')) {
    return
  }
  setWayBack()
  pressLink(target)
  if (found && target.passage !== null) {
    const start = placedIndex.value.get(blockKey(found.start))
    const end = placedIndex.value.get(blockKey(found.end))
    if (start !== undefined && end !== undefined) {
      openFoldsOver(Array.from({ length: end - start + 1 }, (_, offset) => start + offset))
    }
    await nextTick()
    const range = passageRange(found, target.passage)
    const element = box.querySelector(`[data-block="${blockKey(found.start)}"]`)
    const rect = range?.getBoundingClientRect() ?? element?.getBoundingClientRect()
    if (rect) {
      box.scrollTop += rect.top - box.getBoundingClientRect().top - LOCATE_MARGIN_PX
    }
    if (!range || !highlightPassage(range)) {
      flashBlock(blockKey(found.start))
    }
  } else if (heading) {
    await showHeading(heading, true)
  } else {
    box.scrollTop = 0
  }
  armWayBack()
}

/** The located passage in the rendered blocks: the text walk find makes, each formula read as
 *  its TeX, matched as the matcher matched it (`findPassage`). */
function passageRange(found: { start: BlockAt; end: BlockAt }, passage: string): Range | null {
  const box = scroller.value
  const start = placedIndex.value.get(blockKey(found.start))
  const end = placedIndex.value.get(blockKey(found.end))
  if (!box || start === undefined || end === undefined) {
    return null
  }
  const parts: { node: Node; atom: boolean; text: string }[] = []
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push({ node, atom: false, text: node.textContent ?? '' })
      return
    }
    if (!(node instanceof HTMLElement) || node.matches('[data-chrome], .figure-frame, [hidden]')) {
      return
    }
    if (node.matches('.md-inline-math')) {
      parts.push({ node, atom: true, text: (node.dataset.tex ?? '').replace(/^\$|\$$/g, '') })
    } else if (node.matches('.md-math-block')) {
      parts.push({ node, atom: true, text: (node.dataset.md ?? '').replace(/^\$\$|\$\$$/g, '') })
    } else {
      node.childNodes.forEach(walk)
    }
  }
  for (let index = start; index <= end; index += 1) {
    const element = box.querySelector(`[data-block="${blockKey(placed.value[index]!)}"]`)
    if (element) {
      walk(element)
    }
  }
  const hit = findPassage(
    parts.map((part) => part.text),
    passageText(passage),
  )
  if (!hit) {
    return null
  }
  const from = parts[hit.start.part]!
  const to = parts[hit.end.part]!
  const range = document.createRange()
  if (from.atom) {
    range.setStartBefore(from.node)
  } else {
    range.setStart(from.node, hit.start.offset)
  }
  if (to.atom) {
    range.setEndAfter(to.node)
  } else {
    range.setEnd(to.node, hit.end.offset)
  }
  return range
}

/** The passage washed through the CSS Custom Highlight API, never by rewriting the DOM; false
 *  where the browser has no such API. */
const locateFading = ref(false)
let locateTimer: ReturnType<typeof setTimeout> | undefined

function highlightPassage(range: Range): boolean {
  if (typeof Highlight === 'undefined' || !('highlights' in CSS)) {
    return false
  }
  clearTimeout(locateTimer)
  locateFading.value = false
  CSS.highlights.set('sightread-locate', new Highlight(range))
  locateTimer = setTimeout(() => {
    locateFading.value = true
    locateTimer = setTimeout(() => {
      CSS.highlights.delete('sightread-locate')
      locateFading.value = false
    }, FADE_MS)
  }, FLASH_MS)
  return true
}

// --- the agent panel's view of the document ---------------------------------------

/** The document as rendered, read when the panel needs it rather than handed over whole. */
function documentPages() {
  return pages.value
}

/** The document's current selection as text, for the agent — null when there is none or
 *  it lies outside the document pane. Read off a clone of the range, so each formula goes
 *  as its TeX rather than as KaTeX's glyphs (`selectionText`). */
function documentSelection(): string | null {
  const selection = window.getSelection()
  const box = scroller.value
  if (!selection || selection.isCollapsed || !box || selection.rangeCount === 0) {
    return null
  }
  const range = selection.getRangeAt(0)
  if (!box.contains(range.commonAncestorContainer)) {
    return null
  }
  const text = selectionText(range.cloneContents().childNodes)
  return text || null
}

/** The heading of the section in view: the last one above a third of the way down the pane,
 *  where a reader's eye is. Null above the first heading. */
function currentSection(): { page: number; index: number } | null {
  const box = scroller.value
  if (!box || tab.value !== 'markdown') {
    return null
  }
  const line = box.getBoundingClientRect().top + box.clientHeight / 3
  let found: { page: number; index: number } | null = null
  for (const entry of outline.value) {
    const element = box.querySelector(`[data-block="${entry.page}-${entry.index}"]`)
    if (!element?.getClientRects().length) {
      continue
    }
    if (element.getBoundingClientRect().top > line) {
      break
    }
    found = { page: entry.page, index: entry.index }
  }
  return found
}

/** The block a section link just led to, by `page-index`, washed for a moment; then the one
 *  whose wash is fading. */
const flashed = ref<string | null>(null)
const fading = ref<string | null>(null)
let flashTimer: ReturnType<typeof setTimeout> | null = null

function flashBlock(key: string) {
  if (flashTimer !== null) {
    clearTimeout(flashTimer)
  }
  fading.value = null
  flashed.value = key
  flashTimer = setTimeout(() => {
    fading.value = flashed.value
    flashed.value = null
    flashTimer = setTimeout(() => {
      fading.value = null
      flashTimer = null
    }, FADE_MS)
  }, FLASH_MS)
}

/**
 * Takes the reader to a heading: an open editor holding it is written back first — and the
 * heading found again, since that can move it — a fold holding it opens, and it lands near
 * the pane's top. Washed when a link led there; not when the reader picked it by name.
 */
async function showHeading(entry: OutlineEntry, wash: boolean) {
  let heading: OutlineEntry | null = entry
  const open = editing.value
  if (open && open.page === entry.page && entry.index >= open.index && entry.index < open.index + span.value) {
    commitEdit()
    await nextTick()
    heading = findHeading(outline.value, entry.plain, entry.slug)
  }
  const at = heading ? placedIndex.value.get(blockKey(heading)) : undefined
  if (!heading || at === undefined) {
    return
  }
  openFoldsOver([at])
  await nextTick()
  const box = scroller.value
  const element = box?.querySelector(`[data-block="${blockKey(heading)}"]`)
  if (!box || !element) {
    return
  }
  box.scrollTop += element.getBoundingClientRect().top - box.getBoundingClientRect().top - LOCATE_MARGIN_PX
  if (wash) {
    flashBlock(blockKey(heading))
  }
}

/**
 * An answer's section link, followed (docs/web.md § The agent panel, *Section links*): the
 * heading scrolled near the pane's top and washed for a moment, and the way back set.
 */
async function locateSection(target: { label: string; anchor: string }) {
  if (tab.value !== 'markdown') {
    tab.value = 'markdown'
    await nextTick()
    // After the tab's own watcher has put the reader's old place back.
    await new Promise((done) => requestAnimationFrame(done))
  }
  const heading = findHeading(outline.value, target.label, target.anchor)
  if (!heading) {
    return
  }
  setWayBack()
  await showHeading(heading, true)
  armWayBack()
}

// --- the way back --------------------------------------------------------------------------

/** Where the reader was before the last jump: the block at the pane's top and how far into it,
 *  found again by its source — the jump may have opened a fold above it. `key` is empty for a
 *  place given as a scroll offset alone. */
const wayBack = ref<{ key: string; src: string; offset: number; label: string } | null>(null)
/** Set a frame after the jump's own scroll, so that scroll does not count as the reader's. */
let wayBackArmed = false

function setWayBack() {
  const box = scroller.value
  if (!box) {
    return
  }
  wayBackArmed = false
  const section = currentSection()
  const entry = section ? headingAt.value.get(blockKey(section)) : undefined
  const label = entry ? t('viewer.back.heading', { heading: entry.plain }) : t('viewer.back.top')
  const top = box.getBoundingClientRect().top
  const list = rows.value
  // The first row whose bottom is below the pane's top.
  let low = 0
  let high = list.length - 1
  let found = -1
  while (low <= high) {
    const middle = (low + high) >> 1
    const rect = box.querySelector(`[data-block="${list[middle]!.key}"]`)?.getBoundingClientRect()
    if (rect && rect.bottom > top) {
      found = middle
      high = middle - 1
    } else {
      low = middle + 1
    }
  }
  const row = found === -1 ? null : list[found]!
  const rect = row ? box.querySelector(`[data-block="${row.key}"]`)?.getBoundingClientRect() : undefined
  wayBack.value =
    row && rect
      ? { key: row.key, src: placed.value[row.first]!.block.src, offset: top - rect.top, label }
      : { key: '', src: '', offset: box.scrollTop, label }
}

function armWayBack() {
  requestAnimationFrame(() => requestAnimationFrame(() => (wayBackArmed = true)))
}

/** The block the way back names, found again: by its key where its source still matches, else
 *  the nearest block with that source. */
function wayBackKey(back: { key: string; src: string }): string | null {
  if (!back.key) {
    return null
  }
  const at = placedIndex.value.get(back.key)
  if (at !== undefined && placed.value[at]?.block.src === back.src) {
    return back.key
  }
  let best: string | null = null
  let distance = Number.POSITIVE_INFINITY
  placed.value.forEach((entry, index) => {
    if (entry.block.src === back.src && Math.abs(index - (at ?? 0)) < distance) {
      distance = Math.abs(index - (at ?? 0))
      best = blockKey(entry)
    }
  })
  return best
}

/** The scroll position that restores the way back, or null when its block is gone. */
function wayBackTop(back: { key: string; src: string; offset: number }): number | null {
  const box = scroller.value
  if (!box) {
    return null
  }
  if (!back.key) {
    return back.offset
  }
  const key = wayBackKey(back)
  const element = key ? box.querySelector(`[data-block="${key}"]`) : null
  if (!element) {
    return null
  }
  return box.scrollTop + element.getBoundingClientRect().top - box.getBoundingClientRect().top + back.offset
}

/** The pill, or ⌘[: back to where the jump started, focus on the block there. */
async function goBack() {
  const back = wayBack.value
  const box = scroller.value
  wayBack.value = null
  if (!back || !box) {
    return
  }
  const key = wayBackKey(back)
  const at = key ? placedIndex.value.get(key) : undefined
  if (at !== undefined) {
    openFoldsOver([at])
    await nextTick()
  }
  const top = wayBackTop(back)
  if (top !== null) {
    box.scrollTop = top
  }
  const focusKey = key ?? rows.value[0]?.key
  if (focusKey) {
    scroller.value?.querySelector<HTMLElement>(`[data-block="${focusKey}"]`)?.focus({ preventScroll: true })
  }
}

/** The pill hides once the reader has scrolled back near that place themselves. */
function checkWayBack() {
  const back = wayBack.value
  const box = scroller.value
  if (!back || !wayBackArmed || !box) {
    return
  }
  const top = wayBackTop(back)
  if (top !== null && Math.abs(box.scrollTop - top) < box.clientHeight / 3) {
    wayBack.value = null
  }
}

// --- the outline ---------------------------------------------------------------------------

const paneHeight = ref(0)
/** The room left of the column: what the outline needs beside a heading's own controls. */
const gutter = ref(0)
const paneFoot = ref<{ root: HTMLElement | null } | null>(null)
const footHeight = ref(0)
/** The current section, by outline index. */
const currentHeading = ref<number | null>(null)
let paneObserver: ResizeObserver | null = null
let footObserver: ResizeObserver | null = null

function measureGutter() {
  const pane = paneEl.value
  const page = scroller.value?.querySelector('.page')
  gutter.value = pane && page ? page.getBoundingClientRect().left - pane.getBoundingClientRect().left : 0
}

watch(paneEl, (pane) => {
  paneObserver?.disconnect()
  paneObserver = null
  if (pane) {
    paneObserver = new ResizeObserver(() => {
      paneHeight.value = pane.clientHeight
      measureGutter()
    })
    paneObserver.observe(pane)
  }
})

watch(
  () => paneFoot.value?.root ?? null,
  (root) => {
    footObserver?.disconnect()
    footObserver = null
    if (root) {
      footObserver = new ResizeObserver(() => (footHeight.value = root.offsetHeight))
      footObserver.observe(root)
    }
  },
)

watch(pages, () => {
  void nextTick(() => {
    measureGutter()
    readCurrent()
  })
})

const showOutline = computed(
  () =>
    tab.value === 'markdown' &&
    outline.value.length >= 2 &&
    gutter.value >= (editable.value ? OUTLINE_GUTTER_FILES_PX : OUTLINE_GUTTER_HISTORY_PX),
)

/** The ticks run down to where the pieces at the pane's foot begin, never under them. */
const outlineRoom = computed(() => paneHeight.value - OUTLINE_TOP_PX - (footHeight.value ? footHeight.value + 32 : 24))
const outlinePanelHeight = computed(() => Math.max(0, paneHeight.value - OUTLINE_TOP_PX - 16))

function readCurrent() {
  const section = currentSection()
  currentHeading.value = section ? outline.value.findIndex((entry) => entry.page === section.page && entry.index === section.index) : null
}

/** An outline row: a jump, not washed — the reader picked it by name. */
async function jumpToHeading(index: number) {
  const entry = outline.value[index]
  if (!entry) {
    return
  }
  setWayBack()
  await showHeading(entry, false)
  armWayBack()
}

/** An outline row's ask. An open editor is written back first — Safari gives a pressed button
 *  no focus, so one can still be open — and the heading found again where it then stands, so
 *  the section is read as the reader sees it; a heading the draft took away asks nothing. */
function askAboutHeading(index: number, translate: boolean) {
  const entry = outline.value[index]
  if (!entry) {
    return
  }
  const at = afterCommit(entry)
  const focus = commitOpenEditor(null)
  const heading = outline.value.find((row) => row.page === at.page && row.index === at.index)
  if (heading) {
    const section = { heading: heading.text, text: sectionSource(pages.value, heading.page, heading.index) }
    void askAgent(translate ? { section, command: 'translate', send: true } : { section }, focus)
  } else if (focus !== null) {
    void nextTick(() => focusBlock(focus))
  }
}

let scrollFrame = 0

/** The pane scrolled: the current section, the toolbar's place and the way back are read again,
 *  once a frame. */
function onDocumentScroll() {
  repositionToolbar()
  if (scrollFrame) {
    return
  }
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0
    readCurrent()
    checkWayBack()
  })
}

// --- a block link ---------------------------------------------------------------------------

/**
 * `/files?document=…&block=…`, followed once the copy can be edited — a running parse is
 * followed until it can be: the block near the pane's top, washed, a fold holding it opened,
 * and the way back set to the top of the document (docs/web.md § Files).
 */
let linkFollowed = false

watch(
  editable,
  async (now) => {
    if (!now || linkFollowed || !props.initialBlock) {
      return
    }
    linkFollowed = true
    const at = resolveAnchor(pages.value, props.initialBlock, outline.value)
    if (!at || !placedIndex.value.has(blockKey(at))) {
      return
    }
    wayBackArmed = false
    wayBack.value = { key: '', src: '', offset: 0, label: t('viewer.back.top') }
    await revealBlock(at)
    armWayBack()
  },
  { immediate: true },
)

/** A linked block shown: the fold holding it opened, the pane scrolled to put it near the top,
 *  the block washed (docs/web.md § Files, *A block can be linked to*). */
async function revealBlock(at: BlockAt) {
  const index = placedIndex.value.get(blockKey(at))
  if (index === undefined) {
    return
  }
  openFoldsOver([index])
  await nextTick()
  const box = scroller.value
  const element = box?.querySelector(`[data-block="${blockKey(at)}"]`)
  if (box && element) {
    box.scrollTop += element.getBoundingClientRect().top - box.getBoundingClientRect().top - LOCATE_MARGIN_PX
    flashBlock(blockKey(at))
  }
}

// --- following the shown document ------------------------------------------------------------

/**
 * Whenever the shown document changes — a block action, an edit, an undo, the agent's edit —
 * the folds, remembered and shown alike, follow their headings, a fold an edit landed in
 * opens, and the block selection follows its blocks (docs/web.md § Result viewer, *A fold
 * follows its heading through an edit*). Synchronous, so nothing reads the new document with
 * the old folds. Slugs the document no longer has are forgotten once it has loaded whole.
 */
let shown: { placed: PlacedBlock[]; outline: OutlineEntry[]; editable: boolean } | null = null

watch(
  [placed, outline],
  ([nextPlaced, nextOutline]) => {
    const before = shown
    shown = { placed: nextPlaced, outline: nextOutline, editable: editable.value }
    if (before && before.editable === editable.value) {
      collapsed.value = followFolds(before.placed, before.outline, nextPlaced, nextOutline, collapsed.value)
      keepFolds(followFolds(before.placed, before.outline, nextPlaced, nextOutline, rememberedFolds))
      // A partial result growing page by page is not an edit: only the copy's changes open.
      if (editable.value) {
        openFoldsOver(changedBlocks(before.placed, nextPlaced))
      }
      followSelection(before.placed, nextPlaced)
    }
    if (wholeDocument.value) {
      const slugs = new Set(nextOutline.map((entry) => entry.slug))
      if ([...collapsed.value].some((slug) => !slugs.has(slug))) {
        collapsed.value = new Set([...collapsed.value].filter((slug) => slugs.has(slug)))
      }
      keepFolds(new Set([...rememberedFolds].filter((slug) => slugs.has(slug))))
    }
  },
  { flush: 'sync', immediate: true },
)

// --- column widths -----------------------------------------------------------------------

const columnsBox = ref<HTMLElement | null>(null)
/** The width the document and its columns share, kept current so the columns are clamped
 *  again when the window narrows or the other column opens — not only while dragging. */
const columnsWidth = ref(Number.POSITIVE_INFINITY)
let resizing: { column: 'notes' | 'agent'; x: number; width: number } | null = null
let columnsObserver: ResizeObserver | null = null

watch(columnsBox, (box) => {
  columnsObserver?.disconnect()
  columnsObserver = null
  if (box) {
    columnsObserver = new ResizeObserver(() => {
      columnsWidth.value = box.clientWidth
    })
    columnsObserver.observe(box)
  }
})

watch([columnsWidth, notesOpen, agentOpen], () => {
  setColumnWidth('notes', columnWidth.notes.value)
  setColumnWidth('agent', columnWidth.agent.value)
})

/** The widest a column may be right now: its own cap, and whatever leaves the document
 *  pane its minimum beside the other column. */
function columnMax(column: 'notes' | 'agent'): number {
  const other = column === 'notes' ? 'agent' : 'notes'
  const otherOpen = other === 'notes' ? notesOpen.value : agentOpen.value
  const total = columnsWidth.value
  // Each open column's rule takes 1px of the row (7px wide, overlapping its neighbours).
  const rules = (notesOpen.value ? 1 : 0) + (agentOpen.value ? 1 : 0)
  const room = total - PANE_MIN_WIDTH - rules - (otherOpen ? columnWidth[other].value : 0)
  return Math.max(COLUMN_WIDTHS[column].min, Math.min(COLUMN_WIDTHS[column].max, room))
}

function setColumnWidth(column: 'notes' | 'agent', width: number) {
  columnWidth[column].value = Math.round(Math.min(columnMax(column), Math.max(COLUMN_WIDTHS[column].min, width)))
}

function startResize(event: PointerEvent, column: 'notes' | 'agent') {
  if (event.button !== 0 || !(event.currentTarget instanceof HTMLElement)) {
    return
  }
  event.preventDefault()
  event.currentTarget.setPointerCapture(event.pointerId)
  resizing = { column, x: event.clientX, width: columnWidth[column].value }
}

function onResize(event: PointerEvent) {
  if (resizing) {
    // The column sits right of its rule: dragging the rule left widens it.
    setColumnWidth(resizing.column, resizing.width - (event.clientX - resizing.x))
  }
}

function endResize() {
  if (resizing) {
    chooseWidth(resizing.column)
  }
  resizing = null
}

/** The column's width as it now stands is the reader's choice: kept for the next visit. */
function chooseWidth(column: 'notes' | 'agent') {
  chosenWidth[column] = columnWidth[column].value
  saveLayout()
}

function onResizeKey(event: KeyboardEvent, column: 'notes' | 'agent') {
  const step = event.shiftKey ? 64 : 16
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    setColumnWidth(column, columnWidth[column].value + (event.key === 'ArrowLeft' ? step : -step))
    chooseWidth(column)
  }
}

// --- find in document ------------------------------------------------------

/** The document's scroll region. A plain ref to the DOM: nothing renders from it. */
const scroller = ref<HTMLElement | null>(null)
const findOpen = ref(false)
const find = ref<{ focus: () => void } | null>(null)

async function openFind() {
  findOpen.value = true
  await nextTick()
  find.value?.focus()
}

/**
 * Ctrl/Cmd+F, taken from the browser while this dialog is open. The document is a scroll
 * region inside an overlay, and the native find bar cannot usefully reach into it — it
 * matches text the reader cannot see and scrolls the page behind the dialog. Only on the
 * markdown tab: the JSON tab is a `<pre>` the browser searches perfectly well.
 *
 * And ⌘[ (Ctrl+[ elsewhere), the way back — taken for as long as the Markdown view shows, pill
 * or not: on Apple platforms it is the browser's Back, and a reader who has learned the key
 * must not be thrown off the document by pressing it once too often.
 */
function onDocumentKey(event: KeyboardEvent) {
  if (tab.value !== 'markdown') {
    return
  }
  if (event.key === 'f' && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
    event.preventDefault()
    void openFind()
  } else if (matches(event, 'mod+[')) {
    event.preventDefault()
    if (wayBack.value) {
      void goBack()
    }
  }
}

/** Where the reader was when they left for JSON: the document is remounted on the way back
 *  and would otherwise open at the top. */
let documentScroll = 0

/** The find bar belongs to the document, so leaving the document for the JSON view closes
 *  it rather than leaving it hanging over JSON; an open block is written back first, and
 *  everything the Markdown view adds goes with it. The watcher runs before the DOM changes,
 *  so the old document is still there to measure. */
watch(tab, async (view) => {
  if (view === 'json') {
    documentScroll = scroller.value?.scrollTop ?? 0
    findOpen.value = false
    commitEdit()
    wayBack.value = null
    blockSelection.value = null
    closeOverBlocks()
    return
  }
  await nextTick()
  if (scroller.value) {
    scroller.value.scrollTop = documentScroll
  }
  measureGutter()
})

/** A reload, or leaving the Markdown view, closes what was open over the blocks: the blocks
 *  they named may have moved (docs/web.md § Editing). */
function closeOverBlocks() {
  blockMenu.value = null
  menuHandleKey.value = null
  menuAfter = null
  toolbar.value = null
  endDrag()
}

// --- the header's More menu ------------------------------------------------

const moreMenu = ref<Point | null>(null)
const moreFromKey = ref(false)
/** When the menu last closed itself. The press on More that closes it (a press outside the
 *  menu) is followed by that button's click, which must not open it again. */
let moreClosedAt = 0

/** Anchored under the button's right edge: More sits at the dialog's right, so the menu
 *  opens leftwards under it (`placeMenu` flips it). */
function toggleMore(event: MouseEvent) {
  if (moreMenu.value || performance.now() - moreClosedAt < 200) {
    moreMenu.value = null
    return
  }
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
  // A click the keyboard made carries no pointer detail.
  moreFromKey.value = event.detail === 0
  moreMenu.value = { x: box.right, y: box.bottom + 4 }
}

function closeMore() {
  moreMenu.value = null
  moreClosedAt = performance.now()
}

/** What the parse was: read once, not every time the document is open. */
const parseFacts = computed<MenuFact[]>(() => {
  const result = props.result
  if (!result) {
    return []
  }
  return [
    { label: t('viewer.fact.model'), value: result.meta.model, mono: true },
    { label: t('viewer.fact.pages'), value: String(result.pages.length) },
    { label: t('viewer.fact.bbox'), value: result.meta.bbox_format, mono: true },
  ]
})

/** The two views, as a pair of choices: the JSON view is the contract a developer checks
 *  now and then, not a mode to flip while reading. */
const moreItems = computed<MenuItem[]>(() => [
  {
    key: 'markdown',
    label: t('viewer.view.markdown'),
    checked: tab.value === 'markdown',
    separated: parseFacts.value.length > 0,
  },
  { key: 'json', label: t('viewer.view.json'), checked: tab.value === 'json' },
])

function onMoreSelect(key: string) {
  if (key === 'markdown' || key === 'json') {
    tab.value = key
  }
}

/** The live "x of y pages" line a partial result carries; empty once the job is done. */
const progress = computed(() => {
  const result = props.result
  if (!result || !partial.value) {
    return ''
  }
  const total = result.meta.page_count
  const done = result.meta.pages_done ?? pages.value.length
  return total ? t('viewer.parsing', { done, total }) : t('viewer.parsingSimple')
})

// --- figures ---------------------------------------------------------------

/** Crops that answered 404 — old results, or a crop that failed — drawn as the dashed
 *  placeholder instead of a broken image. */
const failedCrops = reactive(new Set<string>())

/** A partial result can expose a placeholder a beat before the worker has written its
 *  crop (billing is persisted first, docs/parsing.md), so its request 404s. Each new
 *  snapshot forgets past failures and retries — the image appears as soon as the file
 *  exists, instead of staying hidden until the viewer is reopened. */
watch(
  () => props.result,
  () => {
    failedCrops.clear()
  },
)

/** Where a figure's crop is — by its own page, which a move does not change (docs/web.md
 *  § Result viewer) — or an image's file, by the name its line carries; null without a route. */
function pictureAddress(block: ResultBlock): string | null {
  if (block.kind === 'fig') {
    return props.figureBase ? `${props.figureBase}/${block.page}/${block.bbox.join(',')}` : null
  }
  if (block.kind === 'img') {
    return props.imageBase ? `${props.imageBase}/${block.name}` : null
  }
  return null
}

/** The picture to load for a figure or an image block, or null for the placeholder. */
function figureUrl(block: ResultBlock): string | null {
  const address = pictureAddress(block)
  return address && !failedCrops.has(address) ? address : null
}

function onFigureError(block: ResultBlock) {
  const address = pictureAddress(block)
  if (address) {
    failedCrops.add(address)
  }
}

/** What the editors draw a figure or an image with: the same addresses, and the frame's words. */
const editorFigures = computed<RichOptions['figures']>(() => ({
  url: figureUrl,
  pending: { figure: t('viewer.figurePending'), image: t('viewer.imagePending') },
}))

// --- inserted images ---------------------------------------------------------------

/** One file sent as the document's image, its line answered; a refusal — the browser's own
 *  size check, or the server's — is said on the status note (docs/web.md § Editing, *Images*). */
async function uploadImage(file: File): Promise<string | null> {
  const id = props.documentId
  const cap = limits.value?.image_max_bytes
  if (id === null) {
    return null
  }
  if (cap !== undefined && file.size > cap) {
    showNote(t('viewer.imageTooLarge', { size: formatBytes(cap) }), false)
    return null
  }
  try {
    const { path } = await uploadDocumentImage(id, file)
    return imageLine(file.name, path)
  } catch (error) {
    showNote(await resolve(error), false)
    return null
  }
}

/**
 * An image whose editor closed before it uploaded (docs/web.md § Editing, *Images*): it lands
 * in the document, or the notes, where it was to go, found by the blocks around it — not when
 * the viewer has closed or moved on to another document since, which is false: send no more.
 * In the document it waits while another block's editor is open (`waitingImages`).
 */
function landStrayed(file: 'document' | 'notes', id: number, line: string, near: PlaceNear): boolean {
  if (closed || id !== props.documentId) {
    return false
  }
  if (file === 'notes') {
    const next = insertNoteBlocks(notes.value, near, line)
    if (next !== null) {
      applyNotes(next)
    }
    return true
  }
  waitingImages.push({ line, near })
  scheduleWaitingImages()
  return true
}

/** Strayed images on their way into the document, in the order they uploaded: landing one
 *  writes the document back, which would close the editor the reader is typing in, and moves
 *  every block after it a place down, so they wait while anything holds blocks by place. */
const waitingImages: { line: string; near: PlaceNear }[] = []
let waitingTimer: ReturnType<typeof setTimeout> | undefined

/** A task later, not in the tick that closed what held the blocks: the close's own work — a
 *  menu opening on the blocks it read, a drop, focus given back to a block — runs first, on
 *  the document as it was. */
function scheduleWaitingImages() {
  if (!waitingImages.length || waitingTimer !== undefined) {
    return
  }
  waitingTimer = setTimeout(() => {
    waitingTimer = undefined
    if (!closed && !editing.value && !dragRun && !blockMenu.value && !toolbar.value) {
      landWaitingImages()
    }
  }, 0)
}

function landWaitingImages() {
  clearTimeout(waitingTimer)
  waitingTimer = undefined
  const before = placed.value
  const active = document.activeElement
  const focused = active instanceof HTMLElement && scroller.value?.contains(active) ? active.dataset.block : undefined
  let landed = 0
  for (const { line, near } of waitingImages.splice(0)) {
    const { target, place } = findPlace(markdown.value, near)
    if (landBlocks(line, target, place)) {
      landed += 1
    }
  }
  if (!landed) {
    return
  }
  showNote(t('viewer.note.imagesAdded', { n: landed }, landed), false)
  // Blocks are drawn by place, so focus would stay on the place: it goes with its block.
  const at = focused === undefined ? -1 : before.findIndex((entry) => blockKey(entry) === focused)
  const moved = at < 0 ? null : (followBlocks(before, placed.value)[at] ?? null)
  if (moved !== null) {
    const key = blockKey(placed.value[moved]!)
    void nextTick(() => focusBlock(key))
  }
}

watch([editing, blockMenu, toolbar], scheduleWaitingImages)

/** What the editors insert images with: only on Files, where the document has an images route. */
const editorImages = computed(() => {
  const id = props.documentId
  return props.imageBase && id !== null
    ? {
        upload: uploadImage,
        accept: (limits.value?.image_media_types ?? ['image/*']).join(','),
        strayed: (line: string, near: PlaceNear) => landStrayed('document', id, line, near),
      }
    : undefined
})

/** The blocks around the open editor's run, and where it starts among the document's blocks:
 *  where an image goes whose block editor closed holding no other block beside it. */
const openRunNear = computed<PlaceNear | null>(() => {
  const open = editing.value
  if (!open) {
    return null
  }
  const flat = pages.value.flatMap((page, pageIndex) => page.blocks.map((block, index) => ({ src: block.src, pageIndex, index })))
  const found = flat.findIndex((entry) => entry.pageIndex > open.page || (entry.pageIndex === open.page && entry.index >= open.index))
  const start = found === -1 ? flat.length : found
  // A new block's editor holds no block of the document yet.
  const run = flat.filter((entry) => entry.pageIndex === open.page && entry.index >= open.index && entry.index < open.index + span.value).length
  // The neighbour on the editor's own page first: the place stays on it.
  const prefer = flat[start - 1]?.pageIndex !== open.page && flat[start + run]?.pageIndex === open.page ? 'before' : 'after'
  return { after: flat[start - 1]?.src ?? null, before: flat[start + run]?.src ?? null, gap: start, prefer }
})

/** The block editors' images: the editor's place in the document goes with them. */
const blockImages = computed(() => editorImages.value && { ...editorImages.value, outer: openRunNear.value })

const notesImages = computed(() => {
  const id = props.documentId
  return editorImages.value && id !== null
    ? { ...editorImages.value, strayed: (line: string, near: PlaceNear) => landStrayed('notes', id, line, near) }
    : undefined
})

// --- copy as markdown ------------------------------------------------------

/**
 * What the cloned fragment can no longer say about a list selection: the clone drops the
 * range's common ancestor, so an all-inside-one-list selection arrives as bare `li` runs.
 * The live range still knows the list — its kind, and the number the first selected item
 * carries — and hands both to the serializer.
 */
function orphanListContext(range: Range): OrphanListContext | undefined {
  const container = range.commonAncestorContainer
  const element = container instanceof Element ? container : container.parentElement
  const list = element?.closest('.md-list')
  if (!list) {
    return undefined
  }
  if (list.tagName !== 'OL') {
    return { ordered: false, start: 1 }
  }
  const start = Number.parseInt(list.getAttribute('start') ?? '1', 10) || 1
  const startNode = range.startContainer
  const startElement = startNode instanceof Element ? startNode : startNode.parentElement
  const item = startElement?.closest('li')
  const items = Array.from(list.children).filter((child) => child.tagName === 'LI')
  const offset = item ? Math.max(0, items.indexOf(item)) : 0
  return { ordered: true, start: start + offset }
}

/**
 * Firefox reports a table-region selection as one range per selected cell. Serialized
 * range by range that becomes a stack of one-cell tables, so when every range sits in a
 * cell of the same table, the live cells are grouped by their row and serialized as the
 * one table the user actually selected. Any other multi-range selection falls through to
 * the per-range path.
 */
function tableRegionMarkdown(selection: Selection): string | null {
  if (selection.rangeCount < 2) {
    return null
  }
  const cells: Element[] = []
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const node = selection.getRangeAt(index).startContainer
    const element = node instanceof Element ? node : node.parentElement
    const cell = element?.closest('td, th')
    if (!cell) {
      return null
    }
    cells.push(cell)
  }
  const table = cells[0]?.closest('table')
  if (!table || !cells.every((cell) => cell.closest('table') === table)) {
    return null
  }
  // Ranges arrive in document order, so insertion order groups rows top to bottom.
  const rows = new Map<Element, Element[]>()
  for (const cell of cells) {
    const row = cell.closest('tr')
    if (!row) {
      return null
    }
    const group = rows.get(row) ?? []
    group.push(cell)
    rows.set(row, group)
  }
  return cellGroupsToMarkdown([...rows.values()])
}

/**
 * Display-math blocks a selection boundary sits inside. `cloneContents` keeps such a
 * block's element — and its full `data-md` — over truncated KaTeX children, so the
 * serializer must not trust the source for these blocks and copies their surviving glyph
 * text instead. A block the selection swallowed whole leaves both boundaries outside it
 * and is not collected here. Blocks are named by their per-occurrence `data-md-id`, not
 * their source — a document can repeat a formula, and a boundary in one twin must not
 * downgrade the other.
 */
function partialMathBlocks(selection: Selection): Set<string> {
  const partial = new Set<string>()
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const range = selection.getRangeAt(index)
    for (const container of [range.startContainer, range.endContainer]) {
      const element = container instanceof Element ? container : container.parentElement
      const id = element?.closest('.md-math-block')?.getAttribute('data-md-id')
      if (id) {
        partial.add(id)
      }
    }
  }
  return partial
}

/** A selection over the rendered blocks as markdown, tables included (docs/web.md): what
 *  Ctrl+C copies and the selection toolbar reads. Empty when it holds nothing to copy. */
function selectionMarkdown(selection: Selection): string {
  const partialMath = partialMathBlocks(selection)
  const region = tableRegionMarkdown(selection)
  if (region) {
    return region
  }
  const fragments: string[] = []
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const range = selection.getRangeAt(index)
    // A selection confined to a code or math block copies verbatim: the clone is a bare
    // text node whose whitespace the serializer would collapse, and in a <pre> the line
    // breaks and indentation are the content.
    const container = range.commonAncestorContainer
    const element = container instanceof Element ? container : container.parentElement
    if (element?.closest('pre')) {
      fragments.push(range.toString())
      continue
    }
    // A selection wholly inside one KaTeX block never reaches the serializer's
    // `data-md` (the clone drops the block root): selecting every visible glyph is
    // the whole formula and copies as its `$$…$$` source; anything less copies as
    // the glyph text it looks like.
    const mathBlock = element?.closest('.md-math-block')
    if (mathBlock) {
      const glyphs = mathBlock.querySelector('.katex-html')?.textContent ?? ''
      const covered = range.toString().replace(/\s+/g, ' ').trim() === glyphs.replace(/\s+/g, ' ').trim()
      const source = mathBlock.getAttribute('data-md')
      fragments.push(covered && source ? source : range.toString())
      continue
    }
    const markdown = nodesToMarkdown(range.cloneContents().childNodes, orphanListContext(range), partialMath)
    if (markdown) {
      fragments.push(markdown)
    }
  }
  return fragments.join('\n')
}

/** Ctrl+C inside the document copies markdown, tables included (docs/web.md). A copy
 *  from inside an open block editor is the textarea's own, and is left to it. */
function onCopy(event: ClipboardEvent) {
  // A textarea copies its text; an open rich editor has already written markdown.
  if (event.defaultPrevented || event.target instanceof HTMLTextAreaElement) {
    return
  }
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !event.clipboardData) {
    return
  }
  const text = selectionMarkdown(selection)
  if (!text) {
    return
  }
  event.preventDefault()
  event.clipboardData.setData('text/plain', text)
}

const json = computed(() => (props.result ? JSON.stringify(props.result, null, 2) : ''))

/**
 * Pages the pipeline could not transcribe. A job with some pages failed still finishes as
 * `succeeded` and simply omits them from the markdown (docs/jobs.md), so without this the
 * document reads as complete and the only evidence is in the JSON tab.
 */
const failedPages = computed(() => props.result?.errors ?? [])

/** The skeleton while the editable copy is being read for the first time. */
const awaitingContent = computed(() => contentWanted.value && !contentLoaded.value && !contentError.value)

// --- the viewer's lifetime ----------------------------------------------------------------

/** A press anywhere: the reader's next action, which a note carrying *Undo* waited for — and,
 *  outside the viewer's own controls and menus, the end of a block selection. */
function onWindowPointerDown(event: PointerEvent) {
  const target = event.target instanceof Element ? event.target : null
  if (!target || !paneFoot.value?.root?.contains(target)) {
    hideUndoNote()
  }
  if (target?.closest('[data-chrome], [role="menu"]') || paneFoot.value?.root?.contains(target) || blockMenu.value) {
    return
  }
  blockSelection.value = null
}

onMounted(() => {
  // Capture, because the modal's own Escape/Tab handler is bound to `document` too and a
  // child's listener is registered first; capture puts this one ahead of it.
  document.addEventListener('keydown', onDocumentKey, true)
  document.addEventListener('selectionchange', onSelectionChange)
  window.addEventListener('pointerdown', onWindowPointerDown, true)
  window.addEventListener('pointerup', onWindowPointerUp)
  window.addEventListener('pointercancel', forgetChromePress)
  window.addEventListener('dragover', onWindowDragOver)
  window.addEventListener('dragend', endDrag, true)
  coarseQuery = window.matchMedia('(pointer: coarse)')
  coarse.value = coarseQuery.matches
  coarseQuery.addEventListener('change', onCoarseChange)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onDocumentKey, true)
  document.removeEventListener('selectionchange', onSelectionChange)
  window.removeEventListener('pointerdown', onWindowPointerDown, true)
  window.removeEventListener('pointerup', onWindowPointerUp)
  window.removeEventListener('pointercancel', forgetChromePress)
  window.removeEventListener('dragover', onWindowDragOver)
  window.removeEventListener('dragend', endDrag, true)
  window.removeEventListener('pointermove', endCopyDrag)
  coarseQuery?.removeEventListener('change', onCoarseChange)
  columnsObserver?.disconnect()
  paneObserver?.disconnect()
  footObserver?.disconnect()
  cancelAnimationFrame(hoverFrame)
  cancelAnimationFrame(scrollFrame)
  cancelAnimationFrame(toolbarFrame)
  for (const timer of [noteTimer, toolbarTimer, excerptsTimer, pressTimer, locateTimer]) {
    clearTimeout(timer)
  }
  if (flashTimer !== null) {
    clearTimeout(flashTimer)
  }
  if (typeof Highlight !== 'undefined' && 'highlights' in CSS) {
    CSS.highlights.delete('sightread-locate')
  }
})

watch(
  editable,
  (now) => {
    if (now) {
      void loadSkills()
      matchNow()
      // A markdown document opened empty — just made — is opened to be written in.
      if (props.markdownDocument && !markdown.value.trim()) {
        startNewBlock(0)
      }
    }
  },
  { immediate: true },
)

watch(
  contentWanted,
  (wanted) => {
    if (wanted && !contentLoaded.value) {
      void loadContent()
    }
  },
  { immediate: true },
)
</script>

<template>
  <UiModal :title="title" size="full" tall flush @close="emit('close')">
    <template #title>
      <div class="doc-head">
        <h2 class="doc-title" :title="title">{{ title }}</h2>
        <!-- Where the edits are, announced as they move; drawn only when a save failed,
             the one state that needs the reader (docs/web.md § Editing). -->
        <span
          v-if="editable"
          class="save-state"
          :class="unsaved ? 'failed' : 'sr-only'"
          role="status"
          aria-live="polite"
        >
          {{ t(`viewer.status.${unsaved ? 'failed' : statusWord}`) }}
        </span>
      </div>
    </template>

    <template #actions>
      <!-- The switches for the markdown view, each with its word: four bare glyphs read as
           decoration until each one is hovered. None of them means anything over JSON. -->
      <template v-if="tab === 'markdown'">
        <UiButton
          variant="ghost"
          size="sm"
          class="view-toggle"
          :class="{ on: findOpen }"
          :aria-pressed="findOpen ? 'true' : 'false'"
          @click="findOpen ? (findOpen = false) : openFind()"
        >
          <template #icon><UiIcon name="search" /></template>
          {{ t('viewer.findLabel') }}
        </UiButton>
        <template v-if="editable">
          <UiButton
            variant="ghost"
            size="sm"
            class="view-toggle"
            :class="{ on: notesOpen }"
            :aria-pressed="notesOpen ? 'true' : 'false'"
            @click="notesOpen = !notesOpen"
          >
            <template #icon><UiIcon name="notebook" /></template>
            {{ t('viewer.notesLabel') }}
          </UiButton>
          <UiButton
            variant="ghost"
            size="sm"
            class="view-toggle"
            :class="{ on: agentOpen }"
            :aria-pressed="agentOpen ? 'true' : 'false'"
            @click="agentOpen = !agentOpen"
          >
            <template #icon><UiIcon name="message-circle" /></template>
            {{ t('viewer.agentLabel') }}
          </UiButton>
        </template>
        <span class="actions-rule" aria-hidden="true" />
      </template>

      <!-- What the parse was, and the JSON view: read now and then, so one press away
           rather than on the header for good. Both are the result, so More waits for it. -->
      <UiButton
        variant="ghost"
        size="sm"
        icon-only
        class="view-toggle"
        :class="{ on: moreMenu }"
        :label="t('viewer.more')"
        :disabled="!result"
        aria-haspopup="menu"
        :aria-expanded="moreMenu ? 'true' : 'false'"
        @click="toggleMore"
      >
        <template #icon><UiIcon name="ellipsis" /></template>
      </UiButton>
      <UiContextMenu
        v-if="moreMenu"
        :at="moreMenu"
        :items="moreItems"
        :label="t('viewer.more')"
        :header="parseFacts.length ? t('viewer.details') : undefined"
        :facts="parseFacts"
        :focus-first="moreFromKey"
        @select="onMoreSelect"
        @close="closeMore"
      />
    </template>

    <div class="body">
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiBanner v-else-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>
      <UiSkeleton v-else-if="pending" class="state" :rows="5" />

      <!-- The parse result, always: the contract, whatever the copy has become. -->
      <pre v-else-if="tab === 'json'" class="json mono">{{ json }}</pre>

      <UiSkeleton v-else-if="awaitingContent" class="state" :rows="5" />
      <!-- A markdown document is its copy alone: one that cannot be read has nothing
           else to show. -->
      <UiBanner v-else-if="markdownDocument && !editable && contentError" class="state" tone="error">
        {{ contentError }}
      </UiBanner>

      <!-- A result with no markdown has nothing to show; a document emptied by its reader
           has an editor to type into (docs/web.md § Editing). -->
      <UiEmptyState
        v-else-if="!pages.length && !partial && !editable"
        class="state"
        :title="t('viewer.empty')"
        :body="t('viewer.emptyBody')"
      />

      <div v-else class="markdown">
        <!-- What is true of the whole document, not of one page. Always rendered, empty or
             not, because grid auto-placement would otherwise put the pane in this row.
             Banner and progress can hold at once — a page can fail while the rest is still
             parsing, and the failure must not silence the live progress. -->
        <div class="doc-notes">
          <UiBanner v-if="failedPages.length" class="degraded-note" tone="error">
            {{
              t('viewer.degraded', {
                count: failedPages.length,
                pages: failedPages.map((entry) => entry.page).join(', '),
              })
            }}
          </UiBanner>
          <UiBanner v-if="contentError" class="degraded-note" tone="error">{{ contentError }}</UiBanner>
          <UiBanner v-if="saveError" class="degraded-note" tone="error">{{ saveError }}</UiBanner>
          <UiBanner v-if="notesError" class="degraded-note" tone="error">{{ notesError }}</UiBanner>
          <UiBanner v-if="notice" class="degraded-note" tone="warn">
            {{ notice }}
            <template #actions>
              <UiButton variant="ghost" size="xs" icon-only :label="t('common.close')" @click="notice = null">
                <template #icon><UiIcon name="close" /></template>
              </UiButton>
            </template>
          </UiBanner>
          <!-- The parse is still running; pages below are what exists so far
               (docs/api.md § Partial results). -->
          <div v-if="partial" class="parsing-note">
            <UiSpinner />
            <span>{{ progress }}</span>
          </div>
        </div>

        <!-- The document pane and, beside it, the columns the header toggles. -->
        <div ref="columnsBox" class="columns">
          <!-- What floats over the document — the find bar, the outline, the toolbar, the
               pieces at its foot — is positioned against this, never laid out in it. -->
          <div
            ref="paneEl"
            class="pane"
            @keydown.capture="onPaneKey"
            @pointerdown="onPanePointerDown"
            @dragover="onPaneDragOver"
            @drop="onPaneDrop"
          >
            <MdFind
              v-if="findOpen"
              ref="find"
              class="doc-find"
              :scroller="scroller"
              @close="findOpen = false"
              @expand="onFindExpand"
            />

            <div
              ref="scroller"
              class="document"
              :class="{ files: documentId !== null, 'locate-fading': locateFading }"
              @copy="onCopy"
              @mousedown.capture="forgetPress"
              @mouseup="editable && onBlockClick()"
              @pointerdown.capture="onDocumentPointerDown"
              @pointermove="onDocumentPointerMove"
              @pointerleave="onDocumentPointerLeave"
              @focusin="onDocumentFocusIn"
              @focusout="onDocumentFocusOut"
              @scroll="onDocumentScroll"
            >
              <!-- A document with no page at all: one editor, which makes page 1. -->
              <section v-if="editable && !pages.length" class="page">
                <article class="prose">
                  <RichEditor
                    v-if="isEditing(0, 0)"
                    :ref="setBlockEditor"
                    :key="editorKey"
                    :source="editorSource"
                    :caret="editorCaret"
                    :placeholder="t('viewer.blockPlaceholder')"
                    :label="t('viewer.blockSource')"
                    :skills="slashSkills"
                    :figures="editorFigures"
                    :images="blockImages"
                    @change="onEditorChange"
                    @leave="leaveEditor"
                    @slash-open="loadSkills"
                    @run-skill="runSkill"
                  />
                  <button v-else type="button" class="block-add" @click="startNewBlock(0)">
                    {{ t('viewer.blockAdd') }}
                  </button>
                </article>
              </section>
              <section
                v-for="(page, pageIndex) in pages"
                :key="page.page"
                class="page"
              >
                <article class="prose">
                  <!-- Every block is edited in place: clicked, it becomes a rich editor drawn
                       exactly as it was read, caret where the click landed; left, it renders
                       again (docs/web.md § Editing). Read-only without a document. The
                       viewer's own controls among the blocks — the handle, the chevron, the
                       fold note, a backlink marker — are `data-chrome`: copy, find and the
                       agent's selection pass over them. -->
                  <div
                    v-for="({ block, key }, index) in pageBlocks[pageIndex]"
                    :key="index"
                    class="block"
                    :class="{
                      editable: editable && block.kind !== 'comment',
                      open: isEditing(pageIndex, index),
                      flash: flashed === key,
                      'flash-fade': fading === key,
                      selected: selectedKeys.has(key),
                      folded: folds.hidden.has(key),
                      moving: dragKeys.has(key),
                    }"
                    :data-kind="block.kind"
                    :tabindex="blockTabindex(block, pageIndex, index)"
                    :data-block="key"
                    :hidden="isHeldByEditor(pageIndex, index) || folds.hidden.has(key)"
                    @mousedown="editable && onBlockPress($event, pageIndex, index)"
                    @keydown="onBlockKey"
                  >
                    <!-- A div, not a button: Firefox starts no drag from a button. -->
                    <div
                      v-if="block.kind !== 'comment' && handleShown(key)"
                      role="button"
                      class="block-handle"
                      :class="{ on: menuHandleKey === key }"
                      data-chrome
                      tabindex="-1"
                      draggable="true"
                      :aria-label="t('viewer.block.handle', { name: blockName(block, nameText) })"
                      aria-haspopup="menu"
                      :aria-expanded="menuHandleKey === key && blockMenu ? 'true' : 'false'"
                      @pointerdown.stop
                      @mousedown.stop
                      @mouseup.stop
                      @click.stop="onHandleClick($event, key)"
                      @dragstart="onHandleDragStart($event, key)"
                      @dragend="endDrag"
                    >
                      <UiIcon name="grip-vertical" />
                    </div>
                    <button
                      v-if="isHeading(block)"
                      type="button"
                      class="block-chevron"
                      :class="{ shut: folds.counts.has(key) }"
                      data-chrome
                      :tabindex="editable ? -1 : 0"
                      :aria-label="t(folds.counts.has(key) ? 'viewer.fold.expand' : 'viewer.fold.collapse')"
                      :aria-expanded="folds.counts.has(key) ? 'false' : 'true'"
                      @pointerdown.stop
                      @mousedown.stop
                      @mouseup.stop
                      @click.stop="toggleFold(key)"
                    >
                      <UiIcon :name="folds.counts.has(key) ? 'chevron-right' : 'chevron-down'" />
                    </button>
                    <template v-if="isEditing(pageIndex, index)">
                      <RichEditor
                        v-if="RICH_KINDS.has(block.kind)"
                        :ref="setBlockEditor"
                        :key="editorKey"
                        :source="editorSource"
                        :caret="editorCaret"
                        :label="t('viewer.blockSource')"
                        :on-navigate="navigateFrom"
                        :on-merge="mergeBack"
                        :skills="slashSkills"
                        :figures="editorFigures"
                        :images="blockImages"
                        @change="onEditorChange"
                        @leave="leaveEditor"
                        @slash-open="loadSkills"
                        @run-skill="runSkill"
                      />
                      <!-- A figure or an image: the picture stays a picture, its caption is
                           edited as a line of text under it. -->
                      <div v-else-if="block.kind === 'fig' || block.kind === 'img'" class="figure-edit">
                        <MdBlock
                          :block="{ ...block, caption: null }"
                          :figure-url="figureUrl(block)"
                          @figure-error="onFigureError(block)"
                        />
                        <RichEditor
                          :ref="setBlockEditor"
                          :key="editorKey"
                          class="caption-editor"
                          :source="editorSource"
                          :caret="editorCaret"
                          :placeholder="t('viewer.captionPlaceholder')"
                          :label="t('viewer.captionSource')"
                          single-line
                          :on-navigate="navigateFrom"
                          @change="onEditorChange"
                          @leave="leaveEditor"
                        />
                      </div>
                      <!-- A contents page has no rich-text form: its lines, in the prose face. -->
                      <textarea
                        v-else
                        :ref="setLineEditor"
                        v-model="draft"
                        class="block-editor"
                        :rows="lineEditorRows"
                        :aria-label="t('viewer.blockSource')"
                        spellcheck="false"
                        @blur="leaveEditor('blur')"
                        @keydown="onLineEditorKey"
                      />
                    </template>
                    <MdBlock
                      v-else
                      :block="block"
                      :figure-url="figureUrl(block)"
                      @figure-error="onFigureError(block)"
                    />
                    <!-- What a collapsed section hides, named for what pressing it does. -->
                    <button
                      v-if="folds.counts.get(key)"
                      type="button"
                      class="fold-note"
                      data-chrome
                      :aria-label="t('viewer.fold.expandNote', { n: folds.counts.get(key) }, folds.counts.get(key) ?? 0)"
                      @pointerdown.stop
                      @mousedown.stop
                      @mouseup.stop
                      @click.stop="expandFromNote(key)"
                    >
                      {{ t('viewer.fold.note', { n: folds.counts.get(key) }, folds.counts.get(key) ?? 0) }}
                    </button>
                    <!-- A passage the notes quote ends here (docs/web.md § Notes, *Backlink
                         markers*): no tab stop — the block menu is the keyboard's route. -->
                    <button
                      v-if="editable && markers.has(key)"
                      type="button"
                      class="block-marker"
                      data-chrome
                      tabindex="-1"
                      :aria-label="t('viewer.block.excerpt')"
                      :title="t('viewer.block.excerpt')"
                      @pointerdown.stop
                      @mousedown.stop
                      @mouseup.stop
                      @click.stop="onMarkerClick(key)"
                    >
                      <UiIcon name="quote" />
                    </button>
                  </div>
                  <!-- A document emptied of its blocks keeps a way back in (docs/web.md
                       § Editing): an editor past the last block, or the control that opens
                       one. A page a move or a delete emptied draws nothing — the blocks
                       around it are the way in. -->
                  <template v-if="editable && !page.blocks.length && !placed.length">
                    <RichEditor
                      v-if="isEditing(pageIndex, 0)"
                      :ref="setBlockEditor"
                      :key="editorKey"
                      :source="editorSource"
                      :caret="editorCaret"
                      :placeholder="t('viewer.blockPlaceholder')"
                      :label="t('viewer.blockSource')"
                      :skills="slashSkills"
                      :figures="editorFigures"
                      :images="blockImages"
                      @change="onEditorChange"
                      @leave="leaveEditor"
                      @slash-open="loadSkills"
                      @run-skill="runSkill"
                    />
                    <button
                      v-else
                      type="button"
                      class="block-add"
                      @click="startNewBlock(pageIndex)"
                    >
                      {{ t('viewer.blockAdd') }}
                    </button>
                  </template>
                </article>
              </section>

              <!-- Where the next page will land, while the parse is still running. -->
              <section v-if="partial" class="page" aria-hidden="true">
                <UiSkeleton class="page-skeleton" :rows="4" />
              </section>
            </div>

            <ViewerOutline
              v-if="showOutline"
              :entries="outline"
              :current="currentHeading"
              :room="outlineRoom"
              :panel-height="outlinePanelHeight"
              :actions="editable"
              :translate="translateSkill"
              @jump="jumpToHeading"
              @translate="askAboutHeading($event, true)"
              @context="askAboutHeading($event, false)"
            />

            <ViewerToolbar
              v-if="toolbar && tab === 'markdown'"
              :first="toolbar.first"
              :last="toolbar.last"
              :bounds="toolbar.bounds"
              :coarse="coarse"
              :skills="toolbarSkills"
              @ask="onToolbarAction('ask')"
              @quote="onToolbarAction('quote')"
              @copy="onToolbarAction('copy')"
              @skill="onToolbarSkill"
            />

            <!-- Where a dragged block would land. -->
            <div
              v-if="dropLine"
              class="drop-line"
              aria-hidden="true"
              :style="{ top: `${dropLine.top}px`, left: `${dropLine.left}px`, width: `${dropLine.width}px` }"
            />

            <PaneFoot
              v-if="tab === 'markdown'"
              ref="paneFoot"
              :hint="showHint"
              :back="wayBack?.label ?? null"
              :note="statusNote"
              @back="goBack"
              @undo="stepUndo('undo', false)"
              @hold="holdNote"
            />

            <!-- The drag's picture, written as the drag starts; never seen in place. -->
            <div ref="dragCard" class="drag-card" aria-hidden="true">
              <UiIcon name="grip-vertical" />
              <span class="drag-card-text" />
            </div>

            <!-- One polite region for the notes, the selection's count and a drag's target. -->
            <p class="sr-only" role="status" aria-live="polite">{{ liveText }}</p>
          </div>

          <!-- Notes: the same in-place editor over the whole file, always open
               (docs/web.md § Notes). No title row, no toolbar — the column is the notes. -->
          <template v-if="editable && notesOpen">
            <div
              class="split"
              role="separator"
              tabindex="0"
              aria-orientation="vertical"
              :aria-label="t('viewer.resizeNotes')"
              :aria-valuenow="columnWidth.notes.value"
              :aria-valuemin="COLUMN_WIDTHS.notes.min"
              :aria-valuemax="columnMax('notes')"
              @pointerdown="startResize($event, 'notes')"
              @pointermove="onResize"
              @pointerup="endResize"
              @pointercancel="endResize"
              @dblclick="setColumnWidth('notes', COLUMN_WIDTHS.notes.initial); chooseWidth('notes')"
              @keydown="onResizeKey($event, 'notes')"
            />
            <aside
              class="notes"
              :style="{ '--column-width': `${columnWidth.notes.value}px` }"
              :aria-label="t('viewer.notesLabel')"
            >
              <div class="notes-view md-side" @click="onNotesClick">
                <RichEditor
                  ref="notesEditor"
                  variant="notes"
                  :source="notes"
                  :placeholder="t('viewer.notesPlaceholder')"
                  :label="t('viewer.notesLabel')"
                  :links="notesLinks"
                  :figures="editorFigures"
                  :images="notesImages"
                  @change="onNotesChange"
                />
              </div>
            </aside>
          </template>

          <!-- The agent's thread about this document (docs/web.md § The agent panel). -->
          <div
            v-if="editable && agentOpen && documentId !== null"
            class="split"
            role="separator"
            tabindex="0"
            aria-orientation="vertical"
            :aria-label="t('viewer.resizeAgent')"
            :aria-valuenow="columnWidth.agent.value"
            :aria-valuemin="COLUMN_WIDTHS.agent.min"
            :aria-valuemax="columnMax('agent')"
            @pointerdown="startResize($event, 'agent')"
            @pointermove="onResize"
            @pointerup="endResize"
            @pointercancel="endResize"
            @dblclick="setColumnWidth('agent', COLUMN_WIDTHS.agent.initial); chooseWidth('agent')"
            @keydown="onResizeKey($event, 'agent')"
          />
          <aside
            v-if="editable && agentOpen && documentId !== null"
            class="agent"
            :style="{ '--column-width': `${columnWidth.agent.value}px` }"
            :aria-label="t('chat.panel')"
          >
            <ChatPanel
              ref="chatPanel"
              :document-id="documentId"
              :document-selection="documentSelection"
              :document-pages="documentPages"
              :current-section="currentSection"
              @changed="loadContent()"
              @locate="locateSection"
              @to-notes="addToNotes"
            />
          </aside>
        </div>
      </div>
    </div>

    <!-- The block menu and the toolbar's skills: the viewer's own, so a toolbar or a handle
         going away does not take its menu with it. -->
    <UiContextMenu
      v-if="blockMenu"
      :at="blockMenu.at"
      :items="blockMenu.items"
      :label="t('viewer.block.menu')"
      :header="blockMenu.header"
      :focus-first="blockMenu.fromKey"
      @select="onBlockMenuSelect"
      @close="onBlockMenuClose"
    />
  </UiModal>
</template>

<style scoped>
.body {
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}

/* The states that replace the document rather than sitting inside it. */
.state {
  margin: var(--space-6);
}

.doc-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  min-width: 0;
}

.doc-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* Drawn only when a save failed — the danger colour, beside the title; every other state is
   in the live region and nowhere on screen. */
.save-state {
  flex-shrink: 0;
  color: var(--danger);
  font-size: var(--text-xs);
  white-space: nowrap;
}

/* A toggle that is on sits on the rail-active surface, as a pressed nav row does. */
.view-toggle.on {
  background: var(--rail-active);
  color: var(--ink);
}

/* The rule between the document's switches and More: two groups, not one row. */
.actions-rule {
  width: 1px;
  height: var(--space-5);
  margin-inline: var(--space-1);
  background: var(--hair);
}

/* --- Markdown view -------------------------------------------------------- */

/* The head band always has its own row, even when it is empty: a warning about the whole
   document and the live progress note push the pane down rather than scrolling away inside
   it. The find bar sits inside the pane instead (see `.pane`). */
.markdown {
  display: grid;
  /* `minmax(0, 1fr)` and not the implicit `auto` track it would otherwise get: an auto
     column is sized by its content, so the widest line in the document would stretch the
     pane — and with it the dialog — past the window instead of scrolling inside it. */
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto minmax(0, 1fr);
  min-height: 0;
  height: 100%;
}

/* The pane and the two columns, side by side. The columns take their width from the pane
   and never float over it; closing one gives the width back (docs/web.md § Notes). */
.columns {
  display: flex;
  min-width: 0;
  min-height: 0;
}

/* The positioning context for everything that floats over the document. */
.pane {
  position: relative;
  display: grid;
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
}

.degraded-note {
  border-inline: none;
  border-top: none;
}

/* A fact about the whole document — it is still growing. */
.parsing-note {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--text-xs);
}

/* Top right of the pane, over the document's margin. */
.doc-find {
  position: absolute;
  top: var(--space-4);
  right: var(--space-6);
  z-index: 3;
  width: min(400px, calc(100% - var(--space-12)));
}

/* One continuous scroll of the whole document: a reader comparing two pages should not have
   to click between them. The scrollbar is hidden on this region, as on the two columns
   beside it; it still scrolls. The located passage's wash fades by this property (below). */
.document {
  --block-heading: var(--display-lg);
  --block-subheading: var(--display-sm);
  --block-text: var(--text-read);
  --block-text-sm: var(--text-read-sm);

  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--space-7) var(--space-10) var(--space-12);
  scrollbar-width: none;
  transition: --locate-fade var(--duration-slow) var(--ease);
}

.document::-webkit-scrollbar {
  display: none;
}

.document.locate-fading {
  --locate-fade: 1;
}

/* The column, centered in the pane. The page boundary carries no rule and no label: it is
   the grouping the data has, not a division the reader is asked to see, so consecutive pages
   are separated by exactly the gap the blocks inside one page already keep. */
.page {
  width: 70%;
  margin-inline: auto;
}

.page + .page {
  margin-top: 18px;
}

.prose {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* A block is a click target only when there is a document to write to; the cursor says
   so, and nothing else does — a hover fill on every paragraph would make reading noisy.
   It is the positioning context for its own controls, which sit in the gutters beside it.
   Never given a `display`: hidden blocks are `[hidden]`, which an author `display` outranks.
   `--first-line` is the height of its first line, which the handle and the chevron centre
   on, and of its last, beside which a marker sits. */
.block {
  --first-line: calc(var(--block-text) * 1.75);

  position: relative;
  min-width: 0;
}

.block[data-kind='h2'] {
  --first-line: calc(var(--block-heading) * 1.2);
}

.block[data-kind='h3'] {
  --first-line: calc(var(--block-subheading) * 1.3);
}

.block[data-kind='code'],
.block[data-kind='math'] {
  --first-line: calc(var(--block-text-sm) * 1.7);
}

.block[data-kind='table'] {
  --first-line: 32px;
}

.block[data-kind='fig'],
.block[data-kind='img'] {
  --first-line: 24px;
}

/* A heading an answer's section link led to, a located passage's block where the browser
   has no highlights, a block link's block: the accent's wash for a moment, spread past the
   text so it reads as a place rather than a highlight (docs/web.md § The agent panel). The
   wash is a layer behind the block, so the spread moves nothing, and only its background
   changes — in, and back out as the block fades. */
.block.flash,
.block.flash-fade,
.block.selected {
  isolation: isolate;
}

.block.flash::before,
.block.flash-fade::before {
  content: '';
  position: absolute;
  inset: calc(-1 * var(--space-2));
  z-index: -1;
  border-radius: var(--radius);
  background: var(--accent-wash);
  pointer-events: none;
  transition: background var(--duration-slow) var(--ease);
}

.block.flash-fade::before {
  background: transparent;
}

@starting-style {
  .block.flash::before {
    background: transparent;
  }
}

/* Selected whole blocks (docs/web.md § Editing, *Keyboard block selection*): the same kind of
   layer, 4px past the block above and below and 6px to each side, so the text does not move. */
.block.selected:not(.flash, .flash-fade)::before {
  content: '';
  position: absolute;
  inset: -4px -6px;
  z-index: -1;
  border-radius: var(--radius);
  background: var(--block-selected);
  pointer-events: none;
}

/* In the air, the moving blocks stay where they were, faded, so the reader sees what they
   picked up. */
.block.moving {
  opacity: 0.4;
}

.block.editable {
  cursor: text;
}

.block.editable:focus-visible {
  outline-offset: 6px;
}

/* The handle: in the gutter left of the block's first line, 4px from the text (docs/web.md
   § Editing, *The block handle and its menu*). Drawn only where it can be used now. */
.block-handle {
  position: absolute;
  top: calc((var(--first-line) - 24px) / 2);
  left: -28px;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius);
  color: var(--faint);
  cursor: grab;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.block-handle:hover {
  background: var(--rail-active);
}

.block-handle.on {
  background: var(--rail-active);
  color: var(--ink);
}

.block-handle:focus-visible {
  outline-offset: 0;
}

.block-handle > :deep(svg) {
  width: 16px;
  height: 16px;
}

/* A heading's handle sits beyond its chevron, on Files. */
.document.files .block[data-kind='h2'] > .block-handle,
.document.files .block[data-kind='h3'] > .block-handle {
  left: -56px;
}

/* The chevron, in the gutter just left of the heading's text: drawn on hover or focus within
   the heading, always on a touch screen and always while collapsed, so a fold is never
   invisible. Hidden by opacity, so it keeps its place in the tab order on History. */
.block-chevron {
  position: absolute;
  top: calc((var(--first-line) - 24px) / 2);
  left: -28px;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--faint);
  opacity: 0;
  cursor: pointer;
  transition:
    opacity var(--duration-fast) var(--ease),
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.block:hover > .block-chevron,
.block:focus-within > .block-chevron,
.block-chevron.shut,
.block-chevron:focus-visible {
  opacity: 1;
}

.block-chevron:hover {
  background: var(--rail);
  color: var(--ink);
}

.block-chevron > :deep(svg) {
  width: 16px;
  height: 16px;
}

@media (hover: none) {
  .block-chevron {
    opacity: 1;
  }
}

/* Under a collapsed heading: what it hides, as a quiet line that is itself the way back in. */
.fold-note {
  display: block;
  margin-top: var(--space-1);
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--faint);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease);
}

.fold-note:hover {
  color: var(--ink);
}

/* A backlink marker, in the right gutter beside the block's last line. */
.block-marker {
  position: absolute;
  right: -28px;
  bottom: calc((var(--first-line) - 24px) / 2);
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--faint);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.block-marker:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.block-marker > :deep(svg) {
  width: 14px;
  height: 14px;
}

/* Where a dragged block would land: a 2px accent line across the column, with an 8px ring —
   a 2px accent border on paper — at its left end. */
.drop-line {
  position: absolute;
  z-index: 4;
  height: 2px;
  margin-top: -1px;
  background: var(--accent);
  pointer-events: none;
}

.drop-line::before {
  content: '';
  position: absolute;
  top: -3px;
  left: -8px;
  box-sizing: border-box;
  width: 8px;
  height: 8px;
  border: 2px solid var(--accent);
  border-radius: 50%;
  background: var(--paper);
}

/* The drag's picture: the grip and the block's first line on paper, never wider than 280px.
   Off-screen: the browser takes its picture of it as the drag starts. */
.drag-card {
  position: fixed;
  top: -1000px;
  left: -1000px;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  max-width: 280px;
  padding: var(--space-1) var(--space-3) var(--space-1) var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  box-shadow: var(--shadow-menu);
  color: var(--ink);
  font-size: var(--text-sm);
  white-space: nowrap;
  pointer-events: none;
}

.drag-card > :deep(svg) {
  flex: none;
  width: 16px;
  height: 16px;
  color: var(--faint);
}

.drag-card-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* The way back into an emptied document: a quiet text control in the measure a block had. */
.block-add {
  display: block;
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--rule);
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font: inherit;
  text-align: left;
  cursor: text;
}

.block-add:hover,
.block-add:focus-visible {
  color: var(--ink);
  border-color: var(--accent);
}

/* A contents page's lines, in the prose face and the measure the rendered block had: no
   box, no ring — the block is the same block with a caret in it. It grows with its lines
   rather than scrolling. */
.block-editor {
  display: block;
  width: 100%;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--text-read);
  line-height: 1.75;
  resize: none;
}

.block-editor:focus-visible {
  outline: none;
}

/* A figure open for its caption: the crop as it was, the caption editor where the caption
   sits, in the caption's own face. */
.figure-edit {
  text-align: center;
}

.caption-editor {
  --block-text: var(--text-xs);

  margin-top: var(--space-2);
}

.caption-editor :deep(.md-p) {
  color: var(--faint);
  line-height: 1.6;
  text-align: center;
}

/* Where the next page will land while the parse runs. */
.page-skeleton {
  width: 100%;
}

/* --- Column rules ----------------------------------------------------------- */

/* The rule between the document and a side column, and the handle that resizes the column
   (docs/web.md § Column widths): a 7px hit area centred on a 1px line, which thickens to
   the edge colour while it is hovered, dragged or focused. */
.split {
  position: relative;
  z-index: 1;
  flex: 0 0 7px;
  margin-inline: -3px;
  cursor: col-resize;
  touch-action: none;
}

.split::after {
  content: '';
  position: absolute;
  inset-block: 0;
  left: 3px;
  width: 1px;
  background: var(--line);
  transition:
    background var(--duration-fast) var(--ease),
    width var(--duration-fast) var(--ease),
    left var(--duration-fast) var(--ease);
}

.split:hover::after,
.split:active::after,
.split:focus-visible::after {
  left: 2px;
  width: 3px;
  background: var(--edge);
}

.split:focus-visible {
  outline: none;
}

/* --- Notes column --------------------------------------------------------- */

/* The notes at the UI scale on the sunken surface: a margin beside the document, not a
   second document. */
.notes {
  --block-heading: var(--display-2xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);
  --rich-gap: var(--space-3);

  display: flex;
  flex: 0 0 var(--column-width, 320px);
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--paper-sunken);
}

/* The whole column is the notes: a click below the last line still writes. */
.notes-view {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  padding: var(--space-5) var(--space-5) var(--space-8);
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
  cursor: text;
}

.notes-view::-webkit-scrollbar {
  display: none;
}

.notes-view :deep(.md-p),
.notes-view :deep(.md-list) {
  line-height: 1.65;
}

/* --- Agent column --------------------------------------------------------- */

.agent {
  display: flex;
  flex: 0 0 var(--column-width, 360px);
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--paper);
}

/* --- JSON view ------------------------------------------------------------ */

/* The contract, not a document: it keeps its own line breaks and scrolls both ways rather
   than being re-wrapped into something that no longer matches the response. */
.json {
  margin: 0;
  height: 100%;
  overflow: auto;
  padding: var(--space-6);
  background: var(--paper-sunken);
  color: var(--muted);
  line-height: 1.7;
  white-space: pre;
}

/* A percentage column needs a floor: below the shell breakpoint 70% of the pane is a
   gutter wider than the text it frames. The side columns stack under the document there,
   each keeping its own scroll. The gutter is the 40px padding: room for a handle or a
   chevron, not both, so on Files a heading's two take turns in one slot. */
@media (max-width: 900px) {
  .page,
  .page-skeleton {
    width: 100%;
  }

  .columns {
    flex-direction: column;
  }

  .pane {
    flex: 1 1 50%;
  }

  .notes,
  .agent {
    flex: 0 0 45%;
    border-top: 1px solid var(--line);
  }

  .split {
    display: none;
  }

  /* One gutter slot for a heading: its chevron, or its handle while the heading is open in
     its editor, selected, moving or has its menu open — how a touch screen reaches a handle
     anyway (docs/web.md § Result viewer, *Outline, collapse and the way back*). */
  .document.files .block[data-kind='h2'] > .block-handle,
  .document.files .block[data-kind='h3'] > .block-handle {
    left: -28px;
  }

  .document.files .block:is([data-kind='h2'], [data-kind='h3']):not(.open, .selected, .moving) > .block-handle:not(.on) {
    display: none;
  }

  .document.files .block:is(.open, .selected, .moving) > .block-chevron,
  .document.files .block-handle.on ~ .block-chevron {
    display: none;
  }
}

@media (max-width: 640px) {
  /* A phone's header has room for the glyphs, not their words: the words stay the
     buttons' names, out of sight. */
  .view-toggle :deep(.btn-label) {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .doc-find {
    right: var(--space-4);
    width: calc(100% - var(--space-8));
  }

  /* The side padding is the gutter's room: a handle or a chevron on the left, on Files a
     backlink marker on the right. */
  .document {
    padding: var(--space-7) var(--space-8) var(--space-10);
  }
}
</style>

<style>
/* The located passage's wash (docs/web.md § Notes, *Locate*): the find highlight's accent,
   painted through the CSS Custom Highlight API and faded by a registered property the
   document transitions — where the browser animates a highlight's colour; elsewhere the wash
   simply ends. Global: a highlight is named for the whole page, not for a component. */
@property --locate-fade {
  syntax: '<number>';
  inherits: true;
  initial-value: 0;
}

::highlight(sightread-locate) {
  background-color: color-mix(in srgb, var(--find) calc((1 - var(--locate-fade)) * 100%), transparent);
}
</style>
