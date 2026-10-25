<script setup lang="ts">
/**
 * The document's conversations, opened from the agent card's title (docs/web.md § The agent
 * panel, *Conversations*). A popover inside the card rather than a menu: each row holds a
 * second button, its **⋯**, which opens the real menu of *Rename* and *Delete*.
 *
 * The panel owns the list and what a choice does; this owns how the popover behaves. It stands
 * under the title, as tall as its rows need and no taller than the room the panel measured,
 * the rows scrolling inside it and *New conversation* fixed at its foot. ↑ ↓ Home End move
 * between the rows and the foot, Tab reaches a row's ⋯, Escape closes it back to the title;
 * a press outside it, focus leaving it or the window resizing closes it too. The title it
 * opened from (`anchor`) is not outside — a press there is the title's own toggle — and nor is
 * the row menu while it is open, which is drawn outside it.
 *
 * A rename is a field in place of the row: Enter or a click elsewhere keeps what it holds,
 * Escape leaves the title as it was, and a field left blank changes nothing (`renamedTitle`).
 */
import type { Conversation } from '~/lib/api'
import { renamedTitle } from '~/lib/conversations'
import type { MenuItem, Point } from '~/lib/menu'

const props = defineProps<{
  conversations: readonly Conversation[]
  currentId: number | null
  /** The list is being read. */
  pending: boolean
  /** Why it could not be read. */
  error: string | null
  /** Why the last rename was refused. */
  renameError: string | null
  /** Where it stands in the card, and how far below that it may reach, in pixels. */
  top: number
  room: number
  /** The server's cap on a title. */
  titleMax?: number
  /** Opened from the keyboard: the open conversation's row takes focus. */
  fromKey: boolean
  anchor: HTMLElement | null
}>()

const emit = defineEmits<{
  select: [id: number]
  new: []
  rename: [id: number, title: string]
  delete: [row: Conversation]
  retry: []
  /** `refocus`: Escape, which goes back to the title. */
  close: [refocus: boolean]
}>()

const { t } = useI18n()

const root = ref<HTMLElement | null>(null)

/** The scrolling part has something to hold: rows, *Loading…*, or why they could not be read. */
const rowsShown = computed(() => props.conversations.length > 0 || props.pending || props.error !== null)
/** Nothing above the foot: no row, nothing loading, no line to say. */
const alone = computed(() => !rowsShown.value && !props.renameError)

function label(row: Conversation): string {
  return row.title || t('chat.untitled')
}

function rowButton(id: number | undefined): HTMLElement | null {
  return root.value?.querySelector<HTMLElement>(`.row-open[data-id="${id}"]`) ?? null
}

// --- keys, presses and focus ----------------------------------------------------------------

/** What the arrows move between: each row's own button, then the foot's. */
function stops(): HTMLElement[] {
  return [...(root.value?.querySelectorAll<HTMLElement>('.row-open') ?? [])]
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The popover's Escape, not the viewer's: it closes back to the title.
    event.preventDefault()
    event.stopPropagation()
    emit('close', true)
    return
  }
  if (event.target instanceof HTMLInputElement) {
    return
  }
  const all = stops()
  // From a row's ⋯, the arrows go on from its row.
  const at = event.target instanceof Element ? event.target.closest('li, .foot')?.querySelector('.row-open') : null
  const from = all.indexOf(at as HTMLElement)
  const last = all.length - 1
  const moves: Record<string, number> = {
    ArrowDown: from < 0 ? 0 : Math.min(from + 1, last),
    ArrowUp: from < 0 ? last : Math.max(from - 1, 0),
    Home: 0,
    End: last,
  }
  const to = moves[event.key]
  if (to !== undefined) {
    event.preventDefault()
    all[to]?.focus()
  }
}

function onPointerDownOutside(event: PointerEvent) {
  const target = event.target as Node | null
  // The row menu closes itself on a press outside it, and says where (`onRowMenuClose`).
  if (rowMenu.value || root.value?.contains(target) || props.anchor?.contains(target)) {
    return
  }
  emit('close', false)
}

/** Focus going anywhere outside closes it — but not to the row menu, which takes it while
 *  open and gives it back as it closes, nor to nothing, as when the window loses focus, nor
 *  to the title, whose click that is and which closes it itself. */
function onFocusOut(event: FocusEvent) {
  const next = event.relatedTarget as Node | null
  if (rowMenu.value || next === null || root.value?.contains(next) || props.anchor?.contains(next)) {
    return
  }
  emit('close', false)
}

function onResize() {
  emit('close', false)
}

/** An Escape that reaches the document while it is open was pressed with focus on nothing —
 *  a row that went as the list was read again — and is still its own: the root says so
 *  (`data-escape-owner`), so the viewer's dialog does not close under it. One pressed inside
 *  it, in the row menu or in the rename field never gets here. */
function onDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    emit('close', true)
  }
}

/** A Retry that brought the list back goes with the failure it stood in, and focus would fall
 *  to the page, out of reach of the arrows: the popover takes it, as when it opened. */
watch(
  () => props.error,
  (error, before) => {
    if (before && !error && (document.activeElement === null || document.activeElement === document.body)) {
      root.value?.focus()
    }
  },
  { flush: 'post' },
)

onMounted(async () => {
  window.addEventListener('pointerdown', onPointerDownOutside, true)
  window.addEventListener('resize', onResize)
  document.addEventListener('keydown', onDocumentKeydown)
  await nextTick()
  const current = root.value?.querySelector<HTMLElement>('.row-open[aria-current="true"]') ?? null
  current?.scrollIntoView({ block: 'nearest' })
  if (props.fromKey) {
    const first = current ?? stops()[0]
    first?.focus()
  } else {
    // The popover, not a row: nothing is highlighted until the pointer or an arrow picks one,
    // while the keys still have somewhere to land.
    root.value?.focus()
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onPointerDownOutside, true)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('keydown', onDocumentKeydown)
  // Closed by a press elsewhere while a name was being typed: that press keeps it.
  endRename(true)
})

// --- a row's menu -----------------------------------------------------------------------------

const rowMenu = ref<{ row: Conversation; at: Point; fromKey: boolean } | null>(null)
/** When the row menu last closed, and whose it was: the press on that ⋯ that closed it is
 *  followed by the ⋯'s click, which must not open it again. */
let rowMenuClosedAt = 0
let rowMenuClosedId: number | null = null

const rowMenuItems = computed<MenuItem[]>(() => [
  { key: 'rename', label: t('chat.rename'), icon: 'edit' },
  { key: 'delete', label: t('common.delete'), icon: 'trash', danger: true, separated: true },
])

/** Anchored under the ⋯'s right edge, at the row's right: `placeMenu` opens it leftwards. */
function toggleRowMenu(row: Conversation, event: MouseEvent) {
  if (rowMenu.value?.row.id === row.id || (rowMenuClosedId === row.id && performance.now() - rowMenuClosedAt < 200)) {
    rowMenu.value = null
    return
  }
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
  // A click the keyboard made carries no pointer detail.
  rowMenu.value = { row, at: { x: box.right, y: box.bottom + 4 }, fromKey: event.detail === 0 }
}

function onRowMenuSelect(key: string) {
  const row = rowMenu.value?.row
  if (!row) {
    return
  }
  if (key === 'rename') {
    void startRename(row)
  } else if (key === 'delete') {
    emit('delete', row)
  }
}

/** A press outside both the menu and the popover closes both. */
function onRowMenuClose(cause?: Event) {
  rowMenuClosedId = rowMenu.value?.row.id ?? null
  rowMenuClosedAt = performance.now()
  rowMenu.value = null
  const target = cause instanceof PointerEvent ? (cause.target as Node | null) : null
  if (target && !root.value?.contains(target) && !props.anchor?.contains(target)) {
    emit('close', false)
  }
}

// --- rename -------------------------------------------------------------------------------------

const renaming = ref<{ id: number; before: string | null; text: string } | null>(null)

/** Found in place rather than by a template ref, which inside the rows' `v-for` is a list. */
function renameInput(): HTMLInputElement | null {
  return root.value?.querySelector<HTMLInputElement>('input.rename') ?? null
}

async function startRename(row: Conversation) {
  renaming.value = { id: row.id, before: row.title, text: row.title ?? '' }
  await nextTick()
  renameInput()?.focus()
  renameInput()?.select()
}

/** Ends the field, sending what it holds when `keep` and it changes the title. */
function endRename(keep: boolean) {
  const editing = renaming.value
  if (!editing) {
    return
  }
  renaming.value = null
  const title = keep ? renamedTitle(editing.text, editing.before, props.titleMax) : null
  if (title !== null) {
    emit('rename', editing.id, title)
  }
}

/** Enter keeps it and Escape does not; both go back to the row, and neither reaches the
 *  popover. An Enter or Escape that ends an input method's composition only ends it, and
 *  goes no further either: the popover's Escape would close it and keep the half-typed name. */
async function onRenameKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) {
    event.stopPropagation()
    return
  }
  if (event.key !== 'Enter' && event.key !== 'Escape') {
    return
  }
  event.preventDefault()
  event.stopPropagation()
  const id = renaming.value?.id
  endRename(event.key === 'Enter')
  // Focus is held by the popover while the field gives way to its row — the field leaving is
  // not focus leaving — and then goes to the row.
  root.value?.focus()
  await nextTick()
  rowButton(id)?.focus()
}

/** A click elsewhere keeps the name. The window losing focus does not end the edit: the
 *  field still holds focus there, and gets it back. */
function onRenameBlur() {
  if (document.activeElement !== renameInput()) {
    endRename(true)
  }
}
</script>

<template>
  <div
    ref="root"
    class="conversations"
    role="dialog"
    data-escape-owner
    tabindex="-1"
    :aria-label="t('chat.conversations')"
    :style="{ top: `${top}px`, '--room': `${room}px` }"
    @keydown="onKeydown"
    @focusout="onFocusOut"
  >
    <div v-if="rowsShown" class="body">
      <p v-if="pending && !conversations.length" class="status">{{ t('common.loading') }}</p>
      <div v-if="error" class="status failed" role="alert">
        <span>{{ error }}</span>
        <UiButton size="xs" variant="ghost" @click="emit('retry')">{{ t('common.retry') }}</UiButton>
      </div>
      <ul v-if="conversations.length" class="rows">
        <li
          v-for="row in conversations"
          :key="row.id"
          class="row"
          :class="{ renaming: renaming?.id === row.id }"
        >
          <template v-if="renaming?.id === row.id">
            <span class="mark"><UiIcon v-if="row.id === currentId" name="check" /></span>
            <UiTextInput
              v-model="renaming.text"
              class="rename"
              :aria-label="t('chat.renameLabel')"
              :maxlength="titleMax"
              @keydown="onRenameKeydown"
              @blur="onRenameBlur"
            />
          </template>
          <template v-else>
            <button
              type="button"
              class="row-open"
              :data-id="row.id"
              :aria-current="row.id === currentId ? 'true' : undefined"
              :title="label(row)"
              @click="emit('select', row.id)"
            >
              <span class="mark"><UiIcon v-if="row.id === currentId" name="check" /></span>
              <span class="row-title">{{ label(row) }}</span>
            </button>
            <button
              type="button"
              class="row-more"
              aria-haspopup="menu"
              :aria-expanded="rowMenu?.row.id === row.id ? 'true' : 'false'"
              :aria-label="t('chat.moreActions', { title: label(row) })"
              :title="t('chat.moreActions', { title: label(row) })"
              @click="toggleRowMenu(row, $event)"
            >
              <UiIcon name="ellipsis" />
            </button>
          </template>
        </li>
      </ul>
    </div>
    <!-- Out of the rows' scroll, so it shows whichever row was renamed. -->
    <p v-if="renameError" class="status failed rename-failed" role="alert">
      {{ t('chat.renameFailed', { error: renameError }) }}
    </p>
    <!-- Fixed at the foot, under the rows however many there are. -->
    <div class="foot" :class="{ alone }">
      <button type="button" class="row-open" @click="emit('new')">
        <span class="mark"><UiIcon name="plus" /></span>
        <span class="row-title">{{ t('chat.newConversation') }}</span>
      </button>
    </div>
  </div>

  <UiContextMenu
    v-if="rowMenu"
    :at="rowMenu.at"
    :items="rowMenuItems"
    :label="t('chat.moreActions', { title: label(rowMenu.row) })"
    :focus-first="rowMenu.fromKey"
    @select="onRowMenuSelect"
    @close="onRowMenuClose"
  />
</template>

<style scoped>
/* The context menu's opaque surface, inside the card under its title, 8px in from its sides
   (docs/web.md § The agent panel, *Conversations*). As tall as the rows need and at most down
   to `--room` — the thread's foot, 8px above the composer's field — but never less than five
   rows and the foot, however short the card. */
.conversations {
  position: absolute;
  right: var(--space-2);
  left: var(--space-2);
  z-index: 5;
  display: flex;
  flex-direction: column;
  max-height: max(calc(6 * var(--control-height-sm) + 4 * var(--space-1) + 3px), var(--room));
  overflow: hidden;
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  outline: none;
  animation: list-in var(--duration-fast) var(--ease-enter);
}

@keyframes list-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .conversations {
    animation: none;
  }
}

/* The rows scroll inside it; the foot stays. */
.body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-1);
}

.status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-2) var(--space-3);
  color: var(--faint);
  font-size: var(--text-xs);
}

.status.failed {
  color: var(--danger);
}

/* Under the rows, out of their scroll, above the foot's hairline. */
.rename-failed {
  flex: none;
}

.rows {
  margin: 0;
  padding: 0;
  list-style: none;
}

/* Filled with the rail, not the accent: the row holds a second button, and an accent fill
   would claim the whole row is one action (the outline panel's rows). */
.row {
  display: flex;
  align-items: center;
  height: var(--control-height-sm);
  border-radius: var(--radius-menu-item);
  transition: background var(--duration-fast) var(--ease);
}

.row:hover,
.row:focus-within {
  background: var(--fill-weak);
}

.row.renaming {
  gap: var(--space-2);
  padding: 0 var(--space-1) 0 var(--space-3);
  background: none;
}

.row-open {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  height: 100%;
  padding: 0 var(--space-2) 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}

.row-open:focus-visible {
  outline-offset: -2px;
}

.mark {
  display: inline-flex;
  flex: none;
  width: 16px;
  color: var(--ink);
}

.mark > :deep(svg) {
  width: 16px;
  height: 16px;
}

.row-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Drawn on the row under the pointer or holding focus — and on the one whose menu is open —
   keeping its room either way, so a title does not re-cut as the pointer passes. Faded
   out rather than hidden, so a screen reader still finds it and Shift+Tab still stops on it. */
.row-more {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  margin-right: var(--space-1);
  padding: 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--faint);
  cursor: pointer;
  opacity: 0;
  transition:
    opacity var(--duration-fast) var(--ease),
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.row:hover .row-more,
.row:focus-within .row-more,
.row-more[aria-expanded='true'] {
  opacity: 1;
}

/* No hover to reveal it: always there. */
@media (hover: none) {
  .row-more {
    opacity: 1;
  }
}

.row-more:hover,
.row-more[aria-expanded='true'] {
  background: var(--fill);
  color: var(--ink);
}

.row-more > :deep(svg) {
  width: 15px;
  height: 15px;
}

/* Above the field's own height and padding, whichever stylesheet loads last; its type size,
   16px under a coarse pointer, stays its own. */
.row .rename {
  flex: 1 1 auto;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
}

/* New conversation, under a hairline below the rows — alone when there are none. */
.foot {
  flex: none;
  padding: var(--space-1);
  border-top: 1px solid var(--menu-edge);
}

.foot.alone {
  border-top: 0;
}

.foot .row-open {
  width: 100%;
  height: var(--control-height-sm);
  transition: background var(--duration-fast) var(--ease);
}

.foot .row-open:hover,
.foot .row-open:focus-visible {
  background: var(--fill-weak);
}

.foot .mark {
  color: var(--muted);
}
</style>
