<script setup lang="ts">
/**
 * Find in the rendered document (docs/web.md § Result viewer).
 *
 * A floating surface, positioned by its caller: it hovers over the document rather than
 * taking a row above it, because a reader who opens a search to find the paragraph they are
 * looking at must not have that paragraph move first.
 *
 * Hits are painted with the **CSS Custom Highlight API**: ranges handed to the browser to
 * colour, with the DOM left exactly as it was. Wrapping hits in `<mark>` would cut the
 * `data-src` / `data-tex` sources that copy-as-markdown reads back out of the elements that
 * carry them, so a search would quietly change what Ctrl+C returns. A browser without the
 * API still steps and scrolls through the hits — it just does not colour them.
 *
 * The search runs over the document's text as one string with an offset map back into its
 * text nodes, not node by node, so a phrase that runs through a bold word is one hit rather
 * than none. KaTeX subtrees are skipped: a typeset formula is a tree of per-glyph spans plus
 * a hidden MathML twin, which yields fragments and doubles every hit. So are the viewer's own
 * controls drawn among the blocks (`data-chrome`: handles, chevrons, fold notes, markers) and
 * the blocks an open editor holds, whose text the editor shows — but not the blocks a
 * collapsed section hides (`.folded`): a hit there counts like any other, and becoming the
 * current one asks the viewer to open its section (docs/web.md § Result viewer).
 */
const props = defineProps<{
  /** The scrolling document pane — what is searched, and what is scrolled to a hit. */
  scroller: HTMLElement | null
}>()

const emit = defineEmits<{
  close: []
  /** The current hit is in a block a collapsed section hides, by its `data-block`. */
  expand: [block: string]
}>()

const { t } = useI18n()

const query = ref('')
const field = ref<HTMLInputElement | null>(null)
/** Ranges into the live document. `shallowRef` because a Range is a DOM object: making its
 *  internals reactive would be work on every keystroke that buys nothing. */
const hits = shallowRef<Range[]>([])
const current = ref(0)

/** Registered highlight names. Two, because the hit you are on has to be findable among the
 *  others — one wash, one solid. */
const ALL = 'sightread-find'
const ONE = 'sightread-find-current'

/** A `Highlight` is a set of ranges, and `CSS.highlights` the registry they are named in.
 *  Typed here rather than relied on from `lib.dom`, which is also the capability check: a
 *  browser without the API returns null and the bar simply does not paint. */
type Registry = { set: (name: string, ranges: Set<Range>) => void; delete: (name: string) => void }
type HighlightCtor = new () => Set<Range>

function registry(): Registry | null {
  return (globalThis.CSS as unknown as { highlights?: Registry } | undefined)?.highlights ?? null
}

function highlightCtor(): HighlightCtor | null {
  return (globalThis as unknown as { Highlight?: HighlightCtor }).Highlight ?? null
}

/**
 * Re-runs the search. `reset` sends the cursor back to the first hit — what a new query
 * wants; a re-scan under a still-growing partial result keeps the reader where they were.
 */
function scan(reset: boolean): void {
  const root = props.scroller
  const needle = query.value
  if (!root || !needle) {
    hits.value = []
    current.value = 0
    paint()
    return
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement?.closest('.katex, [data-chrome], [hidden]:not(.folded)')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
  })

  // Empty text nodes are dropped rather than recorded: they would give two parts the same
  // start offset, and the cursor below would then map a hit into a node with no room for it.
  const parts: { node: Text; start: number }[] = []
  let text = ''
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const value = node.nodeValue ?? ''
    if (!value) {
      continue
    }
    parts.push({ node: node as Text, start: text.length })
    text += value
  }

  // Case-insensitive, but only when lowercasing preserves length: in a few scripts it does
  // not (`İ` becomes two characters), and one shifted offset would highlight the wrong words
  // for the rest of the document. Where it would shift, the search stays case-sensitive.
  const lowered = text.toLowerCase()
  const loweredNeedle = needle.toLowerCase()
  const insensitive = lowered.length === text.length && loweredNeedle.length === needle.length
  const haystack = insensitive ? lowered : text
  const target = insensitive ? loweredNeedle : needle

  const found: Range[] = []
  // Parts and matches are both in document order, so this cursor only moves forward and the
  // whole scan stays linear in the length of the document.
  let part = 0
  let at = haystack.indexOf(target)
  while (at !== -1 && parts.length) {
    const end = at + target.length
    while (part + 1 < parts.length && parts[part + 1]!.start <= at) {
      part += 1
    }
    let last = part
    while (last + 1 < parts.length && parts[last + 1]!.start < end) {
      last += 1
    }
    const range = document.createRange()
    range.setStart(parts[part]!.node, at - parts[part]!.start)
    range.setEnd(parts[last]!.node, end - parts[last]!.start)
    found.push(range)
    at = haystack.indexOf(target, end)
  }

  hits.value = found
  current.value = reset || !found.length ? 0 : Math.min(current.value, found.length - 1)
  paint()
  void reveal()
}

function paint(): void {
  const highlights = registry()
  const Highlight = highlightCtor()
  if (!highlights || !Highlight) {
    return
  }
  highlights.delete(ALL)
  highlights.delete(ONE)
  if (!hits.value.length) {
    return
  }
  const rest = new Highlight()
  const one = new Highlight()
  hits.value.forEach((range, index) => (index === current.value ? one : rest).add(range))
  highlights.set(ALL, rest)
  highlights.set(ONE, one)
}

/** How much of the pane counts as "already on screen" — a hit against the very top edge is
 *  technically visible and practically missed. */
const MARGIN = 48

async function reveal(): Promise<void> {
  const box = props.scroller
  const range = hits.value[current.value]
  if (!box || !range) {
    return
  }
  // A hit a fold hides is opened first, so stepping never lands on text the reader cannot see.
  const folded = range.startContainer.parentElement?.closest<HTMLElement>('.block.folded')
  if (folded?.dataset.block) {
    emit('expand', folded.dataset.block)
    await nextTick()
  }
  const rect = range.getBoundingClientRect()
  const view = box.getBoundingClientRect()
  if (!rect.height && !rect.width) {
    return
  }
  // Already comfortably in view: typing the next letter of a query must not yank the page
  // around under the reader.
  if (rect.top >= view.top + MARGIN && rect.bottom <= view.bottom - MARGIN) {
    return
  }
  // A third down the pane, not at its top: a hit needs the lines above it to be read.
  box.scrollTop += rect.top - view.top - box.clientHeight / 3
}

function step(delta: number): void {
  if (!hits.value.length) {
    return
  }
  current.value = (current.value + delta + hits.value.length) % hits.value.length
  paint()
  void reveal()
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    // The dialog closes on Escape from a listener on `document`. A search you are still
    // inside must not take the whole document with it, so this one stops here.
    event.stopPropagation()
    emit('close')
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    step(event.shiftKey ? -1 : 1)
  }
}

const count = computed(() => {
  if (!query.value) {
    return ''
  }
  if (!hits.value.length) {
    return t('viewer.findNone')
  }
  return t('viewer.findCount', { index: current.value + 1, total: hits.value.length })
})

/**
 * A partial result re-renders the document under this bar every poll (docs/api.md § Partial
 * results), and a range into a replaced node is dead — it stops painting and stops scrolling
 * anywhere. Re-scanning on mutation is what keeps the count honest while a parse is running.
 * Painting cannot re-trigger this: the highlight API colours ranges without touching the DOM.
 * A mutation that only draws or takes away the viewer's controls — a handle under a moving
 * pointer, and the placeholder comment Vue leaves where it was — changes no text and rescans
 * nothing.
 */
let observer: MutationObserver | null = null
let pending: ReturnType<typeof setTimeout> | null = null

function inChrome(node: Node): boolean {
  return Boolean((node instanceof Element ? node : node.parentElement)?.closest('[data-chrome]'))
}

function changesText(record: MutationRecord): boolean {
  if (inChrome(record.target)) {
    return false
  }
  if (record.type !== 'childList') {
    return true
  }
  return [...record.addedNodes, ...record.removedNodes].some(
    (node) => node.nodeType !== Node.COMMENT_NODE && !(node instanceof Element && node.hasAttribute('data-chrome')),
  )
}

function observe(root: HTMLElement | null): void {
  observer?.disconnect()
  observer = null
  if (!root) {
    return
  }
  observer = new MutationObserver((records) => {
    if (!records.some(changesText)) {
      return
    }
    if (pending) {
      clearTimeout(pending)
    }
    pending = setTimeout(() => scan(false), 150)
  })
  observer.observe(root, { childList: true, subtree: true, characterData: true })
}

function focus(): void {
  field.value?.focus()
  field.value?.select()
}

watch(query, () => scan(true))
watch(
  () => props.scroller,
  (root) => {
    observe(root)
    scan(true)
  },
)

onMounted(() => {
  observe(props.scroller)
  focus()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  if (pending) {
    clearTimeout(pending)
  }
  const highlights = registry()
  highlights?.delete(ALL)
  highlights?.delete(ONE)
})

defineExpose({ focus })
</script>

<template>
  <div class="find">
    <UiIcon class="find-glyph" name="search" />
    <!-- A plain input, not `UiTextInput`: this is a toolbar field that has to read as part
         of the bar it sits in, not a bordered control on a form. -->
    <input
      ref="field"
      v-model="query"
      class="find-field"
      type="text"
      autocomplete="off"
      spellcheck="false"
      :placeholder="t('viewer.findPlaceholder')"
      :aria-label="t('viewer.find')"
      @keydown="onKey"
    >
    <span class="find-count tabular" aria-live="polite">{{ count }}</span>
    <UiButton
      variant="ghost"
      size="sm"
      icon-only
      :label="t('viewer.findPrev')"
      :disabled="!hits.length"
      @click="step(-1)"
    >
      <template #icon><UiIcon name="chevron-up" /></template>
    </UiButton>
    <UiButton
      variant="ghost"
      size="sm"
      icon-only
      :label="t('viewer.findNext')"
      :disabled="!hits.length"
      @click="step(1)"
    >
      <template #icon><UiIcon name="chevron-down" /></template>
    </UiButton>
    <UiButton variant="ghost" size="sm" icon-only :label="t('viewer.findClose')" @click="emit('close')">
      <template #icon><UiIcon name="close" /></template>
    </UiButton>
  </div>
</template>

<style scoped>
/* The design's floating-surface tokens, the same ones the context menu is built from: this
   is the other thing in the app that hovers over the page rather than being cut into it. */
.find {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  animation: find-in var(--duration-fast) var(--ease-enter);
}

/* Without blur support the translucency would leave text over text, so it goes opaque. */
@supports not (backdrop-filter: blur(1px)) {
  .find {
    background: var(--paper);
  }
}

@keyframes find-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}

.find-glyph {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  color: var(--faint);
}

.find-field {
  flex: 1;
  min-width: 0;
  height: var(--control-height-sm);
  border: none;
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
}

.find-field:focus {
  /* The bar is the field: a ring around the input alone would draw a box inside a box. */
  outline: none;
}

.find-field::placeholder {
  color: var(--faint);
}

.find-count {
  flex-shrink: 0;
  padding-inline: var(--space-1);
  color: var(--faint);
  font-size: var(--text-2xs);
  white-space: nowrap;
}
</style>

<style>
/* Unscoped on purpose: `::highlight()` paints ranges that live inside the *document's*
   elements, which carry the result viewer's scope attribute rather than this component's, so
   a scoped rule would match nothing. Only the properties the spec allows on a highlight
   pseudo are set here — `background-color`, not the `background` shorthand. */
::highlight(sightread-find) {
  background-color: var(--find);
}

::highlight(sightread-find-current) {
  background-color: var(--find-current);
  color: var(--find-current-fg);
}
</style>
