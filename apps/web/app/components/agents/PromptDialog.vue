<script setup lang="ts">
/**
 * One agent's prompt (docs/web.md § Pages, docs/agents.md § Per-agent settings): the
 * prompt as a monospace textarea that opens on the saved one, or on the default when there
 * is none, and takes edits in place. The template's tokens are chips that insert at the
 * cursor; **Restore default** puts the default back. A text equal to the default is saved
 * as no prompt, so a later default reaches it.
 */
import { putAgentSettings, type AgentEntry, type SavedAgentSettings } from '~/lib/api'

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
/** Counted as the server counts: the trimmed text, in code points rather than UTF-16 units. */
const length = computed(() => [...text.value.trim()].length)
const overCap = computed(() => props.maxChars !== null && length.value > props.maxChars)
const saveDisabled = computed(() => text.value === initial || !text.value.trim() || overCap.value)

/** A chip inserts its token at the caret, replacing a selection, and hands focus back. */
async function insertToken(token: string) {
  const el = textarea.value
  if (!el) {
    text.value += token
    return
  }
  const start = el.selectionStart
  const end = el.selectionEnd
  text.value = text.value.slice(0, start) + token + text.value.slice(end)
  await nextTick()
  el.focus()
  el.setSelectionRange(start + token.length, start + token.length)
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
  <UiModal
    size="wide"
    :title="t('agents.promptDialogTitle', { agent: title })"
    :busy="pending"
    @close="emit('close')"
  >
    <template #title>
      <div class="heading">
        <h2 class="heading-title">{{ t('agents.promptDialogTitle', { agent: title }) }}</h2>
        <span class="heading-state" role="status">
          {{ isDefault ? t('agents.promptDefault') : t('agents.promptEdited') }}
        </span>
      </div>
    </template>

    <form :id="`prompt-form-${agent.name}`" class="form" @submit.prevent="save">
      <div v-if="agent.prompt_tokens?.length" class="tokens">
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
        class="text"
        rows="18"
        spellcheck="false"
        :disabled="pending"
      />
      <p v-if="maxChars !== null" class="count mono tabular" :class="{ over: overCap }">
        {{ t('agents.promptCount', { count: length.toLocaleString(), max: maxChars.toLocaleString() }) }}
      </p>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>

    <template #footer>
      <UiButton variant="ghost" class="foot-start" :disabled="pending || isDefault" @click="text = agent.default_prompt">
        <template #icon><UiIcon name="rotate-ccw" /></template>
        {{ t('agents.promptRestore') }}
      </UiButton>
      <UiButton variant="secondary" :disabled="pending" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton
        variant="primary"
        type="submit"
        :form="`prompt-form-${agent.name}`"
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
  gap: 10px;
}

.tokens {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* A chip is a button that types for you: a hairline and the mono of the text it goes into. */
.token {
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.token:hover {
  background: var(--rail);
  color: var(--ink);
}

.token:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* Same skin as UiTextInput's `.control`, sized for a prompt instead of one line. */
.text {
  width: 100%;
  min-width: 0;
  min-height: 16rem;
  padding: var(--space-3) 14px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  resize: vertical;
  transition: border-color var(--duration-fast) var(--ease);
}

.text:disabled {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

@media (pointer: coarse) {
  .text {
    font-size: 16px;
  }
}

.count {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-2xs);
  text-align: right;
}

/* Over the cap is the one state the count exists to show. */
.count.over {
  color: var(--danger);
}

/* The footer's left action stands apart from Cancel and Save. */
.foot-start {
  margin-right: auto;
}
</style>
