<script setup lang="ts">
/**
 * One agent's prompt (docs/web.md § Pages, docs/agents.md § Per-agent settings): the
 * prompt as a monospace textarea that opens on the saved one, or on the default when there
 * is none, and takes edits in place. The template's tokens are chips that insert at the
 * cursor; **Restore default** puts the default back. A text equal to the default is saved
 * as no prompt, so a later default reaches it.
 */
import { charLength } from '~/lib/agents'
import { putAgentSettings, type AgentEntry, type SavedAgentSettings } from '~/lib/api'
import { insertAtCaret } from '~/lib/caret'

const props = defineProps<{
  agent: AgentEntry
  /** The agent's name as the page shows it. */
  title: string
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  maxChars: number | null
}>()

const emit = defineEmits<{ close: []; saved: [settings: SavedAgentSettings] }>()

const { t } = useI18n()
const { resolve } = useApiError()

const initial = props.agent.settings.prompt ?? props.agent.default_prompt
const text = ref(initial)
const textarea = ref<HTMLTextAreaElement | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)

const isDefault = computed(() => text.value.trim() === props.agent.default_prompt.trim())
const length = computed(() => charLength(text.value))
const overCap = computed(() => props.maxChars !== null && length.value > props.maxChars)
const saveDisabled = computed(() => text.value === initial || !text.value.trim() || overCap.value)

/** A chip inserts its token at the caret, replacing a selection, and hands focus back. */
async function insertToken(token: string) {
  const el = textarea.value
  if (!el) {
    text.value += token
    return
  }
  const inserted = insertAtCaret(text.value, el.selectionStart, el.selectionEnd, token)
  text.value = inserted.text
  await nextTick()
  el.focus()
  el.setSelectionRange(inserted.caret, inserted.caret)
}

async function save() {
  if (pending.value || saveDisabled.value) {
    return
  }
  pending.value = true
  error.value = null
  try {
    emit('saved', await putAgentSettings(props.agent.name, { prompt: isDefault.value ? null : text.value.trim() }))
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <AgentsDefinitionDialog
    :heading="t('agents.promptDialogTitle', { agent: title })"
    :item="{ builtin: true, modified: !isDefault }"
    :state="isDefault ? t('agents.promptDefault') : t('agents.promptEdited')"
    :form-id="`prompt-form-${agent.name}`"
    :is-default="isDefault"
    :pending="pending"
    :error="error"
    :save-disabled="saveDisabled"
    @close="emit('close')"
    @save="save"
    @restore="text = agent.default_prompt"
    @clear-error="error = null"
  >
    <div class="prompt-field">
      <div v-if="agent.prompt_tokens.length" class="tokens">
        <button
          v-for="token in agent.prompt_tokens"
          :key="token"
          type="button"
          class="token mono"
          :aria-label="t('agents.promptInsertToken', { token })"
          :title="t('agents.promptInsertToken', { token })"
          :disabled="pending"
          @click="insertToken(token)"
        >
          {{ token }}
        </button>
      </div>
      <label :for="`prompt-text-${agent.name}`" class="sr-only">{{ t('agents.promptTextLabel') }}</label>
      <textarea
        :id="`prompt-text-${agent.name}`"
        ref="textarea"
        v-model="text"
        class="area mono-area prompt-text"
        rows="18"
        spellcheck="false"
        :disabled="pending"
      />
      <AgentsCharCount v-if="maxChars !== null" class="end-count mono" :count="length" :max="maxChars" />
    </div>
  </AgentsDefinitionDialog>
</template>

<style scoped>
/* The tokens, the prompt and its count sit closer together than the shell's fields do. */
.prompt-field {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tokens {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.prompt-text {
  min-height: 16rem;
}
</style>
