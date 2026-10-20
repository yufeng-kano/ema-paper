<script setup lang="ts">
/**
 * One `memory` call, under the answer of its turn: a quiet line with a memory glyph saying
 * what the agent saved, changed or removed, then **View** and **Undo** (docs/web.md § The
 * agent panel, *What the answer used, and memory lines*). It is not a step.
 *
 * Which state it is in comes from the memories as they are now (`memoryLineState`), never
 * from what was clicked: an action reads the list again and the line follows it, so a
 * reload or another tab shows what is true. What was clicked picks only the words — *Undone*
 * right after this line's own Undo, *No longer in memory* for a row that went any other
 * way. Undo is the memories routes (docs/chat.md § Memory) — delete what was saved, write
 * back what was changed, save again what was removed — and a row saved again carries the
 * call's id, which is how the line finds it.
 */
import {
  createMemory,
  deleteMemory,
  updateMemory,
  type Memory,
  type MemorySnapshot,
} from '~/lib/api'
import { memoryLineState, type MemoryLine } from '~/lib/thread'

const props = defineProps<{
  line: MemoryLine
  /** The reader's memories as last read; null until they are. */
  memories: Memory[] | null
  /** The conversation the line is in: a memory saved again is credited to it. */
  conversationId: number | null
  /** Why the memories could not be read: the state is unknown, so nothing is undone. */
  loadError: string | null
  /** Reads the memories again, so the line follows what an action did. */
  reload: () => Promise<void>
  /** Its card is open. */
  viewing: boolean
}>()

const emit = defineEmits<{ view: [anchor: HTMLElement] }>()

const { t } = useI18n()
const { resolve } = useApiError()

/** This line's own Undo deleted the row it saved, while the page has been open: only then
 *  does it say *Undone*; a row that went any other way is *No longer in memory*. */
const undoneHere = ref(false)

const state = computed(() => memoryLineState(props.line, props.memories, undoneHere.value))

const kind = computed(() => t(`chat.memory.kinds.${props.line.memory.kind}`))
const noun = computed(() => t(`chat.memory.nouns.${props.line.memory.kind}`))

/** What an Undo left, in words of its own; null while the line names the memory, which is
 *  drawn with its markup — `$…$` typeset — as the card draws it. */
const undoneMessage = computed(() => {
  switch (state.value.state) {
    case 'unsaved':
      return t('chat.memory.unsaved', { noun: noun.value })
    case 'reverted':
      return t('chat.memory.reverted', { noun: noun.value })
    case 'restored':
      return t('chat.memory.restored', { noun: noun.value })
    default:
      return null
  }
})

/** What the call did, or that its memory has gone since. */
const keypath = computed(() =>
  state.value.state === 'removed' ? 'chat.memory.gone' : `chat.memory.${props.line.action}`,
)

/** The memory is not kept: removed by the call, its save undone, or gone since. */
const gone = computed(() => ['forgotten', 'unsaved', 'removed'].includes(state.value.state))

/** A saved memory no longer kept has nothing left to view; every other line does. */
const viewable = computed(() => state.value.state !== 'unsaved' && state.value.state !== 'removed')

interface LineAction {
  label: string
  /** The control's full name, where the label alone says less than it does. */
  name: string
  run: () => Promise<unknown>
}

/** The fields the `memory` tool writes, as a snapshot holds them. */
function fields(snapshot: MemorySnapshot) {
  return { kind: snapshot.kind, text: snapshot.text, document_id: snapshot.document_id }
}

/** Saves the snapshot as a new row, found again by the call's id. */
function saveAgain(snapshot: MemorySnapshot) {
  return createMemory({
    ...fields(snapshot),
    pinned: snapshot.pinned,
    conversation_id: props.conversationId,
    source_ref: props.line.toolUseId,
  })
}

const action = computed<LineAction | null>(() => {
  const current = state.value
  switch (current.state) {
    case 'saved':
      return {
        label: t('chat.memory.undo'),
        name: t('chat.memory.undoSaved'),
        run: () => deleteMemory(current.row.id),
      }
    case 'unsaved':
    case 'removed':
      return {
        label: t('chat.memory.saveAgain'),
        name: t('chat.memory.saveAgain'),
        run: () => saveAgain(props.line.memory),
      }
    case 'updated': {
      const previous = props.line.previous
      return previous
        ? {
            label: t('chat.memory.undo'),
            name: t('chat.memory.undoUpdated'),
            run: () => updateMemory(current.row.id, fields(previous)),
          }
        : null
    }
    case 'reverted':
      return {
        label: t('chat.memory.applyAgain'),
        name: t('chat.memory.applyAgain'),
        run: () => updateMemory(current.row.id, fields(props.line.memory)),
      }
    case 'forgotten':
      return {
        label: t('chat.memory.undo'),
        name: t('chat.memory.undoForgotten'),
        run: () => saveAgain(props.line.memory),
      }
    case 'restored':
      return {
        label: t('chat.memory.removeAgain'),
        name: t('chat.memory.removeAgain'),
        run: () => deleteMemory(current.row.id),
      }
    // Not read yet, changed since by someone else, or held by another row: there is
    // nothing safe to undo.
    default:
      return null
  }
})

/** Undo until the memories are read: shown, so the line does not jump, but not offered. */
const waiting = computed(() => state.value.state === 'loading')

const pending = ref(false)
const error = ref<string | null>(null)

async function act() {
  const chosen = action.value
  if (!chosen || pending.value) {
    return
  }
  const from = state.value.state
  pending.value = true
  error.value = null
  try {
    await chosen.run()
    undoneHere.value = from === 'saved'
  } catch (caught) {
    error.value = await resolve(caught)
  } finally {
    // Read whatever happened — a failed undo may still mean the row moved on.
    await props.reload()
    pending.value = false
  }
}

function view(event: MouseEvent) {
  emit('view', event.currentTarget as HTMLElement)
}
</script>

<template>
  <div class="memory-line">
    <UiIcon :name="gone ? 'bookmark-x' : 'bookmark'" />
    <p class="memory-text">
      <span role="status">
        <template v-if="undoneMessage">{{ undoneMessage }}</template>
        <i18n-t v-else :keypath="keypath" scope="global">
          <template #kind>{{ kind }}</template>
          <template #text><MdInline :text="line.memory.text" /></template>
        </i18n-t>
      </span>
      <button
        v-if="viewable"
        type="button"
        class="link"
        aria-haspopup="dialog"
        :aria-expanded="viewing ? 'true' : 'false'"
        :aria-label="t('chat.memory.viewLabel')"
        @click="view"
      >
        {{ t('chat.memory.view') }}
      </button>
      <!-- Busy rather than disabled while it runs: a disabled button drops the keyboard's
           focus, and the control stays where it is as its state changes. -->
      <button
        v-if="action"
        type="button"
        class="link"
        :aria-label="action.name"
        :aria-disabled="pending ? 'true' : undefined"
        :aria-busy="pending ? 'true' : undefined"
        @click="act"
      >
        {{ action.label }}
      </button>
      <button v-else-if="waiting && !loadError" type="button" class="link" disabled>
        {{ t('chat.memory.undo') }}
      </button>
    </p>
    <p v-if="error" class="memory-error" role="alert">{{ error }}</p>
    <p v-else-if="waiting && loadError" class="memory-error">{{ loadError }}</p>
  </div>
</template>

<style scoped>
/* Quieter than the answer it sits under: the size and color of a note, its controls text
   with a faint underline rather than buttons (docs/web.md § The agent panel). */
.memory-line {
  display: grid;
  grid-template-columns: 13px minmax(0, 1fr);
  column-gap: var(--space-2);
  align-items: start;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.memory-line > :deep(svg) {
  width: 13px;
  height: 13px;
  margin-top: 2px;
  color: var(--faint);
}

.memory-text {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  column-gap: var(--space-2);
  margin: 0;
  overflow-wrap: anywhere;
}

.link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--muted);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--edge);
  text-underline-offset: 3px;
  cursor: pointer;
  transition:
    color var(--duration-fast) var(--ease),
    text-decoration-color var(--duration-fast) var(--ease);
}

.link:hover:not(:disabled, [aria-disabled='true']),
.link[aria-expanded='true'] {
  color: var(--ink);
  text-decoration-color: currentcolor;
}

.link:disabled,
.link[aria-disabled='true'] {
  color: var(--faint);
  cursor: default;
}

.memory-error {
  grid-column: 2;
  margin: 0;
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
