<script setup lang="ts">
/**
 * The chat agent's memory file on the Agent page (docs/web.md § Pages; docs/chat.md
 * § Memory, *The memory file*): `memory.md`, the notes the agent keeps across
 * conversations, in a monospace textarea with a live count against the cap, Cancel and
 * Save as the instructions have them. A save reaches new conversations and compacted ones;
 * one refused while a turn of the reader runs says so in a line under the box.
 */
import { getMemoryFile, putMemoryFile } from '~/lib/api'

const { t } = useI18n()
const { resolve } = useApiError()
const { changed } = useAgentContextVersion()
const labelId = useId()
const fieldId = useId()

const saved = ref('')
const draft = ref('')
const maxChars = ref<number | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)

/** Counted in code points, as the server counts. */
const count = computed(() => [...draft.value].length)
const overCap = computed(() => maxChars.value !== null && count.value > maxChars.value)
const dirty = computed(() => draft.value !== saved.value)

async function load() {
  loading.value = true
  loadError.value = null
  try {
    const file = await getMemoryFile()
    saved.value = file.text
    draft.value = file.text
    maxChars.value = file.max_chars
  } catch (caught) {
    loadError.value = await resolve(caught)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function cancel() {
  draft.value = saved.value
  error.value = null
}

async function save() {
  if (!dirty.value || overCap.value || pending.value) {
    return
  }
  pending.value = true
  error.value = null
  try {
    const { text } = await putMemoryFile(draft.value)
    saved.value = text
    draft.value = text
    // *What the agent receives* reads the file again while it is open.
    changed()
  } catch (caught) {
    error.value = await resolve(caught)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <section class="section" :aria-labelledby="labelId">
    <h3 :id="labelId" class="section-label">{{ t('agents.memoryFile.label') }}</h3>
    <p class="note">{{ t('agents.memoryFile.description') }}</p>
    <UiBanner v-if="loadError" tone="error">{{ loadError }}</UiBanner>
    <UiSkeleton v-else-if="loading" :rows="3" />
    <template v-else>
      <label :for="fieldId" class="sr-only">{{ t('agents.memoryFile.label') }}</label>
      <textarea
        :id="fieldId"
        v-model="draft"
        class="editor mono"
        rows="8"
        spellcheck="false"
        :placeholder="t('agents.memoryFile.placeholder')"
        :disabled="pending"
      />
      <div class="under">
        <span class="count" :class="{ over: overCap }">
          {{ maxChars !== null ? t('agents.memoryFile.count', { count, max: maxChars }) : count }}
        </span>
        <div class="buttons">
          <UiButton variant="secondary" :disabled="!dirty || pending" @click="cancel">
            {{ t('common.cancel') }}
          </UiButton>
          <UiButton variant="primary" :loading="pending" :disabled="!dirty || overCap" @click="save">
            {{ t('common.save') }}
          </UiButton>
        </div>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </template>
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
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

.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* The file as it is: a plain box of its text, in the mono face, as tall as a few sections. */
.editor {
  width: 100%;
  min-height: 10rem;
  margin-top: var(--space-1);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-xs);
  line-height: 1.6;
  resize: vertical;
}

.editor:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.editor:disabled {
  color: var(--muted);
}

.under {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
}

.count {
  color: var(--faint);
  font-size: var(--text-xs);
}

/* Over the cap is the one state the count exists to show. */
.count.over {
  color: var(--danger);
}

.buttons {
  display: flex;
  gap: var(--space-2);
}

.error {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-xs);
}
</style>
