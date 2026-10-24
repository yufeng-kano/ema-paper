<script setup lang="ts">
/**
 * The chat agent's **Memory** (docs/web.md § Pages, docs/chat.md § Memory): the notes the
 * agent keeps about the reader across conversations, listed from `GET /api/memories` and
 * kept by the reader — filtered by kind, searched, pinned, edited in place, deleted — under
 * the two switches the agent reads them by: *Let the agent remember on its own*
 * (`memory_auto`) and Pause / Resume (`memory_paused`). Clearing deletes every memory and
 * leaves the switches as they are.
 *
 * Every action saves at once. A row's controls wait while its own request is in flight, and
 * an answer that comes back after *Clear all memories* is dropped rather than bringing back
 * a row the reader just cleared.
 */
import {
  MEMORY_KINDS,
  clearMemories,
  deleteMemory,
  listMemories,
  putAgentSettings,
  updateMemory,
  type AgentEntry,
  type Memory,
  type MemoryFields,
  type MemoryKind,
  type SavedAgentSettings,
} from '~/lib/api'
import {
  MEMORY_FILTERS,
  countMemories,
  filterMemories,
  formatMemoryDate,
  memoryExcerpt,
  sortMemories,
  type MemoryFilter,
} from '~/lib/memory'

const props = defineProps<{ agent: AgentEntry }>()

const emit = defineEmits<{
  /** Named for the agent the request was made for, like every control on the panel. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()
/** A row's change reaches the memory block without passing through the agent's settings. */
const contextVersion = useAgentContextVersion()

const labelId = useId()
const searchId = useId()

/** `limits.memory_max_chars`; null until the session has loaded. */
const maxChars = computed(() => auth.me.value?.limits.memory_max_chars ?? null)

// --- the list -----------------------------------------------------------------------

const memories = ref<Memory[] | null>(null)
const loadError = ref<string | null>(null)
/** The last failed action on a row or a switch. */
const actionError = ref<string | null>(null)
/** The list's generation: a load, and *Clear all memories*, start a new one, and an answer
 *  from an older one is dropped. */
let generation = 0
/** The rows whose own request is in flight. */
const busy = reactive(new Set<number>())

async function load() {
  const seq = ++generation
  loadError.value = null
  try {
    const answer = await listMemories()
    if (seq === generation) {
      memories.value = answer.memories
    }
  } catch (failure) {
    if (seq === generation) {
      loadError.value = await resolve(failure)
    }
  }
}

onMounted(load)

const filter = ref<MemoryFilter>('all')
const query = ref('')
const searchInput = ref<HTMLInputElement | null>(null)

const counts = computed(() => countMemories(memories.value ?? []))
const visible = computed(() => filterMemories(sortMemories(memories.value ?? []), filter.value, query.value))

/** A row's answer, merged over the row it changed — unless the row is gone, or the list is
 *  no longer the one the request was made against. */
function replace(updated: Memory, seq: number) {
  if (seq !== generation || !memories.value) {
    return
  }
  memories.value = memories.value.map((row) => (row.id === updated.id ? { ...row, ...updated } : row))
}

function kindLabel(kind: MemoryKind): string {
  return t(`agents.memory.kinds.${kind}`)
}

function scopeLabel(memory: Pick<Memory, 'document_id' | 'document_name'>): string {
  if (memory.document_id === null) {
    return t('agents.memory.scopeAll')
  }
  return memory.document_name
    ? t('agents.memory.scopeDocument', { name: memory.document_name })
    : t('agents.memory.scopeOneDocument')
}

/** Under the text: where it applies, when it last changed, where it came from while that
 *  conversation exists, and whether every conversation loads it. */
function meta(memory: Memory): string {
  const date = formatMemoryDate(memory.updated_at, locale.value)
  return [
    scopeLabel(memory),
    date === null ? t('agents.memory.today') : date,
    memory.conversation_title ? t('agents.memory.from', { title: memory.conversation_title }) : '',
    memory.pinned ? t('agents.memory.alwaysLoaded') : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

// --- the switches -----------------------------------------------------------------------

/** The click being saved; the stored value otherwise. Put back when the save fails. */
const autoChoice = ref<boolean | null>(null)
const autoOn = computed(() => autoChoice.value ?? props.agent.settings.memory_auto ?? true)
const paused = computed(() => props.agent.settings.memory_paused ?? false)
const pausePending = ref(false)

async function toggleAuto() {
  if (autoChoice.value !== null) {
    return
  }
  const name = props.agent.name
  const next = !autoOn.value
  autoChoice.value = next
  actionError.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { memory_auto: next }))
  } catch (failure) {
    actionError.value = await resolve(failure)
  } finally {
    autoChoice.value = null
  }
}

async function togglePause() {
  const name = props.agent.name
  pausePending.value = true
  actionError.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { memory_paused: !paused.value }))
  } catch (failure) {
    actionError.value = await resolve(failure)
  } finally {
    pausePending.value = false
  }
}

// --- a row's actions --------------------------------------------------------------------

const list = ref<HTMLElement | null>(null)

/** Focus back to a row's control once the row has re-rendered (an edit closed, a pin moved
 *  it) — only when the focus was lost with what re-rendered, never taken from elsewhere. */
async function focusRowControl(id: number, control: 'edit' | 'pin') {
  await nextTick()
  const active = document.activeElement
  if (active && active !== document.body) {
    return
  }
  list.value?.querySelector<HTMLElement>(`[data-${control}="${id}"]`)?.focus()
}

async function togglePin(memory: Memory) {
  if (busy.has(memory.id)) {
    return
  }
  const seq = generation
  busy.add(memory.id)
  actionError.value = null
  try {
    replace(await updateMemory(memory.id, { pinned: !memory.pinned }), seq)
    contextVersion.changed()
  } catch (failure) {
    actionError.value = await resolve(failure)
  } finally {
    busy.delete(memory.id)
    // The button was disabled while it saved, and pinned rows sort first, so the row may
    // have moved: the focus goes back to it, wherever it is now.
    void focusRowControl(memory.id, 'pin')
  }
}

interface Draft {
  id: number
  text: string
  kind: MemoryKind
  scope: 'all' | 'document'
}

const draft = ref<Draft | null>(null)
const draftError = ref<string | null>(null)
const draftLength = computed(() => [...(draft.value?.text ?? '').trim()].length)
const draftOver = computed(() => maxChars.value !== null && draftLength.value > maxChars.value)

async function startEdit(memory: Memory) {
  draft.value = {
    id: memory.id,
    text: memory.text,
    kind: memory.kind,
    scope: memory.document_id === null ? 'all' : 'document',
  }
  draftError.value = null
  await nextTick()
  const input = list.value?.querySelector<HTMLInputElement>('.edit input')
  input?.focus()
  input?.select()
}

function cancelEdit() {
  const id = draft.value?.id
  draft.value = null
  draftError.value = null
  if (id !== undefined) {
    void focusRowControl(id, 'edit')
  }
}

/** Writes only what changed; a scope can only narrow back to every document, since a row
 *  offers no document but its own. */
async function saveEdit(memory: Memory) {
  const current = draft.value
  const text = current?.text.trim() ?? ''
  if (!current || busy.has(memory.id) || !text || draftOver.value) {
    return
  }
  const change: Partial<MemoryFields> = {}
  if (text !== memory.text) {
    change.text = text
  }
  if (current.kind !== memory.kind) {
    change.kind = current.kind
  }
  if (current.scope === 'all' && memory.document_id !== null) {
    change.document_id = null
  }
  if (Object.keys(change).length === 0) {
    cancelEdit()
    return
  }
  const seq = generation
  busy.add(memory.id)
  draftError.value = null
  try {
    replace(await updateMemory(memory.id, change), seq)
    contextVersion.changed()
    if (draft.value?.id === memory.id) {
      cancelEdit()
    }
  } catch (failure) {
    draftError.value = await resolve(failure)
  } finally {
    busy.delete(memory.id)
  }
  // Still editing after a failure: the field was disabled while it saved, and takes the
  // focus back so the reader can correct and press Enter again.
  if (draft.value?.id === memory.id) {
    await nextTick()
    if (!document.activeElement || document.activeElement === document.body) {
      list.value?.querySelector<HTMLInputElement>('.edit input')?.focus()
    }
  }
}

/** Enter saves — never the Enter that confirms an input method's composition. */
function onDraftEnter(event: KeyboardEvent, memory: Memory) {
  if (event.isComposing || event.keyCode === 229) {
    return
  }
  event.preventDefault()
  void saveEdit(memory)
}

/** The edit's own Escape: it cancels the edit and goes no further. */
function onDraftEscape(event: KeyboardEvent) {
  event.stopPropagation()
  event.preventDefault()
  cancelEdit()
}

const deleting = ref<Memory | null>(null)
const deletePending = ref(false)
const deleteError = ref<string | null>(null)

function askDelete(memory: Memory) {
  deleting.value = memory
  deleteError.value = null
}

async function confirmDelete() {
  const memory = deleting.value
  if (!memory) {
    return
  }
  deletePending.value = true
  deleteError.value = null
  busy.add(memory.id)
  try {
    await deleteMemory(memory.id)
    memories.value = (memories.value ?? []).filter((row) => row.id !== memory.id)
    contextVersion.changed()
    deleting.value = null
    // The control that opened the dialog is gone with its row.
    await nextTick()
    searchInput.value?.focus()
  } catch (failure) {
    deleteError.value = await resolve(failure)
  } finally {
    deletePending.value = false
    busy.delete(memory.id)
  }
}

const clearing = ref(false)
const clearPending = ref(false)
const clearError = ref<string | null>(null)

async function confirmClear() {
  clearPending.value = true
  clearError.value = null
  try {
    await clearMemories()
    generation += 1
    memories.value = []
    contextVersion.changed()
    draft.value = null
    clearing.value = false
    await nextTick()
    searchInput.value?.focus()
  } catch (failure) {
    clearError.value = await resolve(failure)
  } finally {
    clearPending.value = false
  }
}
</script>

<template>
  <section class="section" :aria-labelledby="labelId">
    <div class="section-head">
      <h3 :id="labelId" class="section-label">{{ t('agents.memory.label') }}</h3>
      <button
        type="button"
        role="switch"
        class="switch"
        :aria-checked="autoOn"
        :aria-busy="autoChoice !== null || undefined"
        @click="toggleAuto"
      >
        {{ t('agents.memory.auto') }}
        <span class="track" aria-hidden="true"><span class="knob" /></span>
      </button>
    </div>
    <p class="note">{{ t('agents.memory.description') }}</p>
    <p v-if="paused" class="paused-line" role="status">
      <UiIcon name="pause" class="paused-glyph" />
      {{ t('agents.memory.paused') }}
    </p>
    <!-- A switch's or a row's failed save; the list's own failure is the banner below. -->
    <UiBanner v-if="actionError" tone="error">{{ actionError }}</UiBanner>

    <UiBanner v-if="loadError" tone="error">{{ loadError }}</UiBanner>
    <UiSkeleton v-else-if="memories === null" :rows="3" />
    <template v-else>
      <div class="tools">
        <div class="filters" role="group" :aria-label="t('agents.memory.filterGroup')">
          <button
            v-for="choice in MEMORY_FILTERS"
            :key="choice"
            type="button"
            class="filter"
            :aria-pressed="filter === choice"
            @click="filter = choice"
          >
            {{ t(`agents.memory.filters.${choice}`) }}
            <span class="filter-count tabular">{{ counts[choice].toLocaleString(locale) }}</span>
          </button>
        </div>
        <div class="search">
          <label :for="searchId" class="sr-only">{{ t('agents.memory.search') }}</label>
          <UiIcon name="search" class="search-glyph" />
          <input
            :id="searchId"
            ref="searchInput"
            v-model="query"
            class="search-input"
            type="search"
            autocomplete="off"
            spellcheck="false"
            :placeholder="t('agents.memory.search')"
          >
        </div>
      </div>

      <div class="list-wrap" :class="{ dimmed: paused }">
        <ul v-if="visible.length" ref="list" class="list" :aria-label="t('agents.memory.list')">
          <template v-for="memory in visible" :key="memory.id">
            <!-- One row turned into its edit: the text, its kind and scope, Save and Cancel. -->
            <li v-if="draft?.id === memory.id" class="row editing">
              <div
                class="edit"
                role="group"
                :aria-label="t('agents.memory.edit', { text: memoryExcerpt(memory.text) })"
                @keydown.esc="onDraftEscape"
              >
                <UiField v-slot="{ id }" class="edit-text" :label="t('agents.memory.textLabel')" label-hidden>
                  <UiTextInput
                    :id="id"
                    v-model="draft.text"
                    :invalid="draftOver"
                    :disabled="busy.has(memory.id)"
                    @keydown.enter="onDraftEnter($event, memory)"
                  />
                </UiField>
                <div class="edit-controls">
                  <UiField v-slot="{ id }" class="edit-kind" :label="t('agents.memory.kindLabel')" label-hidden>
                    <UiSelect
                      :id="id"
                      :model-value="draft.kind"
                      :disabled="busy.has(memory.id)"
                      @update:model-value="draft.kind = $event as MemoryKind"
                    >
                      <option v-for="kind in MEMORY_KINDS" :key="kind" :value="kind">{{ kindLabel(kind) }}</option>
                    </UiSelect>
                  </UiField>
                  <UiField v-slot="{ id }" class="edit-scope" :label="t('agents.memory.scopeLabel')" label-hidden>
                    <UiSelect
                      :id="id"
                      :model-value="draft.scope"
                      :disabled="busy.has(memory.id)"
                      @update:model-value="draft.scope = $event === 'document' ? 'document' : 'all'"
                    >
                      <option value="all">{{ t('agents.memory.scopeAll') }}</option>
                      <option v-if="memory.document_id !== null" value="document">{{ scopeLabel(memory) }}</option>
                    </UiSelect>
                  </UiField>
                  <div class="edit-buttons">
                    <UiButton variant="secondary" :disabled="busy.has(memory.id)" @click="cancelEdit">
                      {{ t('common.cancel') }}
                    </UiButton>
                    <UiButton
                      variant="primary"
                      :loading="busy.has(memory.id)"
                      :disabled="!draft.text.trim() || draftOver"
                      @click="saveEdit(memory)"
                    >
                      {{ t('common.save') }}
                    </UiButton>
                  </div>
                </div>
                <p v-if="maxChars !== null" class="edit-count tabular" :class="{ over: draftOver }">
                  {{
                    t('agents.memory.count', {
                      count: draftLength.toLocaleString(locale),
                      max: maxChars.toLocaleString(locale),
                    })
                  }}
                </p>
                <UiBanner v-if="draftError" tone="error">{{ draftError }}</UiBanner>
              </div>
            </li>

            <li v-else class="row">
              <span class="kind">{{ kindLabel(memory.kind) }}</span>
              <div class="row-main">
                <!-- Typeset as the chat's memory card shows it; the edit field keeps the source. -->
                <p class="text">
                  <UiIcon v-if="memory.pinned" name="pin" class="pin-glyph" /><MdInline :text="memory.text" />
                </p>
                <p class="meta">{{ meta(memory) }}</p>
              </div>
              <div class="actions">
                <UiButton
                  class="action"
                  variant="ghost"
                  size="xs"
                  icon-only
                  :data-pin="memory.id"
                  :aria-pressed="memory.pinned"
                  :label="t('agents.memory.pin', { text: memoryExcerpt(memory.text) })"
                  :disabled="busy.has(memory.id)"
                  @click="togglePin(memory)"
                >
                  <template #icon><UiIcon name="pin" /></template>
                </UiButton>
                <UiButton
                  class="action"
                  variant="ghost"
                  size="xs"
                  icon-only
                  :data-edit="memory.id"
                  :label="t('agents.memory.edit', { text: memoryExcerpt(memory.text) })"
                  :disabled="busy.has(memory.id)"
                  @click="startEdit(memory)"
                >
                  <template #icon><UiIcon name="edit" /></template>
                </UiButton>
                <UiButton
                  class="action danger"
                  variant="ghost"
                  size="xs"
                  icon-only
                  aria-haspopup="dialog"
                  :label="t('agents.memory.delete', { text: memoryExcerpt(memory.text) })"
                  :disabled="busy.has(memory.id)"
                  @click="askDelete(memory)"
                >
                  <template #icon><UiIcon name="trash" /></template>
                </UiButton>
              </div>
            </li>
          </template>
        </ul>
        <p v-else class="fine empty">
          {{ memories.length ? t('agents.memory.noMatch') : t('agents.memory.empty') }}
        </p>
      </div>
    </template>

    <div class="fine-notes">
      <i18n-t keypath="agents.memory.translateNote" tag="p" class="fine" scope="global">
        <template #command><span class="mono">/translate</span></template>
      </i18n-t>
      <p class="fine">{{ t('agents.memory.referenceNote') }}</p>
    </div>

    <div class="foot">
      <UiButton variant="secondary" size="sm" :loading="pausePending" @click="togglePause">
        {{ paused ? t('agents.memory.resume') : t('agents.memory.pause') }}
      </UiButton>
      <button
        type="button"
        class="danger-text"
        aria-haspopup="dialog"
        :disabled="!memories?.length"
        @click="clearing = true"
      >
        {{ t('agents.memory.clear') }}
      </button>
    </div>

    <UiConfirmDialog
      v-if="deleting"
      :title="t('agents.memory.deleteTitle')"
      :message="t('agents.memory.deleteConfirm', { text: memoryExcerpt(deleting.text, 120) })"
      :confirm-label="t('common.delete')"
      :pending="deletePending"
      :error="deleteError"
      @confirm="confirmDelete"
      @cancel="deleting = null"
    />
    <UiConfirmDialog
      v-if="clearing"
      :title="t('agents.memory.clear')"
      :message="t('agents.memory.clearConfirm')"
      :confirm-label="t('agents.memory.clear')"
      :pending="clearPending"
      :error="clearError"
      @confirm="confirmClear"
      @cancel="clearing = false"
    />
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.section-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

/* The field label's treatment: a section of the panel, not a heading of the page. */
.section-label {
  margin: 0;
  font-family: inherit;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
}

/* A switch is a labelled button: the words, then the system's capsule switch — a gray track
   off, the accent on, the white knob sliding across. */
.switch {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height-sm);
  padding: 0 var(--space-1) 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.switch:hover {
  background: var(--fill-weak);
}

/* Not disabled while it saves — a disabled button drops the keyboard's focus — only busy. */
.switch[aria-busy='true'] {
  cursor: progress;
}

.track {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  width: 36px;
  height: 22px;
  border-radius: var(--radius-full);
  background: var(--fill-strong);
  transition: background var(--duration-fast) var(--ease);
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: var(--radius-full);
  background: var(--accent-fg);
  box-shadow: var(--shadow-segment);
  transition: left var(--duration-fast) var(--ease);
}

.switch[aria-checked='true'] .track {
  background: var(--accent);
}

.switch[aria-checked='true'] .knob {
  left: 16px;
}

.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.paused-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: var(--space-1) 0 0;
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.paused-glyph {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
}

.tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) 6px;
  margin-top: var(--space-2);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* A filter chip is a pressed toggle: a hairline at rest, the selected-row fill when on. */
.filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    border-color var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.filter:hover {
  background: var(--rail);
}

.filter[aria-pressed='true'] {
  border-color: var(--rail-active);
  background: var(--rail-active);
  color: var(--ink);
  font-weight: var(--weight-medium);
}

.filter-count {
  color: var(--faint);
  font-weight: var(--weight-normal);
}

.search {
  position: relative;
  flex: 0 1 12.5rem;
  min-width: 10rem;
  margin-left: auto;
}

.search-glyph {
  position: absolute;
  top: 0;
  bottom: 0;
  left: var(--space-2);
  width: 15px;
  height: 15px;
  margin: auto 0;
  color: var(--faint);
  pointer-events: none;
}

/* `UiTextInput`'s skin at the small control height, with room for the glyph. */
.search-input {
  width: 100%;
  min-width: 0;
  height: var(--control-height-sm);
  padding: 0 var(--space-2) 0 calc(var(--space-2) + 15px + var(--space-2));
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-sm);
}

.search-input::placeholder {
  color: var(--faint);
}

@media (pointer: coarse) {
  .search-input {
    font-size: 16px;
  }
}

/* Paused, the memories are kept but not used: the list stays readable and stands back. */
.list-wrap {
  transition: opacity var(--duration-fast) var(--ease);
}

.list-wrap.dimmed {
  opacity: 0.5;
}

/* One grid for every row, so the kind tags share a column as wide as the longest of them
   in the reader's language. The gap is the list's, which each row's subgrid inherits: a
   gap set on the row instead would only be split between the cells on either side of a
   line, and the text would start half a gap closer to its tag. */
.list {
  display: grid;
  grid-template-columns: minmax(var(--space-14), max-content) minmax(0, 1fr) auto;
  column-gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid var(--line-soft);
}

.row {
  display: grid;
  grid-column: 1 / -1;
  grid-template-columns: subgrid;
  align-items: start;
  padding: 9px 0;
  border-bottom: 1px solid var(--line-soft);
}

.row:last-child {
  border-bottom: 0;
}

.kind {
  justify-self: start;
  margin-top: 1px;
  padding: 0 6px;
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: nowrap;
}

.row-main {
  min-width: 0;
}

.text {
  margin: 0;
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.pin-glyph {
  width: 12px;
  height: 12px;
  margin-right: var(--space-1);
  vertical-align: -1px;
  color: var(--muted);
}

.meta {
  margin: 2px 0 0;
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.actions .action {
  color: var(--faint);
}

.actions .action:hover {
  color: var(--ink);
}

.actions .action[aria-pressed='true'] {
  background: var(--rail-active);
  color: var(--ink);
}

.actions .action.danger:hover {
  background: var(--danger-hover);
  color: var(--danger);
}

.actions .action :deep(svg) {
  width: 14px;
  height: 14px;
}

/* The edit takes the whole row: the text on its own line, the dropdowns and buttons under. */
.row.editing {
  display: block;
}

.edit {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.edit-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.edit-kind {
  flex: 0 1 10rem;
  min-width: 8rem;
}

.edit-scope {
  flex: 1 1 12rem;
  min-width: 10rem;
}

.edit-buttons {
  display: flex;
  gap: var(--space-2);
  margin-left: auto;
}

.edit-count {
  margin: 0;
  color: var(--faint);
  font-size: var(--text-2xs);
  text-align: right;
}

.edit-count.over {
  color: var(--danger);
}

.fine {
  margin: 0;
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.empty {
  padding: var(--space-4) 0;
}

.fine-notes {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

/* Destructive and worded, so no box: the danger hue, and its wash on hover. */
.danger-text {
  height: var(--control-height-sm);
  padding: 0 var(--space-1);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--danger);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.danger-text:hover {
  background: var(--danger-hover);
}

.danger-text:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
