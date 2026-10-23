<script setup lang="ts">
/**
 * The small card a memory line's **View** or a part of an answer's *Used* line opens, on the
 * context menu's surface (docs/web.md § The agent panel, *What the answer used, and memory
 * lines*): one memory, the reader's instructions as they are now, or the memories a turn
 * loaded — each with its kind, a memory deleted since saying so — and a link to the Agent
 * page, where they are managed.
 *
 * It floats the way the context menu does: teleported out of the thread's scroll container
 * (into the dialog's panel when there is one), under the control that opened it or over it
 * where there is no room. Unlike a menu it is read, so it stays with that control while the
 * thread scrolls or grows under it, and closes only once the control has scrolled out of
 * the thread's view, or on Escape, a press outside or focus leaving it — focus then goes
 * back to that control.
 */
import { listAgents, type Memory, type MemoryKind } from '~/lib/api'
import { formatShortDateTime } from '~/lib/format'
import { parseResultMarkdown, type ResultBlock } from '~/lib/markdown'
import { modalPanelKey } from '~/lib/menu'
import { SETTINGS_QUERY } from '~/lib/settings'
import { memoryLineState, type ContextCardView } from '~/lib/thread'

const props = defineProps<{
  /** The control that opened it: where it stands, and where focus goes back to. */
  anchor: HTMLElement
  view: ContextCardView
  /** The reader's memories as last read; null until they are. */
  memories: Memory[] | null
  loadError: string | null
}>()

const emit = defineEmits<{ close: [] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const route = useRoute()
const modalPanel = inject(modalPanelKey, null)
const titleId = useId()

const card = ref<HTMLElement | null>(null)
/** Off-screen until measured, so the first paint is never at the wrong place. */
const position = ref({ x: -9999, y: -9999 })

const title = computed(() => {
  switch (props.view.kind) {
    case 'instructions':
      return t('chat.used.instructionsTitle')
    case 'memories':
      return t('chat.used.memoriesTitle')
    default:
      return t('chat.memory.card')
  }
})

// --- memories --------------------------------------------------------------------------

/** One memory as the card lists it: the row as it is now where there is one, else what the
 *  call left (`text` null when not even that is known), and why it is not there. */
interface CardEntry {
  key: string
  kind: MemoryKind | null
  text: string | null
  scope: string | null
  meta: string[]
  note: string | null
}

function scopeOf(documentId: number | null, documentName: string | null): string {
  if (documentId === null) {
    return t('chat.memory.scopeAll')
  }
  return documentName ? t('chat.memory.scopeDocument', { name: documentName }) : t('chat.memory.scopeOneDocument')
}

function rowEntry(row: Memory): CardEntry {
  const meta = [formatShortDateTime(row.updated_at, locale.value)]
  if (row.conversation_title) {
    meta.push(t('chat.memory.fromConversation', { title: row.conversation_title }))
  }
  if (row.pinned) {
    meta.push(t('chat.memory.pinned'))
  }
  return {
    key: `row-${row.id}`,
    kind: row.kind,
    text: row.text,
    scope: scopeOf(row.document_id, row.document_name),
    meta,
    note: null,
  }
}

const entries = computed<CardEntry[]>(() => {
  const view = props.view
  if (view.kind === 'instructions' || props.memories === null) {
    return []
  }
  if (view.kind === 'memories') {
    return view.ids.map((id) => {
      const row = props.memories?.find((memory) => memory.id === id)
      // A turn names what it loaded by id alone: a memory deleted since has no text left.
      return row
        ? rowEntry(row)
        : { key: `gone-${id}`, kind: null, text: null, scope: null, meta: [], note: t('chat.memory.deletedSince') }
    })
  }
  const state = memoryLineState(view.line, props.memories)
  if ('row' in state && state.row) {
    return [rowEntry(state.row)]
  }
  const snapshot = view.line.memory
  return [
    {
      key: `call-${view.line.toolUseId}`,
      kind: snapshot.kind,
      text: snapshot.text,
      scope: scopeOf(snapshot.document_id, null),
      meta: [],
      note: view.line.action === 'forgotten' ? t('chat.memory.removed') : t('chat.memory.deletedSince'),
    },
  ]
})

// --- instructions ----------------------------------------------------------------------

/** The instructions as they are now — not as the turn had them, which nothing stores. */
const instructions = ref<ResultBlock[] | null>(null)
const instructionsError = ref<string | null>(null)
const instructionsPending = ref(false)

async function loadInstructions() {
  instructionsPending.value = true
  instructionsError.value = null
  try {
    const { agents } = await listAgents()
    const text = agents.find((agent) => agent.name === 'chat')?.settings.instructions?.trim() ?? ''
    instructions.value = text ? parseResultMarkdown(text).flatMap((page) => page.blocks) : []
  } catch (error) {
    instructionsError.value = await resolve(error)
  } finally {
    instructionsPending.value = false
  }
}

watch(
  () => props.view.kind,
  (kind) => {
    if (kind === 'instructions' && instructions.value === null && !instructionsPending.value) {
      void loadInstructions()
    }
  },
  { immediate: true },
)

// --- floating ----------------------------------------------------------------------------

/** How far it keeps from the control and from the window's edges. */
const GAP = 6
const MARGIN = 8

/** The nearest box that scrolls the control: the thread. */
function scrollerOf(element: HTMLElement): HTMLElement | null {
  for (let node = element.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node)
    if (overflowY === 'auto' || overflowY === 'scroll') {
      return node
    }
  }
  return null
}

/** Under the control, or over it where there is no room under; kept inside the window. A
 *  control scrolled out of the thread's view, or gone from it, takes the card with it. */
function place() {
  const element = card.value
  if (!element) {
    return
  }
  if (!props.anchor.isConnected) {
    close()
    return
  }
  const at = props.anchor.getBoundingClientRect()
  const view = scrollerOf(props.anchor)?.getBoundingClientRect()
  if (view && (at.bottom <= view.top || at.top >= view.bottom)) {
    close()
    return
  }
  // The layout box, not the drawn one: the opening animation's scale would shrink it, and
  // the card would be placed by a size it does not have.
  const size = { width: element.offsetWidth, height: element.offsetHeight }
  const roomBelow = window.innerHeight - MARGIN - (at.bottom + GAP)
  const y =
    size.height <= roomBelow || at.top - GAP - size.height < MARGIN
      ? Math.min(at.bottom + GAP, Math.max(MARGIN, window.innerHeight - MARGIN - size.height))
      : at.top - GAP - size.height
  const x = Math.max(MARGIN, Math.min(at.left, window.innerWidth - MARGIN - size.width))
  position.value = { x, y }
}

/** Opened from another control while open: the panel swaps the view, not the card. */
watch(
  () => props.anchor,
  async () => {
    await nextTick()
    place()
    card.value?.focus()
  },
)

function close() {
  emit('close')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The card, not the dialog it floats in.
    event.stopPropagation()
    close()
  }
}

/** The control that opened it is left alone: its own click closes the card. */
function outside(target: EventTarget | null): boolean {
  return target instanceof Node && !card.value?.contains(target) && !props.anchor.contains(target)
}

function onPointerdown(event: PointerEvent) {
  if (outside(event.target)) {
    close()
  }
}

function onFocusout(event: FocusEvent) {
  if (outside(event.relatedTarget)) {
    close()
  }
}

let observer: ResizeObserver | null = null
/** Where the control stood when the card was last placed, and the frame that checks again. */
let anchorAt = ''
let frame = 0

/** The control moves with the thread — scrolled, or pushed down as an answer streams in
 *  above it, which fires no scroll — so where it stands is checked every frame the card is
 *  open, and the card placed again when that changed. Read after the frame's scrolls. */
function follow() {
  const at = props.anchor.getBoundingClientRect()
  const where = `${at.left} ${at.top} ${at.width} ${at.height}`
  if (where !== anchorAt) {
    anchorAt = where
    place()
  }
  frame = requestAnimationFrame(follow)
}

onMounted(() => {
  place()
  card.value?.focus()
  // The instructions arrive after it opens, and the memories may be read again under it.
  observer = new ResizeObserver(() => place())
  if (card.value) {
    observer.observe(card.value)
  }
  frame = requestAnimationFrame(follow)
  window.addEventListener('pointerdown', onPointerdown, true)
  window.addEventListener('resize', place)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  cancelAnimationFrame(frame)
  window.removeEventListener('pointerdown', onPointerdown, true)
  window.removeEventListener('resize', place)
  // Back to the control, unless the reader has already put focus somewhere else — without
  // scrolling the thread back to a control the reader scrolled away from.
  const active = document.activeElement
  if ((!active || active === document.body || card.value?.contains(active)) && props.anchor.isConnected) {
    props.anchor.focus({ preventScroll: true })
  }
})
</script>

<template>
  <Teleport :to="modalPanel ?? 'body'">
    <div
      ref="card"
      class="card"
      role="dialog"
      tabindex="-1"
      :aria-labelledby="titleId"
      :style="{ left: `${position.x}px`, top: `${position.y}px` }"
      @keydown="onKeydown"
      @focusout="onFocusout"
    >
      <p :id="titleId" class="card-title">{{ title }}</p>

      <template v-if="view.kind === 'instructions'">
        <p v-if="instructionsError" class="card-error" role="alert">{{ instructionsError }}</p>
        <p v-else-if="instructions === null" class="card-wait"><UiSpinner :size="12" /></p>
        <p v-else-if="!instructions.length" class="card-note">{{ t('chat.used.instructionsNone') }}</p>
        <div v-else class="instructions md-side">
          <MdBlock v-for="(block, index) in instructions" :key="index" :block="block" />
        </div>
      </template>

      <template v-else>
        <p v-if="memories === null && loadError" class="card-error" role="alert">{{ loadError }}</p>
        <p v-else-if="memories === null" class="card-wait"><UiSpinner :size="12" /></p>
        <ul v-else class="entries">
          <li v-for="entry in entries" :key="entry.key" class="entry">
            <p v-if="entry.kind" class="entry-head">
              <span class="entry-kind">{{ t(`chat.memory.kinds.${entry.kind}`) }}</span>
              <span v-if="entry.scope" class="entry-scope">{{ entry.scope }}</span>
            </p>
            <p v-if="entry.text" class="entry-text" :class="{ gone: entry.note }">
              <MdInline :text="entry.text" />
            </p>
            <p v-if="entry.meta.length" class="entry-meta">{{ entry.meta.join(' · ') }}</p>
            <p v-if="entry.note" class="entry-note">{{ entry.note }}</p>
          </li>
        </ul>
      </template>

      <NuxtLink :to="{ query: { ...route.query, [SETTINGS_QUERY]: 'agent' } }" class="manage">
        {{ t('chat.memory.manage') }}
      </NuxtLink>
    </div>
  </Teleport>
</template>

<style scoped>
/* The context menu's surface (ui/ContextMenu.vue): a floating thing is rounded, hairlined
   and translucent, the design's one exception to flat paper. */
.card {
  position: fixed;
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: max-content;
  min-width: 220px;
  max-width: min(320px, calc(100vw - 16px));
  max-height: calc(100dvh - 16px);
  overflow-y: auto;
  padding: var(--space-3);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.5;
  outline: none;
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  animation: card-in var(--duration-fast) var(--ease-enter);
}

@supports not (backdrop-filter: blur(1px)) {
  .card {
    background: var(--paper);
  }
}

@keyframes card-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .card {
    animation: none;
  }
}

.card p {
  margin: 0;
}

.card-title {
  color: var(--faint);
}

.card-error {
  color: var(--danger);
  overflow-wrap: anywhere;
}

.card-note {
  color: var(--muted);
}

.card-wait {
  display: flex;
  color: var(--faint);
}

/* The instructions at the panel's scale, as an answer is drawn. */
.instructions {
  --block-heading: var(--display-xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);

  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  overflow-wrap: anywhere;
}

.instructions :deep(.md-p),
.instructions :deep(.md-list) {
  line-height: 1.6;
}

.entries {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}

.entry {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.entry-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--faint);
}

.entry-kind {
  padding: 1px 6px;
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--ink-soft);
}

.entry-text {
  color: var(--ink);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.entry-text.gone {
  color: var(--muted);
  text-decoration: line-through;
  text-decoration-color: var(--edge);
}

.entry-meta,
.entry-note {
  color: var(--faint);
}

.manage {
  align-self: flex-start;
  color: var(--muted);
  text-decoration: underline;
  text-decoration-color: var(--edge);
  text-underline-offset: 3px;
  transition: color var(--duration-fast) var(--ease);
}

.manage:hover {
  color: var(--ink);
  text-decoration-color: currentcolor;
}
</style>
