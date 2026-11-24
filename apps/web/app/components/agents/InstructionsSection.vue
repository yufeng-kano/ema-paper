<script setup lang="ts">
/**
 * The chat agent's **Instructions** (docs/web.md § Pages, docs/chat.md § Instructions): the
 * reader's own markdown, appended after the prompt in every conversation, edited in place
 * with the notes' editor rather than as a textarea of source; the two language choices
 * beside it; one Save for the three. Under them, the advanced option of replacing the whole
 * prompt, which says what it gives up, and the drawer that shows what the agent is really
 * sent.
 *
 * Nothing saves until Save: an edit is a draft, Cancel puts the saved values back, and a
 * saved change applies from the next turn. Save writes the instructions only when their text
 * changed, so instructions stored past the cap never stand in the way of a language change.
 */
import { charLength, instructionsUpdate } from '~/lib/agents'
import { putAgentSettings, type AgentEntry, type SavedAgentSettings } from '~/lib/api'
import { languageOptions } from '~/lib/languages'

const props = defineProps<{
  agent: AgentEntry
  /** The agent's name as the page shows it, for the prompt dialog's title. */
  title: string
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  promptMaxChars: number | null
}>()

const emit = defineEmits<{
  /** Named for the agent the request was made for, like every control on the panel. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t, te } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const labelId = useId()
const descriptionId = useId()
const countId = useId()
const drawerId = useId()

/** `limits.instructions_max_chars`; null until the session has loaded — the count then
 *  shows without a cap. */
const maxChars = computed(() => auth.me.value?.limits.instructions_max_chars ?? null)

/** What is stored, in the form the controls hold it: `''` for none. */
const saved = computed(() => ({
  instructions: props.agent.settings.instructions ?? '',
  answer: props.agent.settings.answer_language ?? '',
  translate: props.agent.settings.translate_language ?? '',
}))

const text = ref(saved.value.instructions)
const answerLanguage = ref(saved.value.answer)
const translateLanguage = ref(saved.value.translate)
const editor = ref<{ reset: () => void } | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)

const length = computed(() => charLength(text.value))
const dirty = computed(
  () =>
    text.value.trim() !== saved.value.instructions.trim() ||
    answerLanguage.value !== saved.value.answer ||
    translateLanguage.value !== saved.value.translate,
)
/** What Save writes; null while there is nothing it may send. */
const update = computed(() =>
  instructionsUpdate(
    { instructions: text.value, answer: answerLanguage.value, translate: translateLanguage.value },
    saved.value,
    maxChars.value,
  ),
)

/** A language's name from the catalog; a tag saved through the API that the list lacks is
 *  shown as the tag itself. */
function languageName(tag: string): string {
  return te(`agents.languages.${tag}`) ? t(`agents.languages.${tag}`) : tag
}

async function save() {
  const body = update.value
  if (pending.value || !body) {
    return
  }
  const name = props.agent.name
  // Nothing changes these while the save is in flight: the editor is read-only and the
  // dropdowns are disabled until it answers.
  pending.value = true
  error.value = null
  try {
    const result = await putAgentSettings(name, body)
    emit('saved', name, result)
    // The stored values, the text as the server cleaned it, become the draft.
    text.value = result.instructions ?? ''
    answerLanguage.value = result.answer_language ?? ''
    translateLanguage.value = result.translate_language ?? ''
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}

function cancel() {
  text.value = saved.value.instructions
  answerLanguage.value = saved.value.answer
  translateLanguage.value = saved.value.translate
  error.value = null
  editor.value?.reset()
}

// --- what the agent receives --------------------------------------------------------

const drawerOpen = ref(false)
const receivesButton = ref<HTMLButtonElement | null>(null)

/** Focus goes back to the button that opened the drawer. */
async function closeDrawer() {
  drawerOpen.value = false
  await nextTick()
  receivesButton.value?.focus()
}
</script>

<template>
  <section class="section" :aria-labelledby="labelId">
    <div class="intro">
      <h3 :id="labelId" class="agents-label">{{ t('agents.instructions.label') }}</h3>
      <p :id="descriptionId" class="agents-note">{{ t('agents.instructions.description') }}</p>
      <AgentsInstructionsEditor
        ref="editor"
        class="box"
        :source="saved.instructions"
        :placeholder="t('agents.instructions.placeholder')"
        :label="t('agents.instructions.label')"
        :described-by="`${descriptionId} ${countId}`"
        :editable="!pending"
        @change="text = $event"
      />
      <div class="under">
        <button
          ref="receivesButton"
          type="button"
          class="text-button"
          :aria-expanded="drawerOpen"
          :aria-controls="drawerId"
          @click="drawerOpen ? closeDrawer() : (drawerOpen = true)"
        >
          {{ t('agents.instructions.receives') }}
        </button>
        <AgentsCharCount :id="countId" :count="length" :max="maxChars" />
      </div>
    </div>

    <div class="languages">
      <UiField v-slot="{ id }" class="language" :label="t('agents.instructions.answerLanguage')">
        <UiSelect :id="id" v-model="answerLanguage" :disabled="pending">
          <option value="">{{ t('agents.instructions.followInterface') }}</option>
          <option v-for="tag in languageOptions(saved.answer)" :key="tag" :value="tag">{{ languageName(tag) }}</option>
        </UiSelect>
      </UiField>
      <UiField v-slot="{ id }" class="language" :label="t('agents.instructions.translateLanguage')">
        <UiSelect :id="id" v-model="translateLanguage" :disabled="pending">
          <option value="">{{ t('agents.instructions.followInterface') }}</option>
          <option v-for="tag in languageOptions(saved.translate)" :key="tag" :value="tag">{{ languageName(tag) }}</option>
        </UiSelect>
      </UiField>
    </div>

    <div class="commit">
      <p class="agents-note commit-note">{{ t('agents.instructions.appliesNext') }}</p>
      <div class="commit-buttons">
        <UiButton variant="secondary" :disabled="!dirty || pending" @click="cancel">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton variant="primary" :loading="pending" :disabled="!update" @click="save">
          {{ t('common.save') }}
        </UiButton>
      </div>
    </div>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>

    <AgentsAdvancedPrompt
      :agent="agent"
      :title="title"
      :max-chars="promptMaxChars"
      :note="t('agents.instructions.advancedNote')"
      @saved="emit('saved', agent.name, $event)"
    />

    <AgentsReceivesDrawer v-if="drawerOpen" :id="drawerId" @close="closeDrawer" />
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.intro {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.box {
  margin-top: var(--space-1);
}

.under {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
}

/* A text button: the muted word with a quiet underline, ink on hover. */
.text-button {
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
  text-decoration: underline;
  text-decoration-color: var(--edge);
  text-underline-offset: 3px;
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease);
}

.text-button:hover {
  color: var(--ink);
  text-decoration-color: currentcolor;
}

.languages {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-2);
}

.language {
  flex: 0 1 14rem;
  min-width: 10rem;
}

.commit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-4);
}

.commit-note {
  flex: 1 1 15rem;
}

.commit-buttons {
  display: flex;
  gap: var(--space-2);
}
</style>
