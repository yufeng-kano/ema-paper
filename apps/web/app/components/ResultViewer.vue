<script lang="ts">
// Both script blocks compile into one module, so the imports belong to the block that comes
// first — the whole component reads them, `<script setup>` included.
import type { ComponentPublicInstance } from 'vue'
import {
  ApiRequestError,
  getDocumentContent,
  putDocumentContent,
  putDocumentNotes,
  type JobResult,
} from '~/lib/api'
import { cellGroupsToMarkdown, nodesToMarkdown, type OrphanListContext } from '~/lib/copy'
import {
  appendBlockSource,
  parseResultMarkdown,
  replaceBlockSource,
  type Bbox,
  type ResultBlock,
} from '~/lib/markdown'
import type { MenuFact, MenuItem, Point } from '~/lib/menu'
import type { Caret } from '~/lib/rich-extensions'
import { captionLine } from '~/lib/richdoc'

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
const RICH_KINDS = new Set<ResultBlock['kind']>(['p', 'h2', 'h3', 'list', 'table', 'math', 'code'])
/** The kinds Backspace can join: text blocks, one into the one before. */
const JOINABLE = new Set<ResultBlock['kind']>(['p', 'h2', 'h3'])

/** How long after the last change a save is sent (docs/web.md § Editing). */
const SAVE_DEBOUNCE_MS = 1200

type SaveState = 'saved' | 'editing' | 'saving' | 'failed'
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
 * viewer exactly as it was.
 *
 * The markdown tab is one continuous scroll, and **the document carries no page labels and
 * the viewer no page navigation**: what a reader checks is the transcription, and which page
 * a passage came from is the pipeline's fact rather than the page's content. The grouping is
 * real all the same — a figure's crop is addressed by the page it came from — but a reader
 * finds a passage with Find (docs/web.md § Result viewer).
 *
 * The header is the filename and its controls. What the parse was — model, pages, bbox
 * format — and the JSON view sit in the More menu: read once, not every time the document
 * is open.
 *
 * The find bar **floats over** the document: opening it leaves every line of text where it
 * was. The notes and the agent columns take their width from the document instead, so the
 * column stays centered in whatever room they leave.
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
    /** The library document, when there is one: what makes the viewer editable. */
    documentId?: number | null
  }>(),
  { error: null, errorMessage: null, figureBase: null, documentId: null },
)

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const { resolve } = useApiError()

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

/** The copy exists only once the parse is final: a running job has nothing to edit yet. */
const contentWanted = computed(
  () => props.documentId !== null && props.result !== null && !partial.value && !props.error,
)

let contentSeq = 0

/**
 * Reads the copy. A reload (a 409, an agent edit) replaces the working text wholesale —
 * what is on disk is the truth, and the reader is told. A load while an edit is still
 * unsaved keeps the edit: its save will 409 and that reload will take over.
 */
/** Which unsaved work a reload may overwrite: nothing, one file, or both. */
type Replace = false | 'markdown' | 'notes' | true

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

// --- saving ------------------------------------------------------------------------

const saveState = ref<SaveState>('saved')
const saveError = ref<string | null>(null)
let saveTimer: ReturnType<typeof setTimeout> | null = null
let saving = false
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
 * the moment the first save lands.
 */
async function saveContent() {
  const id = props.documentId
  if (id === null || saving) {
    return
  }
  if (saveTimer !== null) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  saving = true
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
    } else {
      scheduleSave()
    }
  } catch (error) {
    if (error instanceof ApiRequestError && error.type === 'revision_conflict') {
      // Another tab, or the agent: what is on disk wins, and the reader is told. Only
      // this 409: one for a workspace that is recovering, or a revision that could not
      // be recorded, left the draft unsaved, and it stays the reader's to retry.
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
    saving = false
    if (reloadAfterSave) {
      reloadAfterSave = false
      void loadContent()
    }
  }
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

// --- notes ---------------------------------------------------------------------------

const notesState = ref<SaveState>('saved')
const notesError = ref<string | null>(null)
let notesTimer: ReturnType<typeof setTimeout> | null = null
let savingNotes = false
let reloadNotesAfterSave = false

function scheduleNotesSave() {
  notesState.value = 'editing'
  if (notesTimer !== null) {
    clearTimeout(notesTimer)
  }
  notesTimer = setTimeout(() => void saveNotes(), SAVE_DEBOUNCE_MS)
}

async function saveNotes() {
  const id = props.documentId
  if (id === null || savingNotes) {
    return
  }
  if (notesTimer !== null) {
    clearTimeout(notesTimer)
    notesTimer = null
  }
  savingNotes = true
  notesState.value = 'saving'
  notesError.value = null
  const snapshot = notes.value
  try {
    const saved = await putDocumentNotes(id, { text: snapshot, base_revision: notesRevision.value })
    notesRevision.value = saved.revision
    if (notes.value === snapshot) {
      notesState.value = 'saved'
    } else {
      scheduleNotesSave()
    }
  } catch (error) {
    if (error instanceof ApiRequestError && error.type === 'revision_conflict') {
      notesState.value = 'saved'
      notice.value = t('viewer.notesChanged')
      await loadContent('notes')
    } else {
      notesState.value = 'failed'
      notesError.value = await resolve(error)
      retryAfterClose('notes', () => saveNotes())
    }
  } finally {
    savingNotes = false
    if (reloadNotesAfterSave) {
      reloadNotesAfterSave = false
      void loadContent()
    }
  }
}

/** The notes editor's change: the file's new markdown, saved on the debounce. */
function onNotesChange(markdown: string) {
  notes.value = markdown
  scheduleNotesSave()
}

const notesEditor = ref<{ focus: (where?: 'start' | 'end') => void } | null>(null)

/** A click below the last line of the notes still means "write here" — a click inside the
 *  editor, its TeX field included, is the editor's own. */
function onNotesClick(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.rich')) {
    notesEditor.value?.focus('end')
  }
}

watch(
  contentWanted,
  (wanted) => {
    if (wanted && !contentLoaded.value) {
      void loadContent()
    }
  },
  { immediate: true },
)

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

function setLineEditor(el: Element | ComponentPublicInstance | null) {
  lineEditor = el instanceof HTMLTextAreaElement ? el : null
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

/** The first line of a figure block: its placeholder, which the caption editor keeps. */
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
  editing.value = { page: pageIndex, index: blockIndex }
  span.value = count
  joined.value = false
  draft.value = openBlockSource() ?? block.src
  editorSource.value = block.kind === 'fig' ? (block.caption ?? '') : draft.value
  editorCaret.value = caret
  editorKey.value += 1
  if (block.kind === 'toc') {
    await nextTick()
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
 * so Delete, typing or Ctrl+B act on it (docs/web.md § Editing). Only a run of rich-text
 * blocks on one page: a page marker, a contents page and a figure have no place inside one
 * rich editor, so a selection over any of them stays a selection.
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
  if (!blocks.length || !blocks.every((block) => RICH_KINDS.has(block.kind))) {
    return
  }
  void startEdit(from.page, from.index, { from: from.point, to: to.point }, blocks.length)
}

/** The rendered block a selection boundary lies in, and the boundary's point on screen —
 *  read before the editor replaces the rendering, which it draws the same. Null inside the
 *  open editor (its own selection is its own) or where the boundary has no caret box. */
function selectionEdge(node: Node, offset: number) {
  const element = node instanceof Element ? node : node.parentElement
  const block = element?.closest<HTMLElement>('[data-block]')
  if (!block || !scroller.value?.contains(block) || block.classList.contains('open')) {
    return null
  }
  const [page, index] = (block.dataset.block ?? '').split('-').map(Number)
  const caret = document.createRange()
  caret.setStart(node, offset)
  const rect = caret.getClientRects()[0]
  if (page === undefined || index === undefined || !Number.isInteger(page) || !Number.isInteger(index) || !rect) {
    return null
  }
  return { page, index, point: { x: rect.left, y: rect.top + rect.height / 2 } }
}

/** The open editor's markdown, saved on the debounce while the block stays open. A caption
 *  is one line under the figure's placeholder. */
function onEditorChange(markdown: string) {
  const block = openBlock.value
  if (block?.kind === 'fig') {
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

/** Renders the block again, and if its source changed, writes it into the document. */
function commitEdit() {
  if (!editing.value) {
    return
  }
  const next = documentWithDraft()
  editing.value = null
  span.value = 1
  joined.value = false
  if (next !== markdown.value) {
    markdown.value = next
    scheduleSave()
  }
}

/** The nearest block before or after one, across page markers, that a reader can open —
 *  a comment is drawn as nothing and is stepped over. */
function neighbour(pageIndex: number, blockIndex: number, step: -1 | 1): { page: number; index: number } | null {
  let page = pageIndex
  let index = blockIndex + step
  while (page >= 0 && page < pages.value.length) {
    const blocks = pages.value[page]?.blocks ?? []
    if (index < 0) {
      page -= 1
      index = (pages.value[page]?.blocks.length ?? 0) - 1
    } else if (index >= blocks.length) {
      page += 1
      index = 0
    } else if (blocks[index]?.kind === 'comment') {
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
  commitEdit()
  void startEdit(target.page, target.index, direction === 'up' ? 'end' : 'start')
  return true
}

/** Backspace at the very start of a block: join it to the text block before it on the
 *  same page, or — emptied — remove it and continue at the end of the one before. */
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
    // An emptied block is deleted, as leaving it would; the caret goes to the one before.
    draft.value = ''
    commitEdit()
    void startEdit(open.page, open.index - 1, 'end')
    return true
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

function onLineEditorKey(event: KeyboardEvent) {
  if (event.key === 'Escape' || (event.key === 'Enter' && (event.ctrlKey || event.metaKey))) {
    event.stopPropagation()
    event.preventDefault()
    commitEdit()
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

onBeforeUnmount(() => columnsObserver?.disconnect())

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

/** The document's current selection as text, for the agent — null when there is none or
 *  it lies outside the document pane. */
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
  const text = selection.toString().trim()
  return text || null
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
 */
function onFindKey(event: KeyboardEvent) {
  if (event.key !== 'f' || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) {
    return
  }
  if (tab.value !== 'markdown') {
    return
  }
  event.preventDefault()
  void openFind()
}

// Capture, because the modal's own Escape/Tab handler is bound to `document` too and a
// child's listener is registered first; capture puts this one ahead of it.
onMounted(() => document.addEventListener('keydown', onFindKey, true))
onBeforeUnmount(() => document.removeEventListener('keydown', onFindKey, true))

/** Where the reader was when they left for JSON: the document is remounted on the way back
 *  and would otherwise open at the top. */
let documentScroll = 0

/** The find bar belongs to the document, so leaving the document for the JSON view closes
 *  it rather than leaving it hanging over JSON; an open block is written back first. The
 *  watcher runs before the DOM changes, so the old document is still there to measure. */
watch(tab, async (view) => {
  if (view === 'json') {
    documentScroll = scroller.value?.scrollTop ?? 0
    findOpen.value = false
    commitEdit()
    return
  }
  await nextTick()
  if (scroller.value) {
    scroller.value.scrollTop = documentScroll
  }
})

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

function figureKey(page: number, bbox: Bbox): string {
  return `${page}/${bbox.join(',')}`
}

/** The crop to load for a figure block, or null for the placeholder. */
function figureUrl(page: number, block: ResultBlock): string | null {
  if (block.kind !== 'fig' || !props.figureBase || failedCrops.has(figureKey(page, block.bbox))) {
    return null
  }
  return `${props.figureBase}/${figureKey(page, block.bbox)}`
}

function onFigureError(page: number, bbox: Bbox) {
  failedCrops.add(figureKey(page, bbox))
}

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
  const partialMath = partialMathBlocks(selection)
  const region = tableRegionMarkdown(selection)
  const fragments: string[] = []
  if (region) {
    fragments.push(region)
  } else {
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
        const covered =
          range.toString().replace(/\s+/g, ' ').trim() === glyphs.replace(/\s+/g, ' ').trim()
        const source = mathBlock.getAttribute('data-md')
        fragments.push(covered && source ? source : range.toString())
        continue
      }
      const markdown = nodesToMarkdown(
        range.cloneContents().childNodes,
        orphanListContext(range),
        partialMath,
      )
      if (markdown) {
        fragments.push(markdown)
      }
    }
  }
  if (!fragments.length) {
    return
  }
  event.preventDefault()
  event.clipboardData.setData('text/plain', fragments.join('\n'))
}

const json = computed(() => (props.result ? JSON.stringify(props.result, null, 2) : ''))

/**
 * Pages the pipeline could not transcribe. A job with some pages failed still finishes as
 * `succeeded` and simply omits them from the markdown (docs/jobs.md), so without this the
 * document reads as complete and the only evidence is in the JSON tab.
 */
const failedPages = computed(() => props.result?.errors ?? [])

/** The skeleton while the editable copy is being read for the first time. */
const awaitingContent = computed(
  () => contentWanted.value && !contentLoaded.value && !contentError.value,
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
          <!-- The find bar is positioned against this, not laid out in it. -->
          <div class="pane">
            <MdFind
              v-if="findOpen"
              ref="find"
              class="doc-find"
              :scroller="scroller"
              @close="findOpen = false"
            />

            <div ref="scroller" class="document" @copy="onCopy" @mousedown.capture="forgetPress" @mouseup="editable && onBlockClick()">
              <!-- A document with no page at all: one editor, which makes page 1. -->
              <section v-if="editable && !pages.length" class="page">
                <article class="prose">
                  <RichEditor
                    v-if="isEditing(0, 0)"
                    :key="editorKey"
                    :source="editorSource"
                    :caret="editorCaret"
                    :placeholder="t('viewer.blockPlaceholder')"
                    :label="t('viewer.blockSource')"
                    @change="onEditorChange"
                    @leave="commitEdit"
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
                       again (docs/web.md § Editing). Read-only without a document. -->
                  <div
                    v-for="(block, index) in page.blocks"
                    :key="index"
                    class="block"
                    :class="{ editable: editable && block.kind !== 'comment', open: isEditing(pageIndex, index) }"
                    :tabindex="editable && block.kind !== 'comment' && !isEditing(pageIndex, index) ? 0 : undefined"
                    :data-block="`${pageIndex}-${index}`"
                    :hidden="isHeldByEditor(pageIndex, index)"
                    @mousedown="editable && onBlockPress($event, pageIndex, index)"
                    @keydown.enter.self.prevent="startEdit(pageIndex, index)"
                  >
                    <template v-if="isEditing(pageIndex, index)">
                      <RichEditor
                        v-if="RICH_KINDS.has(block.kind)"
                        :key="editorKey"
                        :source="editorSource"
                        :caret="editorCaret"
                        :label="t('viewer.blockSource')"
                        :on-navigate="navigateFrom"
                        :on-merge="mergeBack"
                        @change="onEditorChange"
                        @leave="commitEdit"
                      />
                      <!-- A figure: the image stays an image, its caption is edited as a
                           line of text under it. -->
                      <div v-else-if="block.kind === 'fig'" class="figure-edit">
                        <MdBlock
                          :block="{ ...block, caption: null }"
                          :page="page.page"
                          :figure-url="figureUrl(page.page, block)"
                          @figure-error="onFigureError"
                        />
                        <RichEditor
                          :key="editorKey"
                          class="caption-editor"
                          :source="editorSource"
                          :caret="editorCaret"
                          :placeholder="t('viewer.captionPlaceholder')"
                          :label="t('viewer.captionSource')"
                          single-line
                          :on-navigate="navigateFrom"
                          @change="onEditorChange"
                          @leave="commitEdit"
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
                        @blur="commitEdit"
                        @keydown="onLineEditorKey"
                      />
                    </template>
                    <MdBlock
                      v-else
                      :block="block"
                      :page="page.page"
                      :figure-url="figureUrl(page.page, block)"
                      @figure-error="onFigureError"
                    />
                  </div>
                  <!-- A page emptied of its blocks keeps a way back in (docs/web.md
                       § Editing): an editor past the last block, or the control that
                       opens one. -->
                  <template v-if="editable && !page.blocks.length">
                    <RichEditor
                      v-if="isEditing(pageIndex, 0)"
                      :key="editorKey"
                      :source="editorSource"
                      :caret="editorCaret"
                      :placeholder="t('viewer.blockPlaceholder')"
                      :label="t('viewer.blockSource')"
                      @change="onEditorChange"
                      @leave="commitEdit"
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
              :document-id="documentId"
              :document-selection="documentSelection"
              @changed="loadContent()"
            />
          </aside>
        </div>
      </div>
    </div>
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

/* The positioning context for the find bar. */
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
   beside it; it still scrolls. */
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
}

.document::-webkit-scrollbar {
  display: none;
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
   so, and nothing else does — a hover fill on every paragraph would make reading noisy. */
.block {
  min-width: 0;
}

.block.editable {
  cursor: text;
}

.block.editable:focus-visible {
  outline-offset: 6px;
}

/* The way back into an emptied page: a quiet text control in the measure a block had. */
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
   each keeping its own scroll. */
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

  .document {
    padding: var(--space-7) var(--space-5) var(--space-10);
  }
}
</style>
