<script setup lang="ts">
/**
 * One subagent, opened from the Agent page's Subagents table (docs/web.md § Pages,
 * docs/chat.md § Subagents): the skill dialog's shape — name, model, when to use, whether
 * it may change files, its tools and its prompt — with **Restore default** on a built-in
 * and **Delete subagent** on one of the reader's own. The model list is the chat agent's live Claude
 * catalog, after *Follows the conversation*.
 */
import {
  createSubagent,
  deleteSubagent,
  SUBAGENT_TOOLS,
  updateSubagent,
  WRITING_TOOLS,
  type ConnectionModel,
  type Subagent,
  type SubagentFields,
} from '~/lib/api'

const props = defineProps<{
  /** Null for a new subagent. */
  subagent: Subagent | null
  /** The chat agent's catalog; null while it loads or when it failed. */
  models: ConnectionModel[] | null
  maxChars: number | null
}>()

const emit = defineEmits<{
  close: []
  saved: [subagent: Subagent, previous: string | null]
  deleted: [name: string]
}>()

const { t } = useI18n()
const { resolve } = useApiError()

const NAME_RULE = /^[a-z0-9][a-z0-9-]{0,63}$/
const DESCRIPTION_MAX = 1024
const EDITOR_TOOL = 'str_replace_based_edit_tool'

function fieldsOf(subagent: Subagent | null): SubagentFields {
  return subagent
    ? {
        description: subagent.description,
        model: subagent.model,
        writes: subagent.writes,
        tools: [...subagent.tools],
        prompt: subagent.prompt,
      }
    : {
        description: '',
        model: null,
        writes: false,
        tools: SUBAGENT_TOOLS.filter((tool) => !WRITING_TOOLS.includes(tool)),
        prompt: '',
      }
}

/** Tools compared as a set, in the one order the checkboxes list them. */
function sameFields(left: SubagentFields, right: SubagentFields): boolean {
  const tools = (list: readonly string[]) => SUBAGENT_TOOLS.filter((tool) => list.includes(tool)).join(',')
  return (
    left.description === right.description &&
    left.model === right.model &&
    left.writes === right.writes &&
    left.prompt === right.prompt &&
    tools(left.tools) === tools(right.tools)
  )
}

const initialName = props.subagent?.name ?? ''
const initial = fieldsOf(props.subagent)
const name = ref(initialName)
const fields = reactive<SubagentFields>(fieldsOf(props.subagent))
const pending = ref(false)
const error = ref<string | null>(null)
const confirming = ref(false)

const builtin = computed(() => props.subagent?.builtin === true)
const nameInvalid = computed(() => name.value !== '' && !NAME_RULE.test(name.value))
const isDefault = computed(() => props.subagent?.default != null && sameFields(fields, props.subagent.default))
const changed = computed(() => name.value !== initialName || !sameFields(fields, initial))
const length = computed(() => [...fields.prompt.trim()].length)
const overCap = computed(() => props.maxChars !== null && length.value > props.maxChars)
const saveDisabled = computed(
  () =>
    !changed.value ||
    !NAME_RULE.test(name.value) ||
    !fields.description.trim() ||
    fields.description.length > DESCRIPTION_MAX ||
    !fields.prompt.trim() ||
    overCap.value,
)

/** A stored model the catalog does not list is still the current choice, so the picker
 *  shows it rather than silently blanking it. */
const modelInjected = computed(() => {
  const current = fields.model
  return current && !(props.models ?? []).some((model) => model.id === current) ? current : null
})

const heading = computed(() =>
  props.subagent ? t('agents.subagentDialogTitle', { name: props.subagent.name }) : t('agents.subagentDialogNew'),
)
const state = computed(() => {
  if (!props.subagent) {
    return ''
  }
  const kind = props.subagent.builtin ? t('agents.builtIn') : t('agents.custom')
  return props.subagent.modified ? `${kind} ${t('agents.editedSuffix')}` : kind
})

function toggleTool(tool: string, on: boolean) {
  fields.tools = on ? [...fields.tools.filter((entry) => entry !== tool), tool] : fields.tools.filter((entry) => entry !== tool)
}

/** Turned off, it takes the tools that need it along: the API refuses `bash` without it. */
function toggleWrites(on: boolean) {
  fields.writes = on
  if (!on) {
    fields.tools = fields.tools.filter((tool) => !WRITING_TOOLS.includes(tool))
  }
}

function restoreDefault() {
  const shipped = props.subagent?.default
  if (shipped) {
    Object.assign(fields, { ...shipped, tools: [...shipped.tools] })
  }
}

async function save() {
  if (pending.value || saveDisabled.value) {
    return
  }
  pending.value = true
  error.value = null
  const written: SubagentFields = {
    description: fields.description.trim(),
    model: fields.model,
    writes: fields.writes,
    tools: SUBAGENT_TOOLS.filter((tool) => fields.tools.includes(tool)),
    prompt: fields.prompt.trim(),
  }
  try {
    const subagent = props.subagent
    const saved = subagent
      ? await updateSubagent(subagent.name, {
          ...written,
          ...(!subagent.builtin && name.value !== subagent.name ? { name: name.value } : {}),
        })
      : await createSubagent({ name: name.value, ...written })
    emit('saved', saved, subagent?.name ?? null)
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}

async function remove() {
  const subagent = props.subagent
  if (!subagent || pending.value) {
    return
  }
  pending.value = true
  error.value = null
  try {
    await deleteSubagent(subagent.name)
    emit('deleted', subagent.name)
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <!-- The confirmation stands in for the dialog rather than over it: two dialogs would
       both answer Escape and both trap Tab. The fields keep their state behind it. -->
  <UiConfirmDialog
    v-if="confirming && subagent"
    :title="t('agents.subagentDelete')"
    :message="t('agents.subagentDeleteConfirm', { name: subagent.name })"
    :confirm-label="t('common.delete')"
    :pending="pending"
    :error="error"
    @confirm="remove"
    @cancel="confirming = false; error = null"
  />
  <UiModal v-else size="wide" :title="heading" :busy="pending" @close="emit('close')">
    <template #title>
      <div class="heading">
        <h2 class="heading-title">{{ heading }}</h2>
        <span v-if="state" class="heading-state">{{ state }}</span>
      </div>
    </template>

    <form :id="`subagent-form-${subagent?.name ?? 'new'}`" class="form" @submit.prevent="save">
      <div class="pair">
        <UiField v-slot="{ id, describedBy }" :label="t('agents.nameLabel')" :error="nameInvalid ? t('agents.nameRule') : undefined">
          <UiTextInput
            :id="id"
            v-model="name"
            mono
            required
            :maxlength="64"
            :described-by="describedBy"
            :invalid="nameInvalid"
            :disabled="pending || builtin"
          />
        </UiField>
        <UiField v-slot="{ id }" :label="t('agents.subagentModel')">
          <UiSelect
            :id="id"
            :model-value="fields.model ?? ''"
            :disabled="pending"
            @update:model-value="fields.model = $event || null"
          >
            <option value="">{{ t('agents.followsConversation') }}</option>
            <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
            <option v-for="model in models ?? []" :key="model.id" :value="model.id">
              {{ model.name ? `${model.name} · ${model.id}` : model.id }}
            </option>
          </UiSelect>
        </UiField>
      </div>

      <UiField v-slot="{ id }" :label="t('agents.whenLabel')">
        <textarea
          :id="id"
          v-model="fields.description"
          class="area"
          rows="2"
          :maxlength="DESCRIPTION_MAX"
          :disabled="pending"
        />
      </UiField>

      <fieldset class="tools">
        <legend class="legend">{{ t('agents.subagentTools') }}</legend>
        <label class="writes">
          <input
            type="checkbox"
            :checked="fields.writes"
            :disabled="pending"
            @change="toggleWrites(($event.target as HTMLInputElement).checked)"
          >
          <span>{{ t('agents.subagentWrites') }}</span>
        </label>
        <div class="tool-row">
          <label
            v-for="tool in SUBAGENT_TOOLS"
            :key="tool"
            class="tool"
            :class="{ off: WRITING_TOOLS.includes(tool) && !fields.writes }"
          >
            <input
              type="checkbox"
              :checked="fields.tools.includes(tool)"
              :disabled="pending || (WRITING_TOOLS.includes(tool) && !fields.writes)"
              @change="toggleTool(tool, ($event.target as HTMLInputElement).checked)"
            >
            <span class="mono">{{ tool }}</span>
            <span v-if="tool === EDITOR_TOOL && !fields.writes" class="tool-note">{{ t('agents.viewOnly') }}</span>
            <span v-else-if="WRITING_TOOLS.includes(tool) && !fields.writes" class="tool-note">{{ t('agents.needsWrites') }}</span>
          </label>
        </div>
      </fieldset>

      <div class="prompt-field">
        <label :for="`subagent-prompt-${subagent?.name ?? 'new'}`" class="legend">{{ t('agents.subagentPrompt') }}</label>
        <textarea
          :id="`subagent-prompt-${subagent?.name ?? 'new'}`"
          v-model="fields.prompt"
          class="area mono-area"
          rows="10"
          spellcheck="false"
          :disabled="pending"
        />
        <p v-if="maxChars !== null" class="count mono tabular" :class="{ over: overCap }">
          {{ t('agents.promptCount', { count: length.toLocaleString(), max: maxChars.toLocaleString() }) }}
        </p>
      </div>

      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>

    <template #footer>
      <UiButton
        v-if="subagent?.builtin"
        variant="ghost"
        class="foot-start"
        :disabled="pending || isDefault"
        @click="restoreDefault"
      >
        <template #icon><UiIcon name="rotate-ccw" /></template>
        {{ t('agents.promptRestore') }}
      </UiButton>
      <UiButton
        v-else-if="subagent"
        variant="ghost"
        class="foot-start danger"
        :disabled="pending"
        @click="confirming = true; error = null"
      >
        <template #icon><UiIcon name="trash" /></template>
        {{ t('agents.subagentDelete') }}
      </UiButton>
      <UiButton variant="secondary" :disabled="pending" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton
        variant="primary"
        type="submit"
        :form="`subagent-form-${subagent?.name ?? 'new'}`"
        :loading="pending"
        :disabled="saveDisabled"
      >
        {{ t('common.save') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.heading {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  min-width: 0;
}

.heading-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-sm);
  letter-spacing: normal;
}

.heading-state {
  flex-shrink: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
}

@media (max-width: 560px) {
  .pair {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* The field label's treatment, for a legend and a label outside `UiField`. */
.legend {
  padding: 0;
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
}

.tools {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.tools .legend {
  margin-bottom: var(--space-2);
}

.tool-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* A checkbox chip per tool: the tool's name in mono on a hairline. */
.tool {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height-sm);
  padding: 0 10px 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  color: var(--ink-soft);
  cursor: pointer;
}

.tool input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--accent);
}

.tool .mono {
  font-size: var(--text-2xs);
}

.tool-note {
  color: var(--faint);
  font-size: var(--text-2xs);
}

/* A tool the switch above has not allowed: there, but not to be picked. */
.tool.off {
  color: var(--faint);
  cursor: not-allowed;
}

.writes {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  justify-self: start;
  color: var(--ink-soft);
  font-size: var(--text-sm);
  cursor: pointer;
}

.writes input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--accent);
}

.prompt-field {
  display: grid;
  gap: var(--space-2);
}

/* Same skin as UiTextInput's `.control`, for a text of several lines. */
.area {
  width: 100%;
  min-width: 0;
  padding: var(--space-2) 10px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.5;
  resize: vertical;
  transition: border-color var(--duration-fast) var(--ease);
}

.mono-area {
  padding: 10px var(--space-3);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.area:disabled {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

@media (pointer: coarse) {
  .area {
    font-size: 16px;
  }
}

.count {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-2xs);
  text-align: right;
}

.count.over {
  color: var(--danger);
}

.foot-start {
  margin-right: auto;
}

.foot-start.danger {
  color: var(--danger);
}
</style>
