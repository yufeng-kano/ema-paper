<script setup lang="ts">
/**
 * One skill, opened from the Agent page's Skills table (docs/web.md § Pages, docs/chat.md
 * § Skills): its name, argument hint, when to use it, the two switches and its body. A
 * built-in keeps its name and offers **Restore default**, which refills the fields for Save
 * to write; one of the reader's own offers **Delete skill** behind a confirmation. Opened
 * with no skill, it is a new one of the reader's own.
 */
import { createSkill, deleteSkill, updateSkill, type Skill, type SkillFields } from '~/lib/api'

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

const NAME_RULE = /^[a-z0-9][a-z0-9-]{0,63}$/
const DESCRIPTION_MAX = 1024
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
const confirming = ref(false)

const builtin = computed(() => props.skill?.builtin === true)
const same = (left: SkillFields, right: SkillFields) =>
  (Object.keys(left) as Array<keyof SkillFields>).every((key) => left[key] === right[key])

const nameInvalid = computed(() => name.value !== '' && !NAME_RULE.test(name.value))
const isDefault = computed(() => props.skill?.default != null && same(fields, props.skill.default))
const changed = computed(() => name.value !== initialName || !same(fields, initial))
const length = computed(() => [...fields.body.trim()].length)
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
const state = computed(() => {
  if (!props.skill) {
    return ''
  }
  const kind = props.skill.builtin ? t('agents.builtIn') : t('agents.custom')
  return props.skill.modified ? `${kind} ${t('agents.editedSuffix')}` : kind
})

/** `$ARGUMENTS` at the caret, replacing a selection: where the command's arguments go. */
async function insertArguments() {
  const token = '$ARGUMENTS'
  const el = body.value
  if (!el) {
    fields.body += token
    return
  }
  const start = el.selectionStart
  const end = el.selectionEnd
  fields.body = fields.body.slice(0, start) + token + fields.body.slice(end)
  await nextTick()
  el.focus()
  el.setSelectionRange(start + token.length, start + token.length)
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
  <!-- The confirmation stands in for the dialog rather than over it: two dialogs would
       both answer Escape and both trap Tab. The fields keep their state behind it. -->
  <UiConfirmDialog
    v-if="confirming && skill"
    :title="t('agents.skillDelete')"
    :message="t('agents.skillDeleteConfirm', { name: skill.name })"
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

    <form :id="`skill-form-${skill?.name ?? 'new'}`" class="form" @submit.prevent="save">
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
        <p v-if="maxChars !== null" class="count mono tabular" :class="{ over: overCap }">
          {{ t('agents.promptCount', { count: length.toLocaleString(), max: maxChars.toLocaleString() }) }}
        </p>
      </div>

      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>

    <template #footer>
      <UiButton
        v-if="skill?.builtin"
        variant="ghost"
        class="foot-start"
        :disabled="pending || isDefault"
        @click="restoreDefault"
      >
        <template #icon><UiIcon name="rotate-ccw" /></template>
        {{ t('agents.promptRestore') }}
      </UiButton>
      <UiButton
        v-else-if="skill"
        variant="ghost"
        class="foot-start danger"
        :disabled="pending"
        @click="confirming = true; error = null"
      >
        <template #icon><UiIcon name="trash" /></template>
        {{ t('agents.skillDelete') }}
      </UiButton>
      <UiButton variant="secondary" :disabled="pending" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton
        variant="primary"
        type="submit"
        :form="`skill-form-${skill?.name ?? 'new'}`"
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
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
}

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

.token {
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.token:hover {
  background: var(--rail);
}

.token:disabled {
  cursor: not-allowed;
  opacity: 0.5;
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
