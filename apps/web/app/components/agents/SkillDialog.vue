<script setup lang="ts">
/**
 * One skill, opened from the Agent page's Skills table (docs/web.md § Pages, docs/chat.md
 * § Skills): its name, argument hint, when to use it, the two switches and its body. A
 * built-in keeps its name and offers **Restore default**, which refills the fields for Save
 * to write; one of the reader's own offers **Delete skill** behind a confirmation. Opened
 * with no skill, it is a new one of the reader's own.
 */
import { DESCRIPTION_MAX, NAME_RULE, charLength } from '~/lib/agents'
import { createSkill, deleteSkill, updateSkill, type Skill, type SkillFields } from '~/lib/api'
import { insertAtCaret } from '~/lib/caret'

const props = defineProps<{
  /** Null for a new skill. */
  skill: Skill | null
  maxChars: number | null
}>()

const emit = defineEmits<{
  close: []
  /** `previous` is the name it had: a skill of the reader's own can be renamed. */
  saved: [skill: Skill, previous: string | null]
  deleted: [name: string]
}>()

const { t } = useI18n()
const { resolve } = useApiError()

/** The API's `ARGUMENT_HINT_MAX_CHARS`. */
const HINT_MAX = 128

const EMPTY: SkillFields = { description: '', argument_hint: '', body: '', user_invocable: true, model_invocable: true }

function fieldsOf(skill: Skill | null): SkillFields {
  return skill
    ? {
        description: skill.description,
        argument_hint: skill.argument_hint,
        body: skill.body,
        user_invocable: skill.user_invocable,
        model_invocable: skill.model_invocable,
      }
    : { ...EMPTY }
}

const initialName = props.skill?.name ?? ''
const initial = fieldsOf(props.skill)
const name = ref(initialName)
const fields = reactive<SkillFields>({ ...initial })
const body = ref<HTMLTextAreaElement | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)

const builtin = computed(() => props.skill?.builtin === true)
const same = (left: SkillFields, right: SkillFields) =>
  (Object.keys(left) as Array<keyof SkillFields>).every((key) => left[key] === right[key])

const nameInvalid = computed(() => name.value !== '' && !NAME_RULE.test(name.value))
const isDefault = computed(() => props.skill?.default != null && same(fields, props.skill.default))
const changed = computed(() => name.value !== initialName || !same(fields, initial))
const length = computed(() => charLength(fields.body))
const overCap = computed(() => props.maxChars !== null && length.value > props.maxChars)
const saveDisabled = computed(
  () =>
    !changed.value ||
    !NAME_RULE.test(name.value) ||
    !fields.description.trim() ||
    fields.description.length > DESCRIPTION_MAX ||
    fields.argument_hint.length > HINT_MAX ||
    !fields.body.trim() ||
    overCap.value,
)

const heading = computed(() =>
  props.skill ? t('agents.skillDialogTitle', { name: props.skill.name }) : t('agents.skillDialogNew'),
)

/** `$ARGUMENTS` at the caret, replacing a selection: where the command's arguments go. */
async function insertArguments() {
  const token = '$ARGUMENTS'
  const el = body.value
  if (!el) {
    fields.body += token
    return
  }
  const { text, caret } = insertAtCaret(fields.body, el.selectionStart, el.selectionEnd, token)
  fields.body = text
  await nextTick()
  el.focus()
  el.setSelectionRange(caret, caret)
}

function restoreDefault() {
  if (props.skill?.default) {
    Object.assign(fields, props.skill.default)
  }
}

async function save() {
  if (pending.value || saveDisabled.value) {
    return
  }
  pending.value = true
  error.value = null
  const written: SkillFields = {
    description: fields.description.trim(),
    argument_hint: fields.argument_hint.trim(),
    body: fields.body.trim(),
    user_invocable: fields.user_invocable,
    model_invocable: fields.model_invocable,
  }
  try {
    const skill = props.skill
    const saved = skill
      ? // A built-in written back to its defaults loses the reader's edit on the server.
        await updateSkill(skill.name, {
          ...written,
          ...(!skill.builtin && name.value !== skill.name ? { name: name.value } : {}),
        })
      : await createSkill({ name: name.value, ...written })
    emit('saved', saved, skill?.name ?? null)
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}

async function remove() {
  const skill = props.skill
  if (!skill || pending.value) {
    return
  }
  pending.value = true
  error.value = null
  try {
    await deleteSkill(skill.name)
    emit('deleted', skill.name)
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
    :item="skill"
    :form-id="`skill-form-${skill?.name ?? 'new'}`"
    :is-default="isDefault"
    :delete-label="t('agents.skillDelete')"
    :delete-confirm="skill ? t('agents.skillDeleteConfirm', { name: skill.name }) : ''"
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
      <UiField v-slot="{ id }" :label="t('agents.skillArgumentHint')">
        <UiTextInput
          :id="id"
          v-model="fields.argument_hint"
          mono
          :maxlength="HINT_MAX"
          :placeholder="t('agents.skillArgumentHintPlaceholder')"
          :disabled="pending"
        />
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

    <fieldset class="switches">
      <legend class="legend">{{ t('agents.skillInvocation') }}</legend>
      <div class="switch-row">
        <label class="switch">
          <input v-model="fields.user_invocable" type="checkbox" :disabled="pending" >
          <span>{{ t('agents.skillSlashSwitch', { name: name || '…' }) }}</span>
        </label>
        <label class="switch">
          <input v-model="fields.model_invocable" type="checkbox" :disabled="pending" >
          <span>{{ t('agents.skillAutoSwitch') }}</span>
        </label>
      </div>
    </fieldset>

    <div class="body-field">
      <div class="body-head">
        <label :for="`skill-body-${skill?.name ?? 'new'}`" class="legend">{{ t('agents.skillBody') }}</label>
        <button
          type="button"
          class="token mono"
          :aria-label="t('agents.skillInsertArguments')"
          :title="t('agents.skillInsertArguments')"
          :disabled="pending"
          @click="insertArguments"
        >
          $ARGUMENTS
        </button>
      </div>
      <textarea
        :id="`skill-body-${skill?.name ?? 'new'}`"
        ref="body"
        v-model="fields.body"
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
.switches {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.switches .legend {
  margin-bottom: var(--space-2);
}

.switch-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-6);
}

.switch {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--control-height-sm);
  color: var(--ink-soft);
  font-size: var(--text-sm);
  cursor: pointer;
}

.switch input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--accent);
}

.body-field {
  display: grid;
  gap: var(--space-2);
}

.body-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
</style>
