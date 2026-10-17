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
 * whole file (`variant="notes"`).
 *
 * Formulas stay typeset while they are edited: a click on one opens a small floating TeX
 * field under it, and every keystroke re-renders the formula above.
 */
import { Editor } from '@tiptap/core'
import { Slice } from '@tiptap/pm/model'
import { NodeSelection, Selection, TextSelection } from '@tiptap/pm/state'
import { richExtensions, type Caret, type MathTarget, type Point } from '~/lib/rich-extensions'
import { docToMarkdown, markdownToDoc, sliceToMarkdown } from '~/lib/richdoc'

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
    /** Arrow past the first or last line. False keeps the key for the editor. */
    onNavigate?: (direction: 'up' | 'down') => boolean
    /** Backspace at the very start, with the editor's current markdown. False keeps the key. */
    onMerge?: (markdown: string) => boolean
  }>(),
  { variant: 'block', caret: null, placeholder: '', singleLine: false, label: undefined, onNavigate: undefined, onMerge: undefined },
)

const emit = defineEmits<{
  /** The editor's markdown, after a change the reader made. Never on load. */
  change: [markdown: string]
  /** The reader left: Escape, or focus went somewhere outside the editor. */
  leave: []
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

// --- the TeX field -------------------------------------------------------------------

const math = ref<{ pos: number; display: boolean; latex: string; top: number; left: number } | null>(null)
const mathInput = ref<HTMLInputElement | HTMLTextAreaElement | null>(null)

async function openMath(target: MathTarget) {
  const box = root.value
  if (!box) {
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
  emit('leave')
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
  // Enter on a selected formula opens its TeX field: the keyboard's click.
  const { selection } = editor.state
  if (event.key === 'Enter' && selection instanceof NodeSelection) {
    const name = selection.node.type.name
    const dom = editor.view.nodeDOM(selection.from)
    if ((name === 'inlineMath' || name === 'blockMath') && dom instanceof HTMLElement) {
      void openMath({ pos: selection.from, latex: String(selection.node.attrs.latex), display: name === 'blockMath', dom })
      return true
    }
  }
  if (event.key === 'Escape') {
    // The editor's Escape, not the dialog's: it leaves the text, and the next one closes.
    event.stopPropagation()
    if (props.variant === 'notes') {
      editor.commands.blur()
    }
    emit('leave')
    return true
  }
  if (event.key === 'Enter' && props.singleLine && !event.shiftKey) {
    emit('leave')
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
    // A selection made on the rendered blocks, which the editor draws the same: the same
    // text, selected.
    const { view } = editor
    const at = (point: Point) => view.posAtCoords({ left: point.x, top: point.y })?.pos
    focusAt(TextSelection.between(doc.resolve(at(caret.from) ?? 0), doc.resolve(at(caret.to) ?? doc.content.size)))
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

onMounted(() => {
  const element = mount.value
  if (!element) {
    return
  }
  editor = new Editor({
    element,
    extensions: richExtensions({
      placeholder: props.placeholder,
      onEditMath: (target) => void openMath(target),
    }),
    content: markdownToDoc(props.source),
    // A caption is one line: no shortcut there may make a heading, a list or a block.
    ...(props.singleLine ? { enableInputRules: ['bold', 'italic', 'code', 'inlineMath'] } : {}),
    editorProps: {
      attributes: {
        class: 'rich-doc',
        // A parsed paper's names and acronyms are not misspellings (docs/web.md § Editing).
        spellcheck: 'false',
        ...(props.label ? { 'aria-label': props.label } : {}),
      },
      handleKeyDown: (_view, event) => onKey(event),
      // Copy is markdown whether the block is open or rendered (docs/web.md § Editing).
      clipboardTextSerializer: (slice) => sliceToMarkdown(slice.content.toJSON() ?? []),
      // Pasted markdown reads as markdown, through the viewer's parser.
      clipboardTextParser: (text, _context, plain, view) => {
        const doc = view.state.schema.nodeFromJSON(markdownToDoc(text))
        return plain || !text.includes('\n') && doc.childCount === 1 && doc.firstChild?.type.name === 'paragraph'
          ? new Slice(doc.content, 1, 1)
          : Slice.maxOpen(doc.content)
      },
    },
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
      // Into the TeX field is still inside the editor.
      if (closing || root.value?.contains(event.relatedTarget as Node | null)) {
        return
      }
      emit('leave')
    },
  })
  if (props.variant === 'block') {
    placeCaret(props.caret)
  }
})

/** The notes file reloaded underneath (another tab, the agent): taken only when it is not
 *  this editor's own last change coming back. */
watch(
  () => props.source,
  (source) => {
    if (props.variant !== 'notes' || !editor || source === emitted) {
      return
    }
    emitted = null
    trailing = /\n\s*$/.exec(source)?.[0] ?? ''
    editor.commands.setContent(markdownToDoc(source), { emitUpdate: false })
  },
)

onBeforeUnmount(() => {
  closing = true
  editor?.destroy()
  editor = null
})

defineExpose({
  focus: (where: 'start' | 'end' = 'end') => {
    if (editor) {
      focusAt(where === 'start' ? Selection.atStart(editor.state.doc) : Selection.atEnd(editor.state.doc))
    }
  },
})
</script>

<template>
  <div ref="root" class="rich" :class="`rich-${variant}`">
    <div ref="mount" />
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
      <span class="math-hint">{{ t(math.display ? 'viewer.mathDoneDisplay' : 'viewer.mathDone') }}</span>
    </div>
  </div>
</template>

<style scoped>
.rich {
  position: relative;
  min-width: 0;
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
.rich :deep(.md-table p) {
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
</style>
