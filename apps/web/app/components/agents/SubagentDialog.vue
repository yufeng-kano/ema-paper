<script setup lang="ts">
/**
 * One subagent, opened from the Agent page's Subagents table (docs/web.md § Pages,
 * docs/chat.md § Subagents): the skill dialog's shape — name, model, when to use, whether
 * it may change files, its tools and its prompt — with **Restore default** on a built-in
 * and **Delete subagent** on one of the reader's own. The model list is the chat agent's live
 * catalog, after *Follows the conversation*.
 */
import { DESCRIPTION_MAX, NAME_RULE, charLength } from '~/lib/agents'
import {
  createSubagent,
  deleteSubagent,
  SUBAGENT_TOOLS,
  updateSubagent,
  WRITING_TOOLS,
  type AgentModel,
  type Subagent,
  type SubagentFields,
} from '~/lib/api'
import { EDITOR_TOOL } from '~/lib/steps'

const props = defineProps<{
  /** Null for a new subagent. */
  subagent: Subagent | null
  /** The chat agent's catalog; null while it loads or when it failed. */
  models: AgentModel[] | null
  maxChars: number | null
}>()

const emit = defineEmits<{
  close: []
  saved: [subagent: Subagent, previous: string | null]
  deleted: [name: string]
}>()

const { t } = useI18n()
const { resolve } = useApiError()

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

const builtin = computed(() => props.subagent?.builtin === true)
const nameInvalid = computed(() => name.value !== '' && !NAME_RULE.test(name.value))
const isDefault = computed(() => props.subagent?.default != null && sameFields(fields, props.subagent.default))
const changed = computed(() => name.value !== initialName || !sameFields(fields, initial))
const length = computed(() => charLength(fields.prompt))
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

const heading = computed(() =>
  props.subagent ? t('agents.subagentDialogTitle', { name: props.subagent.name }) : t('agents.subagentDialogNew'),
)

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
  <AgentsDefinitionDialog
    :heading="heading"
    :item="subagent"
    :form-id="`subagent-form-${subagent?.name ?? 'new'}`"
    :is-default="isDefault"
    :delete-label="t('agents.subagentDelete')"
    :delete-confirm="subagent ? t('agents.subagentDeleteConfirm', { name: subagent.name }) : ''"
    :pending="pending"
    :error="error"
    :save-disabled="saveDisabled"
    @close="emit('close')"
    @save="save"
    @restore="restoreDefault"
    @delete="remove"
    @clear-error="error = null"
  >
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
          <ModelOptions :models="models" :current="fields.model" />
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
      <AgentsCharCount v-if="maxChars !== null" class="end-count mono" :count="length" :max="maxChars" />
    </div>
  </AgentsDefinitionDialog>
</template>

<style scoped>
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
</style>
