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
import { parseResultMarkdown, replaceBlockSource, type Bbox, type ResultBlock } from '~/lib/markdown'

/**
 * Module scope, not component state: the rail, the notes column and the agent panel are
 * reading preferences, so opening the next document keeps the choice you just made instead
 * of asking for it again. Deliberately not persisted — they last as long as the page is
 * loaded and no longer, which is the lifetime a preference this small earns.
 */
const railOpen = ref(false)
const notesOpen = ref(false)
const agentOpen = ref(false)

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
 * The markdown tab is one continuous scroll, and **the document itself carries no page
 * labels**: what a reader checks is the transcription, and which page a passage came from is
 * the pipeline's fact rather than the page's content. The grouping is real all the same — a
 * figure's crop is addressed by the page it came from — and it is offered as a rail the
 * header toggles, off by default. Navigation is a thing you ask for; a page number printed
 * through the prose is a thing you cannot decline (docs/web.md § Result viewer).
 *
 * The rail and the find bar **float over** the document rather than sitting beside or above
 * it: opening either leaves every line of text where it was. The notes and agent columns
 * are the opposite — they sit beside the pane and take their width from it, because they
 * are read alongside the document, not over it.
 *
 * Two mechanics keep the rail and the document from fighting each other while both scroll:
 *
 *  - Scrolling sets the active page to the *last* section whose top has passed a reading
 *    line 72px below the pane's top edge — not the first visible one, which flickers between
 *    two pages for the whole length of a page boundary.
 *  - A click sets `scrollTop` from `offsetTop` rather than calling `scrollIntoView`, which
 *    would also scroll the dialog itself inside the viewport, and suppresses the
 *    scroll-driven update briefly so the click's own target wins the race against the smooth
 *    scroll it just started.
 *
 * The element refs are plain variables, not reactive state: they are the DOM, and making
 * them reactive would re-render the document on every scroll frame.
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
    if (takeMarkdown || ((saveState.value === 'saved' || saveState.value === 'failed') && !dirtyEditor)) {
      markdown.value = content.markdown
      revision.value = content.revision
      saveState.value = 'saved'
    }
    if (takeNotes || notesState.value === 'saved' || notesState.value === 'failed') {
      notes.value = content.notes
      notesRevision.value = content.notes_revision
      notesState.value = 'saved'
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
  const snapshot = markdown.value
  try {
    const saved = await putDocumentContent(id, { markdown: snapshot, base_revision: revision.value })
    revision.value = saved.revision
    if (markdown.value === snapshot) {
      saveState.value = 'saved'
    } else {
      scheduleSave()
    }
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 409) {
      // Another tab, or the agent: what is on disk wins, and the reader is told.
      saveState.value = 'saved'
      notice.value = t('viewer.changedUnderneath')
      // Only the file that conflicted; an unsaved notes draft is still the reader's.
      await loadContent('markdown')
    } else {
      saveState.value = 'failed'
      saveError.value = await resolve(error)
    }
  } finally {
    saving = false
  }
}

// --- notes ---------------------------------------------------------------------------

const notesState = ref<SaveState>('saved')
const notesError = ref<string | null>(null)
let notesTimer: ReturnType<typeof setTimeout> | null = null
let savingNotes = false

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
    if (error instanceof ApiRequestError && error.status === 409) {
      notesState.value = 'saved'
      notice.value = t('viewer.notesChanged')
      await loadContent('notes')
    } else {
      notesState.value = 'failed'
      notesError.value = await resolve(error)
    }
  } finally {
    savingNotes = false
  }
}

const notesEditing = ref(false)
const notesEditor = ref<HTMLTextAreaElement | null>(null)
const notesPages = computed(() => (notesEditing.value ? [] : parseResultMarkdown(notes.value)))

async function startNotes() {
  if (!editable.value) {
    return
  }
  notesEditing.value = true
  await nextTick()
  notesEditor.value?.focus()
}

function onNotesInput() {
  scheduleNotesSave()
}

function onNotesKey(event: KeyboardEvent) {
  if (event.key === 'Escape' || (event.key === 'Enter' && (event.ctrlKey || event.metaKey))) {
    // The dialog closes on Escape too; this one is the editor's.
    event.stopPropagation()
    event.preventDefault()
    notesEditing.value = false
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

/** Closing with an edit still in the debounce — or still in an open block — sends it now
 *  rather than losing it. */
onBeforeUnmount(() => {
  commitEdit()
  if (saveTimer !== null) {
    clearTimeout(saveTimer)
    void saveContent()
  }
  if (notesTimer !== null) {
    clearTimeout(notesTimer)
    void saveNotes()
  }
})

// --- block editing -------------------------------------------------------------------

/** The block whose source is open, by position; null while every block is rendered. */
const editing = ref<{ page: number; index: number } | null>(null)
const draft = ref('')
/** The open block's textarea. A plain variable set through a function ref: a `ref`
 *  attribute inside `v-for` collects an array, and there is only ever one of these. */
let editor: HTMLTextAreaElement | null = null

function setEditor(el: Element | ComponentPublicInstance | null) {
  editor = el instanceof HTMLTextAreaElement ? el : null
}

function isEditing(pageIndex: number, blockIndex: number): boolean {
  return editing.value?.page === pageIndex && editing.value.index === blockIndex
}

async function startEdit(pageIndex: number, blockIndex: number) {
  if (!editable.value || isEditing(pageIndex, blockIndex)) {
    return
  }
  // Removing a focused textarea does not reliably blur it, so the open block is closed
  // by hand before the next one opens.
  commitEdit()
  const block = pages.value[pageIndex]?.blocks[blockIndex]
  if (!block) {
    return
  }
  editing.value = { page: pageIndex, index: blockIndex }
  draft.value = block.src
  await nextTick()
  editor?.focus()
}

/** A click that ends a text selection is a selection, not a request to edit. */
function onBlockClick(pageIndex: number, blockIndex: number) {
  const selection = window.getSelection()
  if (selection && !selection.isCollapsed) {
    return
  }
  void startEdit(pageIndex, blockIndex)
}

/** The source of the block under the open editor, or null when none is open. */
function openBlockSource(): string | null {
  const open = editing.value
  if (!open) {
    return null
  }
  return pages.value[open.page]?.blocks[open.index]?.src ?? null
}

/** Renders the block again, and if its source changed, writes it into the document. */
function commitEdit() {
  const open = editing.value
  if (!open) {
    return
  }
  editing.value = null
  const block = pages.value[open.page]?.blocks[open.index]
  if (!block || draft.value === block.src) {
    return
  }
  markdown.value = replaceBlockSource(markdown.value, open.page, open.index, draft.value)
  scheduleSave()
}

function onEditorKey(event: KeyboardEvent) {
  if (event.key === 'Escape' || (event.key === 'Enter' && (event.ctrlKey || event.metaKey))) {
    event.stopPropagation()
    event.preventDefault()
    commitEdit()
  }
}

/** Enough rows for the source plus one to type into — the textarea does not scroll. */
const editorRows = computed(() => {
  const lines = draft.value.split('\n')
  const wrapped = lines.reduce((total, line) => total + Math.max(1, Math.ceil(line.length / 90)), 0)
  return Math.max(2, wrapped + 1)
})

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

// --- the page rail ---------------------------------------------------------

/** The rail's rows. The figure count is computed here rather than carried through the
 *  parser: it is what this list draws, not something the document means. */
const rail = computed(() =>
  pages.value.map((page) => ({
    page: page.page,
    figures: page.blocks.filter((block) => block.kind === 'fig').length,
  })),
)

const activePage = ref(0)
const scroller = ref<HTMLElement | null>(null)
const sections = new Map<number, HTMLElement>()
/** Set while a click-driven scroll is still settling, so `onScroll` does not overrule it. */
let suppressUntil = 0

/** A result that arrives, or is replaced by a partial one's next snapshot, must not leave
 *  the rail pointing at a page the document no longer has. */
watch(
  pages,
  (list) => {
    if (!list.some((page) => page.page === activePage.value)) {
      activePage.value = list[0]?.page ?? 0
    }
  },
  { immediate: true },
)

function registerSection(page: number, el: Element | ComponentPublicInstance | null) {
  if (el instanceof HTMLElement) {
    sections.set(page, el)
  } else {
    sections.delete(page)
  }
}

function goToPage(page: number) {
  const box = scroller.value
  const section = sections.get(page)
  activePage.value = page
  if (!box || !section) {
    return
  }
  suppressUntil = Date.now() + 120
  box.scrollTop = section.offsetTop - box.offsetTop
}

const READING_LINE = 72

function onScroll() {
  const box = scroller.value
  // Nothing reads this while the rail is closed, and the walk is per scroll frame.
  if (!box || !railOpen.value || Date.now() < suppressUntil) {
    return
  }
  const line = box.scrollTop + READING_LINE
  let current = activePage.value
  for (const page of pages.value) {
    const section = sections.get(page.page)
    if (section && section.offsetTop - box.offsetTop <= line) {
      current = page.page
    }
  }
  activePage.value = current
}

// --- find in document ------------------------------------------------------

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

/**
 * The JSON tab replaces the whole body, so coming back mounts a fresh scroller at the top
 * while `activePage` still names wherever the reader had got to — the rail would highlight
 * page 5 over a document showing page 1. The find bar goes the other way: it belongs to the
 * document, so leaving the document closes it rather than leaving it hanging over JSON.
 */
watch(tab, async (view) => {
  if (view === 'json') {
    findOpen.value = false
    commitEdit()
    return
  }
  if (!activePage.value) {
    return
  }
  await nextTick()
  goToPage(activePage.value)
})

const meta = computed(() => {
  const result = props.result
  if (!result) {
    return ''
  }
  return t('viewer.meta', {
    model: result.meta.model,
    pages: result.pages.length,
    bbox: result.meta.bbox_format,
  })
})

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
  if (event.target instanceof HTMLTextAreaElement) {
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
        <h2 class="doc-title">{{ title }}</h2>
        <!-- The one word that says where the edits are (docs/web.md § Editing). -->
        <span
          v-if="editable"
          class="save-state"
          :class="statusWord"
          role="status"
          aria-live="polite"
        >
          {{ t(`viewer.status.${statusWord}`) }}
        </span>
      </div>
      <p v-if="meta" class="doc-meta mono">{{ meta }}</p>
    </template>

    <!-- A segmented pair, not two loose buttons: they are two views of one thing, and the
         shared border is what says so. -->
    <template #actions>
      <!-- The switches for the markdown view. None means anything over the JSON tab. -->
      <template v-if="tab === 'markdown'">
        <UiButton
          variant="ghost"
          size="sm"
          icon-only
          class="rail-toggle"
          :label="t(railOpen ? 'viewer.railHide' : 'viewer.railShow')"
          :aria-pressed="railOpen ? 'true' : 'false'"
          @click="railOpen = !railOpen"
        >
          <template #icon><UiIcon name="panel-left" /></template>
        </UiButton>
        <UiButton
          variant="ghost"
          size="sm"
          icon-only
          :label="t('viewer.find')"
          :aria-pressed="findOpen ? 'true' : 'false'"
          @click="findOpen ? (findOpen = false) : openFind()"
        >
          <template #icon><UiIcon name="search" /></template>
        </UiButton>
        <template v-if="editable">
          <UiButton
            variant="ghost"
            size="sm"
            icon-only
            class="column-toggle"
            :class="{ on: notesOpen }"
            :label="t(notesOpen ? 'viewer.notesHide' : 'viewer.notesShow')"
            :aria-pressed="notesOpen ? 'true' : 'false'"
            @click="notesOpen = !notesOpen"
          >
            <template #icon><UiIcon name="notebook" /></template>
          </UiButton>
          <UiButton
            variant="ghost"
            size="sm"
            icon-only
            class="column-toggle"
            :class="{ on: agentOpen }"
            :label="t(agentOpen ? 'viewer.agentHide' : 'viewer.agentShow')"
            :aria-pressed="agentOpen ? 'true' : 'false'"
            @click="agentOpen = !agentOpen"
          >
            <template #icon><UiIcon name="message-circle" /></template>
          </UiButton>
        </template>
      </template>

      <div class="tabs" role="tablist" :aria-label="t('viewer.views')">
        <button
          v-for="view in (['markdown', 'json'] as const)"
          :key="view"
          type="button"
          role="tab"
          class="tab"
          :class="{ active: tab === view }"
          :aria-selected="tab === view ? 'true' : 'false'"
          @click="tab = view"
        >
          {{ t(`viewer.view.${view}`) }}
        </button>
      </div>
    </template>

    <div class="body">
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiBanner v-else-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>
      <UiSkeleton v-else-if="pending" class="state" :rows="5" />

      <!-- The parse result, always: the contract, whatever the copy has become. -->
      <pre v-else-if="tab === 'json'" class="json mono">{{ json }}</pre>

      <UiSkeleton v-else-if="awaitingContent" class="state" :rows="5" />

      <UiEmptyState
        v-else-if="!pages.length && !partial"
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
        <div class="columns">
          <!-- The rail and the find bar are positioned against this, not laid out in it. -->
          <div class="pane">
            <nav v-if="railOpen" class="page-rail" :aria-label="t('viewer.pagesLabel')">
              <p class="eyebrow sm page-rail-label">{{ t('viewer.pagesLabel') }}</p>
              <button
                v-for="entry in rail"
                :key="entry.page"
                type="button"
                class="page-button"
                :class="{ active: entry.page === activePage }"
                :aria-current="entry.page === activePage ? 'true' : undefined"
                @click="goToPage(entry.page)"
              >
                <span>{{ t('viewer.pageN', { page: entry.page }) }}</span>
                <span v-if="entry.figures" class="page-figures">
                  {{ t('viewer.figureCount', { count: entry.figures }) }}
                </span>
              </button>
            </nav>

            <MdFind
              v-if="findOpen"
              ref="find"
              class="doc-find"
              :scroller="scroller"
              @close="findOpen = false"
            />

            <div ref="scroller" class="document" @scroll="onScroll" @copy="onCopy">
              <section
                v-for="(page, pageIndex) in pages"
                :key="page.page"
                :ref="(el) => registerSection(page.page, el)"
                class="page"
              >
                <article class="prose">
                  <!-- Every block is markdown: clicked, it opens as its source; left, it
                       renders again (docs/web.md § Editing). Read-only without a document. -->
                  <div
                    v-for="(block, index) in page.blocks"
                    :key="index"
                    class="block"
                    :class="{ editable, open: isEditing(pageIndex, index) }"
                    :tabindex="editable && !isEditing(pageIndex, index) ? 0 : undefined"
                    @click="editable && onBlockClick(pageIndex, index)"
                    @keydown.enter.self.prevent="startEdit(pageIndex, index)"
                  >
                    <textarea
                      v-if="isEditing(pageIndex, index)"
                      :ref="setEditor"
                      v-model="draft"
                      class="block-editor mono"
                      :rows="editorRows"
                      :aria-label="t('viewer.blockSource')"
                      spellcheck="false"
                      @blur="commitEdit"
                      @keydown="onEditorKey"
                    />
                    <MdBlock
                      v-else
                      :block="block"
                      :page="page.page"
                      :figure-url="figureUrl(page.page, block)"
                      @figure-error="onFigureError"
                    />
                  </div>
                </article>
              </section>

              <!-- Where the next page will land, while the parse is still running. -->
              <section v-if="partial" class="page" aria-hidden="true">
                <UiSkeleton class="page-skeleton" :rows="4" />
              </section>
            </div>
          </div>

          <!-- Notes: markdown too. Rendered until clicked, source while editing
               (docs/web.md § Notes). No title row, no toolbar — the column is the notes. -->
          <aside v-if="editable && notesOpen" class="notes" :aria-label="t('viewer.notesLabel')">
            <textarea
              v-if="notesEditing"
              ref="notesEditor"
              v-model="notes"
              class="notes-editor mono"
              :aria-label="t('viewer.notesSource')"
              :placeholder="t('viewer.notesPlaceholder')"
              spellcheck="false"
              @input="onNotesInput"
              @blur="notesEditing = false"
              @keydown="onNotesKey"
            />
            <div
              v-else
              class="notes-view"
              tabindex="0"
              :title="t('viewer.notesEdit')"
              @click="startNotes"
              @keydown.enter.self.prevent="startNotes"
            >
              <template v-if="notesPages.some((page) => page.blocks.length)">
                <template v-for="page in notesPages" :key="page.page">
                  <MdBlock
                    v-for="(block, index) in page.blocks"
                    :key="index"
                    :block="block"
                    :page="page.page"
                  />
                </template>
              </template>
              <span v-else class="notes-empty">{{ t('viewer.notesPlaceholder') }}</span>
            </div>
          </aside>

          <!-- The agent's thread about this document (docs/web.md § The agent panel). -->
          <aside v-if="editable && agentOpen && documentId !== null" class="agent" :aria-label="t('chat.panel')">
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

/* Quiet while saved; the accent while something is in motion, because then it is a fact
   about data that has not landed yet. */
.save-state {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-xs);
  white-space: nowrap;
  transition: color var(--duration-fast) var(--ease);
}

.save-state.editing,
.save-state.saving {
  color: var(--accent);
}

.save-state.failed {
  color: var(--danger);
}

.doc-meta {
  margin-top: var(--space-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--faint);
  font-size: var(--text-2xs);
}

/* A column toggle that is on sits on the rail-active surface, as a pressed nav row does. */
.column-toggle.on {
  background: var(--rail-active);
  color: var(--ink);
}

/* --- Tabs ----------------------------------------------------------------- */

/*
 * No `overflow: hidden` to round the children: the focus ring is an outline at a 2px
 * offset, and a clipping ancestor eats most of it on a control that sits flush against the
 * edge. The buttons carry the corners themselves instead — one pixel less than the border's
 * radius, since the border sits outside them.
 */
.tabs {
  display: inline-flex;
  flex-shrink: 0;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
}

.tab {
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: none;
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.tab:first-child {
  border-radius: calc(var(--radius) - 1px) 0 0 calc(var(--radius) - 1px);
}

.tab:last-child {
  border-radius: 0 calc(var(--radius) - 1px) calc(var(--radius) - 1px) 0;
}

.tab + .tab {
  border-left: 1px solid var(--edge);
}

.tab:hover:not(.active) {
  background: var(--rail);
}

.tab.active {
  background: var(--accent);
  color: var(--accent-fg);
}

/* --- Markdown view -------------------------------------------------------- */

/* The head band always has its own row, even when it is empty: a warning about the whole
   document and the live progress note push the pane down rather than scrolling away inside
   it. The rail and the find bar do the opposite — they float (see `.pane`). */
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

/* Over the document, not beside it: the text does not move when the rail opens. At a 70%
   column there is a margin on the left for it to sit in, so on a wide dialog it covers
   nothing but empty page. */
.page-rail {
  position: absolute;
  inset-block: 0;
  left: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 148px;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-4) var(--space-3);
  background: var(--rail);
  border-right: 1px solid var(--line);
  box-shadow: var(--shadow-menu);
}

/* Top right of the pane, clear of the rail whether it is open or not. */
.doc-find {
  position: absolute;
  top: var(--space-4);
  right: var(--space-6);
  z-index: 3;
  width: min(400px, calc(100% - var(--space-12)));
}

.page-rail-label {
  padding: 0 var(--space-2) var(--space-2);
}

.page-button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-shrink: 0;
  height: var(--control-height-sm);
  padding: 0 var(--space-2);
  border: none;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-xs);
  text-align: left;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.page-button:hover:not(.active) {
  background: var(--rail-active);
  color: var(--ink);
}

/* Only the page you are reading is marked in the accent — that is what makes the rail a
   position indicator rather than a list of numbers. */
.page-button.active {
  background: var(--rail-active);
  color: var(--ink);
  font-weight: var(--weight-semibold);
}

.page-figures {
  flex-shrink: 0;
  font-size: var(--text-3xs);
  font-weight: var(--weight-normal);
  color: var(--faint);
}

/* One continuous scroll of the whole document — the rail selects, it does not filter. A
   reader comparing two pages should not have to click between them. The scrollbar is
   hidden on this region, as on the two columns beside it; it still scrolls. */
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

/* The source of one block, in the measure the rendered block had, in the mono face. It
   grows with its lines rather than scrolling: the block is being read as much as typed. */
.block-editor {
  display: block;
  width: 100%;
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink-soft);
  font-size: var(--text-read-sm);
  line-height: 1.6;
  resize: vertical;
}

.block-editor:focus-visible {
  outline: none;
}

/* Where the next page will land while the parse runs. */
.page-skeleton {
  width: 100%;
}

/* KaTeX brings its own faces and sizing; the span only needs to not fight the line. */
.prose :deep(.md-inline-math .katex) {
  font-size: 1.06em;
}

/* An inline code span: the mono face on the sunken surface, sized to sit in the line.
   `pre-wrap`, because its spaces are content — `a  b` must read as it copies. */
.prose :deep(.md-inline-code),
.notes-view :deep(.md-inline-code) {
  padding: 1px var(--space-1);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  font-family: var(--mono);
  font-size: 0.9em;
  white-space: pre-wrap;
}

/* --- Notes column --------------------------------------------------------- */

/* The notes at the UI scale on the sunken surface: a margin beside the document, not a
   second document. */
.notes {
  --block-heading: var(--display-2xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);

  display: flex;
  flex: 0 0 320px;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-left: 1px solid var(--line);
  background: var(--paper-sunken);
}

.notes-view,
.notes-editor {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  margin: 0;
  padding: var(--space-5) var(--space-5) var(--space-8);
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
}

.notes-view::-webkit-scrollbar,
.notes-editor::-webkit-scrollbar {
  display: none;
}

.notes-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  cursor: text;
}

.notes-view:focus-visible {
  outline-offset: -2px;
}

.notes-view :deep(.md-p),
.notes-view :deep(.md-list),
.notes-view :deep(.md-toc) {
  line-height: 1.65;
}

.notes-empty {
  color: var(--faint);
  font-size: var(--text-sm);
}

.notes-editor {
  border: 0;
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.7;
  resize: none;
}

.notes-editor:focus-visible {
  outline: none;
}

.notes-editor::placeholder {
  color: var(--faint);
}

/* --- Agent column --------------------------------------------------------- */

.agent {
  display: flex;
  flex: 0 0 360px;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-left: 1px solid var(--line);
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
    border-left: none;
    border-top: 1px solid var(--line);
  }
}

@media (max-width: 640px) {
  /* No room for a rail over the document on a phone, so its switch goes too — a header this
     narrow has no space for a control that cannot do anything. */
  .page-rail,
  .rail-toggle {
    display: none;
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
