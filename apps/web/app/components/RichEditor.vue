<script setup lang="ts">
/**
 * Markdown edited in place, the way Typora or Notion edit it (docs/web.md § Editing).
 *
 * A TipTap editor over the schema in `app/lib/rich-extensions.ts`, loaded from and written
 * back to markdown by `app/lib/richdoc.ts` — never by a markdown parser of its own. It draws
 * with the same `md-*` classes as the rendered block, so the block a reader clicks does not
 * move when it becomes editable.
 *
 * Two callers. The document pane opens one per clicked block (`variant="block"`): it puts
 * the caret where the click landed, and hands the edges of the block back to the viewer —
 * leaving it, arrowing past its first or last line, Backspace at its very start — because
 * only the viewer knows the blocks around it. The notes column keeps one open over the
 * whole file (`variant="notes"`), where section links are read and drawn as chips.
 *
 * Formulas stay typeset while they are edited: a click on one opens a small floating TeX
 * field under it, and every keystroke re-renders the formula above.
 *
 * `/` in an empty top-level paragraph opens the slash menu (docs/web.md § Editing, *The slash
 * menu*): a listbox under the slash, inside the editor's root as the TeX field is, so focus
 * never leaves the text — the editor names its highlighted row as its active descendant. Its
 * rows turn the paragraph into another block, or — in the document's block editor, given
 * `skills` — run a skill on the editor's text, which the viewer sends.
 */
import { Editor } from '@tiptap/core'
import { Slice, type Node as ProseNode } from '@tiptap/pm/model'
import { NodeSelection, Selection, TextSelection, type Transaction } from '@tiptap/pm/state'
import { matchCommands } from '~/lib/composer'
import { BLOCK_DRAG_TYPE, setDropEffect } from '~/lib/dnd'
import { droppedBlocksMarkdown } from '~/lib/excerpts'
import type { IconName } from '~/lib/icons'
import {
  richExtensions,
  washKey,
  type Caret,
  type LeaveReason,
  type MathTarget,
  type RichOptions,
  type TextPoint,
  type WashMeta,
} from '~/lib/rich-extensions'
import { docToMarkdown, markdownToDoc, sliceToMarkdown } from '~/lib/richdoc'
import {
  SLASH_CONVERSIONS,
  matchSlash,
  opensSlash,
  skillIcon,
  slashQuery,
  type SlashConversion,
  type SlashSkill,
} from '~/lib/slash'

const props = withDefaults(
  defineProps<{
    /** The markdown to load. Read once for a block; followed for notes while nothing in
     *  the editor is unsaved. */
    source: string
    variant?: 'block' | 'notes'
    caret?: Caret | null
    placeholder?: string
    /** A figure caption is one line: Enter leaves instead of starting a paragraph. */
    singleLine?: boolean
    label?: string
    /** The ids of what describes the text (a note, a count), as `aria-describedby`. */
    describedBy?: string
    /** False holds the text as it is — still selectable, never changed — while the caller
     *  saves it. */
    editable?: boolean
    /** Arrow past the first or last line. False keeps the key for the editor. */
    onNavigate?: (direction: 'up' | 'down') => boolean
    /** Backspace at the very start, with the editor's current markdown. False keeps the key. */
    onMerge?: (markdown: string) => boolean
    /** Section links are read (the notes): how each is drawn, and what a press on a chip
     *  does. Read once, as the editor is built. */
    links?: RichOptions['links']
    /** The skills the slash menu offers to run on this editor's text — the document's block
     *  editor: given that text, the slash line left out, the skills to list, or null while
     *  they load. Without it the menu has the conversions only. */
    skills?: (text: string) => SlashSkill[] | null
  }>(),
  {
    variant: 'block',
    caret: null,
    placeholder: '',
    singleLine: false,
    label: undefined,
    describedBy: undefined,
    editable: true,
    onNavigate: undefined,
    onMerge: undefined,
    links: undefined,
    skills: undefined,
  },
)

const emit = defineEmits<{
  /** The editor's markdown, after a change the reader made. Never on load. */
  change: [markdown: string]
  /** The reader left: Escape, focus going somewhere outside the editor, or Enter in a
   *  one-line editor. */
  leave: [reason: LeaveReason]
  /** The slash menu opened with its skills section: they are read again. */
  'slash-open': []
  /** A skill picked from the slash menu, with the editor's markdown, the slash line gone. */
  'run-skill': [name: string, markdown: string]
}>()

const { t } = useI18n()

const root = ref<HTMLElement | null>(null)
const mount = ref<HTMLElement | null>(null)
let editor: Editor | null = null
/** Set as the editor is torn down: removing a focused editor can blur it, and that blur is
 *  the viewer moving on, not the reader leaving. */
let closing = false
/** What this editor last emitted, so a notes reload of the same text is not a reload. */
let emitted: string | null = null
/** What the file ended with (a final newline, say): kept when the file is written back. */
let trailing = /\n\s*$/.exec(props.source)?.[0] ?? ''

/** Section links are read where links are drawn — the notes. */
const inlineOptions = props.links ? { cites: true } : undefined

/** The nearest ancestor that scrolls: where a wash scrolls to, and the room the slash menu
 *  has below its line. */
function scrollParent(element: HTMLElement | null): HTMLElement | null {
  let parent = element?.parentElement ?? null
  while (parent && !/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) {
    parent = parent.parentElement
  }
  return parent
}

// --- the TeX field -------------------------------------------------------------------

const math = ref<{ pos: number; display: boolean; latex: string; top: number; left: number } | null>(null)
const mathInput = ref<HTMLInputElement | HTMLTextAreaElement | null>(null)

async function openMath(target: MathTarget) {
  const box = root.value
  if (!box || !props.editable) {
    return
  }
  const outer = box.getBoundingClientRect()
  const rect = target.dom.getBoundingClientRect()
  math.value = {
    pos: target.pos,
    display: target.display,
    latex: target.latex,
    top: rect.bottom - outer.top + 6,
    left: target.display ? 0 : Math.max(0, rect.left - outer.left),
  }
  await nextTick()
  mathInput.value?.focus()
}

/** Every keystroke re-typesets the formula above the field. */
function onMathInput() {
  const open = math.value
  const node = open && editor?.state.doc.nodeAt(open.pos)
  if (!open || !editor || !node || (node.type.name !== 'inlineMath' && node.type.name !== 'blockMath')) {
    return
  }
  editor.view.dispatch(editor.state.tr.setNodeMarkup(open.pos, undefined, { ...node.attrs, latex: open.latex }))
}

/** Closes the field; an emptied formula is deleted. `refocus` returns the caret to the
 *  text just past the formula. */
function closeMath(refocus: boolean) {
  const open = math.value
  math.value = null
  if (!open || !editor) {
    return
  }
  const node = editor.state.doc.nodeAt(open.pos)
  let after = open.pos + (node?.nodeSize ?? 0)
  if (node && !open.latex.trim()) {
    editor.view.dispatch(editor.state.tr.delete(open.pos, after))
    after = open.pos
  }
  if (refocus) {
    // Synchronously, not through the `focus` command, which waits a frame: in that frame
    // the field is already gone, focus sits on the page, and an Escape would close the
    // dialog instead of the block.
    const caret = TextSelection.near(editor.state.doc.resolve(Math.min(after, editor.state.doc.content.size)))
    editor.view.dispatch(editor.state.tr.setSelection(caret))
    editor.view.focus()
  }
}

function onMathKey(event: KeyboardEvent) {
  const open = math.value
  if (!open) {
    return
  }
  const commit =
    event.key === 'Escape' ||
    (event.key === 'Enter' &&
      !event.isComposing &&
      event.keyCode !== 229 &&
      (!open.display || event.ctrlKey || event.metaKey))
  if (commit) {
    event.preventDefault()
    event.stopPropagation()
    closeMath(true)
  }
}

function onMathBlur(event: FocusEvent) {
  if (closing || root.value?.contains(event.relatedTarget as Node | null)) {
    return
  }
  closeMath(false)
  emit('leave', 'blur')
}

const { label: keyLabel } = useShortcuts()

// --- the slash menu --------------------------------------------------------------------

/** The menu's widest and tallest, and the gap between it and the slash's line. */
const SLASH_MAX_HEIGHT = 432
const SLASH_GAP = 4

/** The open menu: where the slash's paragraph starts, followed through every change; the
 *  filter last committed; and the editor's text without the slash line, which the skills
 *  section runs on. */
const slash = ref<{ pos: number; query: string; besides: string } | null>(null)
const slashActive = ref(0)
/** Below the line the menu hangs from its top; flipped above, it stands on its bottom edge, so
 *  a filter that shortens it keeps it against the line instead of leaving it where it was. */
const slashPlace = ref<{ top: number | null; bottom: number | null; left: number; maxHeight: number }>({
  top: 0,
  bottom: null,
  left: 0,
  maxHeight: SLASH_MAX_HEIGHT,
})
const slashMenu = ref<HTMLElement | null>(null)
const slashList = ref<HTMLElement | null>(null)
/** A slash paragraph whose menu the reader closed: it stays closed until its slash is gone,
 *  so only a newly typed slash opens a menu again. */
let slashDismissed: number | null = null
const slashId = useId()

type ConvertRow = { kind: 'convert'; key: SlashConversion; icon: IconName; label: string; markdown: string }
type SkillRow = { kind: 'skill'; name: string; icon: IconName; description: string }
type SlashRow = ConvertRow | SkillRow

const slashConversions = computed<ConvertRow[]>(() =>
  slash.value
    ? matchSlash(SLASH_CONVERSIONS, slash.value.query).map((item) => ({
        kind: 'convert',
        key: item.key,
        icon: item.icon,
        label: t(`viewer.convert.${item.key}`),
        markdown: item.markdown,
      }))
    : [],
)

/** Whether the menu has a skills section: the document's block editor, holding text besides
 *  the slash line — there is nothing to run a skill on otherwise. */
const slashHasSkills = computed(() => Boolean(props.skills && slash.value?.besides.trim()))

/** The skills the filter matches, or null while they load. */
const slashSkills = computed<SkillRow[] | null>(() => {
  const open = slash.value
  if (!open || !props.skills || !slashHasSkills.value) {
    return []
  }
  const all = props.skills(open.besides)
  if (all === null) {
    return null
  }
  const options = all.map((skill) => ({ name: skill.name, description: skill.description, hint: '', group: 'skill' as const }))
  return matchCommands(options, open.query).map((option) => ({
    kind: 'skill',
    name: option.name,
    icon: skillIcon(option.name),
    description: option.description,
  }))
})

const slashRows = computed<SlashRow[]>(() => [...slashConversions.value, ...(slashSkills.value ?? [])])

function slashRowId(index: number): string {
  return `${slashId}-row-${index}`
}

/** The editor names the highlighted row, as the composer's textarea does for its menu. */
watch(
  () => (slash.value && slashRows.value.length ? slashRowId(Math.min(slashActive.value, slashRows.value.length - 1)) : null),
  (active) => {
    const dom = editor?.view.dom
    if (!dom) {
      return
    }
    if (active) {
      dom.setAttribute('aria-activedescendant', active)
      dom.setAttribute('aria-controls', `${slashId}-list`)
    } else {
      dom.removeAttribute('aria-activedescendant')
      dom.removeAttribute('aria-controls')
    }
  },
)

/** A committed filter that matches nothing closes the menu — once the skills have answered,
 *  since a skill may still match. */
watch(slashRows, (rows) => {
  if (slash.value && !rows.length && slashSkills.value !== null) {
    dismissSlash()
  }
})

/** The editor's markdown without the top-level block at `pos`. */
function markdownWithout(pos: number): string {
  if (!editor) {
    return ''
  }
  const index = editor.state.doc.resolve(pos).index(0)
  const json = editor.getJSON()
  return docToMarkdown({ ...json, content: (json.content ?? []).filter((_, at) => at !== index) })
}

function openSlash(pos: number) {
  slash.value = { pos, query: '', besides: markdownWithout(pos) }
  slashActive.value = 0
  if (slashHasSkills.value) {
    emit('slash-open')
  }
  void placeSlash()
}

function closeSlash() {
  slash.value = null
}

/** Closed by the reader — Escape, a click away, a filter matching nothing: the slash stays
 *  in the text and opens nothing more. */
function dismissSlash() {
  slashDismissed = slash.value?.pos ?? slashDismissed
  closeSlash()
}

/**
 * Under the slash's line, `min(300px, 100%)` wide and held inside the editor; flipped above
 * the line where the scroll region has no room below and more above, and never taller than
 * the room on the side it opens to.
 */
async function placeSlash() {
  await nextTick()
  const open = slash.value
  const box = root.value
  if (!open || !editor || !box || !slashMenu.value) {
    return
  }
  const coords = editor.view.coordsAtPos(Math.min(open.pos + 1, editor.state.doc.content.size))
  const outer = box.getBoundingClientRect()
  const region = scrollParent(box)?.getBoundingClientRect() ?? { top: 0, bottom: window.innerHeight }
  const below = region.bottom - coords.bottom - 2 * SLASH_GAP
  const above = coords.top - region.top - 2 * SLASH_GAP
  const flip = below < Math.min(SLASH_MAX_HEIGHT, slashMenu.value.scrollHeight) && above > below
  const maxHeight = Math.max(120, Math.min(SLASH_MAX_HEIGHT, flip ? above : below))
  slashPlace.value = { ...slashPlace.value, maxHeight }
  await nextTick()
  const menu = slashMenu.value
  if (!menu || slash.value !== open) {
    return
  }
  const left = Math.min(Math.max(0, coords.left - outer.left - SLASH_GAP), Math.max(0, outer.width - menu.offsetWidth))
  slashPlace.value = flip
    ? { top: null, bottom: outer.bottom - coords.top + SLASH_GAP, left, maxHeight }
    : { top: coords.bottom - outer.top + SLASH_GAP, bottom: null, left, maxHeight }
}

/**
 * Reads the menu off the editor after every transaction (docs/web.md § Editing, *The slash
 * menu*): a slash typed into an empty top-level paragraph that holds only text opens it; the
 * text after the slash filters it, read only outside a composition; the caret leaving the
 * paragraph, or the slash going, closes it.
 */
function syncSlash(tr: Transaction) {
  if (!editor || props.singleLine) {
    return
  }
  if (slashDismissed !== null) {
    slashDismissed = tr.mapping.map(slashDismissed)
  }
  const open = slash.value
  if (open) {
    open.pos = tr.mapping.map(open.pos)
  }
  const { state, view } = editor
  const { selection, doc } = state
  const $from = selection.$from
  const paragraph =
    selection.empty && $from.depth === 1 && $from.parent.type.name === 'paragraph' && isTextOnly($from.parent) ? $from.parent : null
  const start = paragraph ? $from.before() : -1
  // A dismissed slash is forgotten once its paragraph no longer starts with one.
  if (slashDismissed !== null) {
    const node = doc.nodeAt(slashDismissed)
    if (node?.type.name !== 'paragraph' || slashQuery(node.textContent) === null) {
      slashDismissed = null
    }
  }
  if (open) {
    if (!paragraph || start !== open.pos) {
      dismissSlash()
      return
    }
    // While an input method composes, the menu keeps the rows of the last committed filter.
    if (view.composing) {
      return
    }
    const query = slashQuery(paragraph.textContent)
    if (query === null) {
      closeSlash()
    } else if (query !== open.query) {
      open.query = query
      slashActive.value = 0
    }
    return
  }
  if (tr.docChanged && paragraph && !view.composing && opensSlash(paragraph.textContent) && slashDismissed !== start) {
    openSlash(start)
  }
}

function isTextOnly(node: ProseNode): boolean {
  let text = true
  node.forEach((child) => {
    text &&= child.isText
  })
  return text
}

function moveSlash(step: number) {
  const rows = slashRows.value
  if (!rows.length) {
    return
  }
  slashActive.value = (slashActive.value + step + rows.length) % rows.length
  void nextTick(() => {
    const list = slashList.value
    const row = list?.querySelector<HTMLElement>(`#${CSS.escape(slashRowId(slashActive.value))}`)
    if (!list || !row) {
      return
    }
    if (row.offsetTop < list.scrollTop) {
      list.scrollTop = row.offsetTop - SLASH_GAP
    } else if (row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight + SLASH_GAP
    }
  })
}

/**
 * A row applied: the typed `/query` removed, and the empty paragraph turned into the block
 * with the caret in it — a display formula with its TeX field open, a table of three columns
 * with the caret in its first header cell. A skill removes the whole slash line and hands the
 * editor's text to the viewer.
 */
function applySlash(row: SlashRow) {
  const open = slash.value
  closeSlash()
  if (!editor || !open) {
    return
  }
  const paragraph = editor.state.doc.nodeAt(open.pos)
  if (paragraph?.type.name !== 'paragraph') {
    return
  }
  const start = open.pos
  const end = start + paragraph.nodeSize
  const { schema } = editor.state
  if (row.kind === 'skill') {
    editor.view.dispatch(editor.state.tr.delete(start, end))
    emit('run-skill', row.name, docToMarkdown(editor.getJSON()))
    return
  }
  const chain = editor.chain().deleteRange({ from: start + 1, to: end - 1 })
  switch (row.key) {
    case 'paragraph':
      break
    case 'heading':
      chain.setNode('heading', { level: 2 })
      break
    case 'subheading':
      chain.setNode('heading', { level: 3 })
      break
    case 'bulleted':
      chain.toggleBulletList()
      break
    case 'numbered':
      chain.toggleOrderedList()
      break
    case 'quote':
      chain.wrapIn('blockquote')
      break
    case 'code':
      chain.setNode('codeBlock')
      break
    case 'formula':
      chain.command(({ tr }) => {
        tr.replaceWith(start, start + 2, schema.nodes.blockMath!.create({ latex: '' }))
        tr.setSelection(NodeSelection.create(tr.doc, start))
        return true
      })
      break
    case 'table':
      chain.command(({ tr }) => {
        const cells = (type: 'tableHeader' | 'tableCell') =>
          schema.nodes.tableRow!.create(null, [0, 1, 2].map(() => schema.nodes[type]!.createAndFill()!))
        tr.replaceWith(start, start + 2, schema.nodes.table!.create(null, [cells('tableHeader'), cells('tableCell')]))
        // Into the table, its first row, its first cell, that cell's paragraph.
        tr.setSelection(TextSelection.create(tr.doc, start + 4))
        return true
      })
      break
  }
  chain.scrollIntoView().run()
  if (row.key === 'formula') {
    void nextTick(() => {
      const dom = editor?.view.nodeDOM(start)
      if (dom instanceof HTMLElement) {
        void openMath({ pos: start, latex: '', display: true, dom })
      }
    })
  }
}

/** The menu's keys: ↑ ↓ move, Enter or Tab applies, Escape closes the menu alone. */
function onSlashKey(event: KeyboardEvent): boolean {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    moveSlash(event.key === 'ArrowDown' ? 1 : -1)
    return true
  }
  if (event.key === 'Enter' || event.key === 'Tab') {
    const row = slashRows.value[slashActive.value]
    if (row) {
      applySlash(row)
      return true
    }
    // Only the skills' *Loading…* is there: nothing to apply, and Tab must not leave.
    return event.key === 'Tab'
  }
  if (event.key === 'Escape') {
    // The menu's Escape, not the editor's: the block stays open with the typed text.
    event.stopPropagation()
    dismissSlash()
    return true
  }
  return false
}

// --- the editor ------------------------------------------------------------------------

/** Whether the caret sits in the document's first (or last) textblock. */
function atEdge(edge: 'start' | 'end'): boolean {
  if (!editor) {
    return false
  }
  const { state } = editor
  const bound = edge === 'start' ? Selection.atStart(state.doc) : Selection.atEnd(state.doc)
  // A block that is one display formula opens with the formula selected as a whole.
  const whole = state.selection instanceof NodeSelection
  return (state.selection.empty || whole) && state.selection.$from.start() === bound.$from.start()
}

/** On the first (or last) line of its textblock — or on a selected formula, which is one. */
function onEdgeLine(direction: 'up' | 'down'): boolean {
  return !!editor && (editor.state.selection instanceof NodeSelection || editor.view.endOfTextblock(direction))
}

function onKey(event: KeyboardEvent): boolean {
  if (!editor || event.isComposing || event.keyCode === 229) {
    return false
  }
  if (slash.value && onSlashKey(event)) {
    return true
  }
  const { selection } = editor.state
  if (event.key === 'Enter' && selection instanceof NodeSelection) {
    const name = selection.node.type.name
    const dom = editor.view.nodeDOM(selection.from)
    // Enter on a selected formula opens its TeX field: the keyboard's click.
    if ((name === 'inlineMath' || name === 'blockMath') && dom instanceof HTMLElement) {
      void openMath({ pos: selection.from, latex: String(selection.node.attrs.latex), display: name === 'blockMath', dom })
      return true
    }
    // Enter on a selected section link locates, as its click does — a chip only.
    if (name === 'sectionLink' && dom instanceof HTMLElement && dom.classList.contains('rich-link')) {
      dom.click()
      return true
    }
  }
  if (event.key === 'Escape') {
    // The editor's Escape, not the dialog's: it leaves the text, and the next one closes.
    event.stopPropagation()
    if (props.variant === 'notes') {
      editor.commands.blur()
    }
    emit('leave', 'escape')
    return true
  }
  if (event.key === 'Enter' && props.singleLine && !event.shiftKey) {
    emit('leave', 'enter')
    return true
  }
  if (props.variant !== 'block') {
    return false
  }
  const { state } = editor
  if (event.key === 'ArrowUp' && atEdge('start') && onEdgeLine('up')) {
    return props.onNavigate?.('up') ?? false
  }
  if (event.key === 'ArrowDown' && atEdge('end') && onEdgeLine('down')) {
    return props.onNavigate?.('down') ?? false
  }
  if (event.key === 'Backspace' && atEdge('start') && state.selection.$from.parentOffset === 0) {
    const top = state.doc.firstChild?.type.name
    if (top === 'paragraph' || top === 'heading') {
      return props.onMerge?.(docToMarkdown(editor.getJSON())) ?? false
    }
  }
  return false
}

/** Puts the caret and focuses at once — the `focus` command waits a frame, and the keys a
 *  reader types in that frame would land on the page instead. */
function focusAt(selection: Selection) {
  if (!editor) {
    return
  }
  editor.view.dispatch(editor.state.tr.setSelection(selection).scrollIntoView())
  editor.view.focus()
}

/**
 * Where a text position lands in this editor's document: the editor holds the same
 * textblocks in the same order as the rendered blocks it replaced, because it was built from
 * the same parse (docs/web.md § Editing, *Selecting is editing*). A display formula or a raw
 * block is one slot of its own, which a position lands before or after.
 */
function resolveText(doc: ProseNode, point: TextPoint): number {
  const slots: { pos: number; size: number; atom: boolean }[] = []
  doc.descendants((node, pos) => {
    if (node.isTextblock) {
      slots.push({ pos: pos + 1, size: node.content.size, atom: false })
      return false
    }
    if (node.isBlock && node.isAtom) {
      slots.push({ pos, size: node.nodeSize, atom: true })
      return false
    }
    return true
  })
  const last = slots.length - 1
  const slot = slots[Math.min(point.slot, last)]
  if (!slot) {
    return 0
  }
  // Past the last slot is the end of the text.
  const offset = point.slot > last ? slot.size : point.offset
  return slot.atom ? slot.pos + (offset > 0 ? slot.size : 0) : slot.pos + Math.min(offset, slot.size)
}

function placeCaret(caret: Caret | null) {
  if (!editor) {
    return
  }
  const { doc } = editor.state
  if (caret === 'join') {
    // The first block is the one before, the rest is this one: Backspace's own join, at
    // the seam between them.
    const first = doc.firstChild
    if (first && doc.childCount > 1) {
      focusAt(TextSelection.near(doc.resolve(first.nodeSize + 1)))
      editor.commands.joinBackward()
      return
    }
  }
  if (caret && typeof caret === 'object' && 'from' in caret) {
    // A selection made on the rendered blocks, carried by text position: the same text,
    // selected.
    focusAt(TextSelection.between(doc.resolve(resolveText(doc, caret.from)), doc.resolve(resolveText(doc, caret.to))))
    return
  }
  if (caret && typeof caret === 'object') {
    const hit = editor.view.posAtCoords({ left: caret.x, top: caret.y })
    if (hit) {
      focusAt(TextSelection.near(doc.resolve(hit.pos)))
      // The click that opened the block landed on a formula: it was meant for the formula,
      // so its field opens now rather than on a second click.
      const formula = hit.inside >= 0 ? doc.nodeAt(hit.inside) : null
      const dom = formula && editor.view.nodeDOM(hit.inside)
      if (formula && (formula.type.name === 'inlineMath' || formula.type.name === 'blockMath') && dom instanceof HTMLElement) {
        void openMath({ pos: hit.inside, latex: String(formula.attrs.latex), display: formula.type.name === 'blockMath', dom })
      }
      return
    }
  }
  focusAt(caret === 'start' ? Selection.atStart(doc) : Selection.atEnd(doc))
}

/** A composition that ends without changing the text sends no transaction: the filter is
 *  read again once it has settled. */
function onCompositionEnd() {
  setTimeout(() => {
    if (editor && slash.value) {
      syncSlash(editor.state.tr)
    }
  }, 30)
}

// --- blocks dropped from the document -----------------------------------------------------------

/**
 * Where a drag of the viewer's blocks would land (docs/web.md § Notes, *A block dragged into
 * the notes lands as blocks*): the line drawn on the edge of the top-level block the drop goes
 * before or after, in the root's coordinates.
 */
const dropLine = ref<{ top: number; left: number; width: number } | null>(null)

function carriesBlocks(event: DragEvent): boolean {
  return props.variant === 'notes' && [...(event.dataTransfer?.types ?? [])].includes(BLOCK_DRAG_TYPE)
}

/** The top-level position the drop lands at — before or after the block under the pointer by
 *  its vertical middle, after the last block below them all, the whole of empty notes — and
 *  the edge the line sits on. */
function blockDropAt(y: number): { pos: number; replace: boolean; edge: number } | null {
  if (!editor) {
    return null
  }
  const { view } = editor
  const { doc } = view.state
  const paper = view.dom.getBoundingClientRect()
  if (doc.childCount === 1 && doc.firstChild?.type.name === 'paragraph' && doc.firstChild.content.size === 0) {
    return { pos: 0, replace: true, edge: paper.top }
  }
  let found: { pos: number; replace: boolean; edge: number } | null = null
  doc.forEach((node, offset) => {
    const dom = found ? null : view.nodeDOM(offset)
    if (dom instanceof HTMLElement) {
      const rect = dom.getBoundingClientRect()
      if (y <= rect.bottom) {
        const after = y > (rect.top + rect.bottom) / 2
        found = after ? { pos: offset + node.nodeSize, replace: false, edge: rect.bottom } : { pos: offset, replace: false, edge: rect.top }
      }
    }
  })
  return found ?? { pos: doc.content.size, replace: false, edge: paper.bottom }
}

function onBlockDragOver(event: DragEvent) {
  if (!carriesBlocks(event) || !editor) {
    return
  }
  event.preventDefault()
  setDropEffect(event, 'copy')
  const at = blockDropAt(event.clientY)
  const frame = root.value?.getBoundingClientRect()
  const paper = editor.view.dom.getBoundingClientRect()
  dropLine.value = at && frame ? { top: at.edge - frame.top, left: paper.left - frame.left, width: paper.width } : null
}

function onBlockDragLeave(event: DragEvent) {
  if (!root.value?.contains(event.relatedTarget as Node | null)) {
    dropLine.value = null
  }
}

function clearDropLine() {
  dropLine.value = null
}

/** The blocks land as blocks: the drag's markdown, a figure as its caption and a page marker
 *  left out, read through the viewer's parser and inserted whole. A copy — the document keeps
 *  its block — saved like any notes edit. */
function onBlockDrop(event: DragEvent) {
  if (!carriesBlocks(event) || !editor) {
    return
  }
  event.preventDefault()
  dropLine.value = null
  const at = blockDropAt(event.clientY)
  const markdown = droppedBlocksMarkdown(event.dataTransfer?.getData('text/plain') ?? '')
  if (!at || markdown === null) {
    return
  }
  const { view } = editor
  const doc = view.state.schema.nodeFromJSON(markdownToDoc(markdown, inlineOptions))
  const tr = at.replace ? view.state.tr.replaceWith(0, view.state.doc.content.size, doc.content) : view.state.tr.insert(at.pos, doc.content)
  view.dispatch(tr.scrollIntoView())
}

onMounted(() => {
  window.addEventListener('dragend', clearDropLine)
  const element = mount.value
  if (!element) {
    return
  }
  editor = new Editor({
    element,
    extensions: richExtensions({
      placeholder: props.placeholder,
      onEditMath: (target) => void openMath(target),
      links: props.links,
    }),
    content: markdownToDoc(props.source, inlineOptions),
    editable: props.editable,
    // A caption is one line: no shortcut there may make a heading, a list or a block.
    ...(props.singleLine ? { enableInputRules: ['bold', 'italic', 'code', 'inlineMath'] } : {}),
    editorProps: {
      attributes: {
        class: 'rich-doc',
        // A parsed paper's names and acronyms are not misspellings (docs/web.md § Editing).
        spellcheck: 'false',
        ...(props.label ? { 'aria-label': props.label } : {}),
        ...(props.describedBy ? { 'aria-describedby': props.describedBy } : {}),
      },
      handleKeyDown: (_view, event) => onKey(event),
      // Copy is markdown whether the block is open or rendered (docs/web.md § Editing).
      clipboardTextSerializer: (slice) => sliceToMarkdown(slice.content.toJSON() ?? []),
      // Pasted markdown reads as markdown, through the viewer's parser — block references to
      // this document rewritten as section links first, where links are read.
      clipboardTextParser: (text, _context, plain, view) => {
        const markdown = props.links?.rewrite?.(text) ?? text
        const doc = view.state.schema.nodeFromJSON(markdownToDoc(markdown, inlineOptions))
        return plain || !markdown.includes('\n') && doc.childCount === 1 && doc.firstChild?.type.name === 'paragraph'
          ? new Slice(doc.content, 1, 1)
          : Slice.maxOpen(doc.content)
      },
      // A drag of the viewer's blocks is the root's to land (`onBlockDrop`), never text dropped
      // into the line under the pointer.
      handleDrop: (_view, event) => carriesBlocks(event),
    },
    onTransaction: ({ transaction }) => syncSlash(transaction),
    onUpdate: ({ editor: current, transaction }) => {
      if (!transaction.docChanged) {
        return
      }
      // The TeX field follows its formula as text before it changes.
      if (math.value) {
        math.value.pos = transaction.mapping.map(math.value.pos)
      }
      emitted = docToMarkdown(current.getJSON(), props.variant === 'notes' ? trailing : '')
      emit('change', emitted)
    },
    // Back in the text, the TeX field is done with: a click away closes it.
    onFocus: () => {
      if (math.value) {
        closeMath(false)
      }
    },
    onBlur: ({ event }) => {
      // Into the TeX field — or onto the slash menu — is still inside the editor.
      if (closing || root.value?.contains(event.relatedTarget as Node | null)) {
        return
      }
      if (slash.value) {
        dismissSlash()
      }
      emit('leave', 'blur')
    },
  })
  editor.view.dom.addEventListener('compositionend', onCompositionEnd)
  if (props.variant === 'block') {
    placeCaret(props.caret)
  }
})

/** The notes file reloaded underneath (another tab, the agent, an excerpt): taken only when
 *  it is not this editor's own last change coming back — and kept out of the editor's history,
 *  so its undo starts over rather than stepping back across text it never wrote. */
watch(
  () => props.source,
  (source) => {
    if (props.variant !== 'notes' || !editor || source === emitted) {
      return
    }
    emitted = null
    trailing = /\n\s*$/.exec(source)?.[0] ?? ''
    closeSlash()
    editor.chain().setMeta('addToHistory', false).setContent(markdownToDoc(source, inlineOptions), { emitUpdate: false }).run()
  },
)

watch(
  () => props.editable,
  (editable) => {
    // No update event: nothing in the text changed.
    editor?.setEditable(editable, false)
  },
)

/** How long a wash holds, and how long it then takes to fade (`--duration-slow`). */
const WASH_MS = 700
const WASH_FADE_MS = 240
let washTimer: ReturnType<typeof setTimeout> | undefined

function setWash(meta: WashMeta) {
  if (editor) {
    editor.view.dispatch(editor.state.tr.setMeta(washKey, meta))
  }
}

/** Where a top-level block starts in the document, by index. */
function topLevelPos(doc: ProseNode, index: number): number {
  let pos = 0
  for (let at = 0; at < Math.min(index, doc.childCount); at += 1) {
    pos += doc.child(at).nodeSize
  }
  return pos
}

/** Top-level blocks washed once and scrolled to the middle of the column (docs/web.md
 *  § Notes, *Then the notes show it*), focus left where it was. */
function wash(first: number, count: number) {
  if (!editor) {
    return
  }
  clearTimeout(washTimer)
  setWash({ first, count })
  const dom = editor.view.nodeDOM(topLevelPos(editor.state.doc, first))
  const region = scrollParent(root.value)
  if (dom instanceof HTMLElement && region) {
    const rect = dom.getBoundingClientRect()
    const box = region.getBoundingClientRect()
    region.scrollTop += rect.top - box.top - Math.max(0, (box.height - rect.height) / 2)
  }
  washTimer = setTimeout(() => {
    setWash('fade')
    washTimer = setTimeout(() => setWash('clear'), WASH_FADE_MS)
  }, WASH_MS)
}

onBeforeUnmount(() => {
  closing = true
  clearTimeout(washTimer)
  window.removeEventListener('dragend', clearDropLine)
  editor?.view.dom.removeEventListener('compositionend', onCompositionEnd)
  editor?.destroy()
  editor = null
})

defineExpose({
  focus: (where: 'start' | 'end' = 'end') => {
    if (editor) {
      focusAt(where === 'start' ? Selection.atStart(editor.state.doc) : Selection.atEnd(editor.state.doc))
    }
  },
  wash,
  /** The caret at the start of a top-level block, scrolled into view: the keyboard reaching
   *  an excerpt, so what was reached is read out. */
  focusBlock: (index: number) => {
    if (editor) {
      const { doc } = editor.state
      focusAt(Selection.near(doc.resolve(Math.min(topLevelPos(doc, index) + 1, doc.content.size))))
    }
  },
  /** The section links are drawn again: what they resolve to changed. */
  refreshLinks: () => {
    const storage = editor?.storage as { sectionLink?: { refresh: () => void } } | undefined
    storage?.sectionLink?.refresh()
  },
  /** The selection as markdown — the clipboard's serializer over it — and, as the editor
   *  writes them, the top-level blocks before the one it starts in; null with nothing
   *  selected. */
  selectedMarkdown: (): { markdown: string; before: string } | null => {
    if (!editor || editor.state.selection.empty) {
      return null
    }
    const { selection, doc } = editor.state
    const markdown = sliceToMarkdown(selection.content().content.toJSON() ?? [])
    if (!markdown.trim()) {
      return null
    }
    const json = editor.getJSON()
    return { markdown, before: docToMarkdown({ ...json, content: (json.content ?? []).slice(0, doc.resolve(selection.from).index(0)) }) }
  },
})
</script>

<template>
  <div
    ref="root"
    class="rich"
    :class="`rich-${variant}`"
    @dragover="onBlockDragOver"
    @dragleave="onBlockDragLeave"
    @drop="onBlockDrop"
  >
    <div ref="mount" />
    <!-- Where blocks dragged from the document would land: on a block's edge. -->
    <div
      v-if="dropLine"
      class="drop-line"
      aria-hidden="true"
      :style="{ top: `${dropLine.top}px`, left: `${dropLine.left}px`, width: `${dropLine.width}px` }"
    />
    <!-- The TeX field: the context menu's floating surface, under the formula it edits. -->
    <div
      v-if="math"
      class="math-field"
      :class="{ display: math.display }"
      :style="{ top: `${math.top}px`, left: `${math.left}px` }"
    >
      <textarea
        v-if="math.display"
        ref="mathInput"
        v-model="math.latex"
        class="math-input mono"
        rows="2"
        spellcheck="false"
        :aria-label="t('viewer.mathSource')"
        @input="onMathInput"
        @keydown="onMathKey"
        @blur="onMathBlur"
      />
      <input
        v-else
        ref="mathInput"
        v-model="math.latex"
        class="math-input mono"
        type="text"
        spellcheck="false"
        :aria-label="t('viewer.mathSource')"
        @input="onMathInput"
        @keydown="onMathKey"
        @blur="onMathBlur"
      >
      <span class="math-hint">{{
        math.display ? t('viewer.mathDoneDisplay', { keys: keyLabel('mod+enter') }) : t('viewer.mathDone')
      }}</span>
    </div>

    <!-- The slash menu: the menu surface under the slash. A press inside it keeps focus in
         the text, which is what keeps the block open. -->
    <div
      v-if="slash"
      ref="slashMenu"
      class="slash"
      :class="{ above: slashPlace.bottom !== null }"
      :style="{
        top: slashPlace.top === null ? 'auto' : `${slashPlace.top}px`,
        bottom: slashPlace.bottom === null ? 'auto' : `${slashPlace.bottom}px`,
        left: `${slashPlace.left}px`,
        maxHeight: `${slashPlace.maxHeight}px`,
      }"
      @mousedown.prevent
    >
      <div :id="`${slashId}-list`" ref="slashList" class="slash-list" role="listbox" :aria-label="t('viewer.slash.label')">
        <div v-if="slashConversions.length" class="slash-group" role="group" :aria-labelledby="`${slashId}-convert`">
          <p :id="`${slashId}-convert`" class="slash-label">{{ t('viewer.slash.convert') }}</p>
          <button
            v-for="(row, index) in slashConversions"
            :id="slashRowId(index)"
            :key="`convert-${index}`"
            type="button"
            role="option"
            class="slash-row"
            :class="{ active: slashActive === index }"
            :aria-selected="slashActive === index ? 'true' : 'false'"
            tabindex="-1"
            @mousemove="slashActive = index"
            @click="applySlash(row)"
          >
            <UiIcon :name="row.icon" />
            <span class="slash-name">{{ row.label }}</span>
            <span v-if="row.markdown" class="slash-key mono">{{ row.markdown }}</span>
          </button>
        </div>
        <div v-if="slashHasSkills" class="slash-group" role="group" :aria-labelledby="`${slashId}-skills`">
          <p :id="`${slashId}-skills`" class="slash-label">{{ t('viewer.slash.skills') }}</p>
          <p v-if="slashSkills === null" class="slash-empty" role="status">{{ t('common.loading') }}</p>
          <template v-else>
            <button
              v-for="(row, index) in slashSkills"
              :id="slashRowId(slashConversions.length + index)"
              :key="`skill-${index}`"
              type="button"
              role="option"
              class="slash-row"
              :class="{ active: slashActive === slashConversions.length + index }"
              :aria-selected="slashActive === slashConversions.length + index ? 'true' : 'false'"
              tabindex="-1"
              :title="`/${row.name}: ${row.description}`"
              @mousemove="slashActive = slashConversions.length + index"
              @click="applySlash(row)"
            >
              <UiIcon :name="row.icon" />
              <span class="slash-name mono described">/{{ row.name }}</span>
              <span class="slash-description">{{ row.description }}</span>
            </button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rich {
  position: relative;
  min-width: 0;
}

/* The notes fill their column, so a drop anywhere in it lands. */
.rich-notes {
  min-height: 100%;
}

/* The document pane's drop line, on the notes (docs/web.md § Editing, *Moving blocks*). */
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

/* The editable surface draws nothing of its own: no border, no ring, no fill — the block is
   the same block, now with a caret in it. Blocks inside one editor keep the gap the
   surface around them keeps between rendered blocks — as margins, not a flex gap: a flex
   container as the editable root breaks Home and End line navigation in Chromium. */
.rich :deep(.rich-doc) {
  outline: none;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  /* ProseMirror turns ligatures off, and Firefox then sets the whole text narrower than the
     rendered block: they stay on, as they are there. */
  font-variant-ligatures: normal;
  font-feature-settings: normal;
}

.rich :deep(.rich-doc > * + *) {
  margin-top: var(--rich-gap, 20px);
}

.rich :deep(.md-list p),
.rich :deep(.md-table p),
.rich :deep(.md-quote > p) {
  margin: 0;
  color: inherit;
  font-size: inherit;
  line-height: inherit;
}

/* A table scrolls sideways in place, as the rendered one does inside its wrapper. */
.rich :deep(table.md-table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
}

.rich :deep(.md-table th p) {
  text-transform: uppercase;
}

/* A formula is one object: it highlights as a whole and opens on a click. */
.rich :deep(.rich-math) {
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.rich :deep(.rich-math:hover),
.rich :deep(.ProseMirror-selectednode.rich-math) {
  background: var(--rail-active);
}

.rich :deep(.rich-math.is-source) {
  color: var(--faint);
  font-family: var(--mono);
  font-size: 0.9em;
}

.rich :deep(.md-math-block.rich-math) {
  min-height: 1.5em;
}

.rich :deep(.rich-raw) {
  color: var(--faint);
}

.rich :deep(p.is-editor-empty:first-child::before) {
  content: attr(data-placeholder);
  float: left;
  height: 0;
  color: var(--faint);
  pointer-events: none;
}

/* A section link in the notes: the answers' chip — the locate glyph and the heading on the
   rail surface — pressed while the document shows what it located (docs/web.md § Notes). */
.rich :deep(.rich-link) {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  max-width: 100%;
  height: 20px;
  margin-inline: 2px;
  padding: 0 6px;
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1;
  vertical-align: 1px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.rich :deep(.rich-link:hover),
.rich :deep(.rich-link.pressed),
.rich :deep(.ProseMirror-selectednode.rich-link) {
  background: var(--rail-active);
  color: var(--ink);
}

.rich :deep(.rich-link svg) {
  flex: none;
  width: 12px;
  height: 12px;
}

.rich :deep(.rich-link-label) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* An excerpt's link sits just under its quote, its chip's glyph in line with the quote's
   text — the bar and the inset, less the chip's own margin and padding — so the two read as
   one excerpt. */
.rich :deep(.rich-doc > blockquote + p:has(> .rich-link:only-child, > .rich-link-text:only-child)) {
  margin-top: var(--space-1);
  padding-left: calc(2px + var(--space-3) - 8px);
}

/* An excerpt the notes just took, or the one a marker leads to: the located heading's wash,
   behind the blocks, spread past them so it reads as a place. */
.rich :deep(.rich-wash) {
  position: relative;
  isolation: isolate;
}

.rich :deep(.rich-wash)::before {
  content: '';
  position: absolute;
  inset: calc(-1 * var(--space-1)) calc(-1 * var(--space-2));
  z-index: -1;
  border-radius: var(--radius);
  background: var(--accent-wash);
  pointer-events: none;
  transition: background var(--duration-slow) var(--ease);
}

.rich :deep(.rich-wash-fade)::before {
  background: transparent;
}

@starting-style {
  .rich :deep(.rich-wash)::before {
    background: transparent;
  }
}

.math-field {
  position: absolute;
  z-index: 4;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: min(420px, 100%);
  padding: var(--space-2);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
}

.math-field.display {
  width: 100%;
}

.math-input {
  width: 100%;
  min-width: 0;
  padding: var(--space-1) var(--space-2);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.6;
  resize: none;
}

.math-input:focus-visible {
  outline: none;
  background: var(--rail);
}

.math-hint {
  padding: 0 var(--space-2);
  color: var(--faint);
  font-size: var(--text-2xs);
  white-space: nowrap;
}

/* --- the slash menu: the menu surface, its rows the context menu's ------------------ */

.slash {
  position: absolute;
  z-index: 4;
  display: flex;
  flex-direction: column;
  width: min(300px, 100%);
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  font-family: var(--font);
  line-height: normal;
  white-space: normal;
  transform-origin: top left;
  animation: slash-in var(--duration-fast) var(--ease-enter);
}

.slash.above {
  transform-origin: bottom left;
}

@supports not (backdrop-filter: blur(1px)) {
  .slash {
    background: var(--paper);
  }
}

@keyframes slash-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .slash {
    animation: none;
  }
}

/* The rows scroll; the legend under them stays. */
.slash-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
}

.slash-list::-webkit-scrollbar {
  display: none;
}

.slash-group + .slash-group {
  margin-top: var(--space-1);
  padding-top: var(--space-1);
  border-top: 1px solid var(--menu-edge);
}

.slash-label {
  margin: 0;
  padding: var(--space-2) var(--space-3) var(--space-1);
  color: var(--faint);
  font-size: var(--text-xs);
}

.slash-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: none;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  white-space: nowrap;
  cursor: default;
}

/* The highlighted row is the accent, filled, as in the context menu. */
.slash-row.active {
  background: var(--accent);
  color: var(--accent-fg);
}

.slash-row > :deep(svg) {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  color: var(--faint);
}

.slash-row.active > :deep(svg) {
  color: currentcolor;
}

.slash-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* A skill's name keeps its width, up to most of the row, and the description takes the rest:
   shrunk in proportion to their widths, a long description would cut the name to a letter. */
.slash-name.described {
  flex: 0 0 auto;
  max-width: 70%;
}

.slash-description {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
}

.slash-key {
  flex-shrink: 0;
  margin-left: auto;
  padding-left: var(--space-3);
  color: var(--faint);
  font-size: var(--text-xs);
}

.slash-row.active .slash-description,
.slash-row.active .slash-key {
  color: currentcolor;
}

.slash-empty {
  margin: 0;
  padding: var(--space-1) var(--space-3) var(--space-2);
  color: var(--faint);
  font-size: var(--text-xs);
}
</style>
