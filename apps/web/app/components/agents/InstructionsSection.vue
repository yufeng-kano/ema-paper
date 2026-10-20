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
 * saved change applies from the next turn.
 */
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

const { t, te, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const labelId = useId()
const descriptionId = useId()
const countId = useId()
const drawerId = useId()
const advancedId = useId()

/** `limits.instructions_max_chars`; null until the session has loaded, or from an API that
 *  predates it — the count then shows without a cap. */
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
/** Bumped to remount the editor on Cancel: it follows its `source` only when that changes,
 *  and Cancel is often a return to the same saved text. */
const editorKey = ref(0)
const editor = ref<{ focus: (where?: 'start' | 'end') => void } | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)

/** Counted as the server counts: the trimmed text, in code points rather than UTF-16 units. */
const length = computed(() => [...text.value.trim()].length)
const overCap = computed(() => maxChars.value !== null && length.value > maxChars.value)
const dirty = computed(
  () =>
    text.value.trim() !== saved.value.instructions.trim() ||
    answerLanguage.value !== saved.value.answer ||
    translateLanguage.value !== saved.value.translate,
)

/** A language's name from the catalog; a tag saved through the API that the list lacks is
 *  shown as the tag itself. */
function languageName(tag: string): string {
  return te(`agents.languages.${tag}`) ? t(`agents.languages.${tag}`) : tag
}

/** A click on the box below the last line still writes, as in the notes column. */
function onEditorClick(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.rich')) {
    editor.value?.focus('end')
  }
}

async function save() {
  if (pending.value || !dirty.value || overCap.value) {
    return
  }
  const name = props.agent.name
  // Nothing changes these while the save is in flight: the editor is read-only and the
  // dropdowns are disabled until it answers.
  pending.value = true
  error.value = null
  try {
    const result = await putAgentSettings(name, {
      instructions: text.value.trim() || null,
      answer_language: answerLanguage.value || null,
      translate_language: translateLanguage.value || null,
    })
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
  editorKey.value += 1
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

// --- advanced: the whole prompt -------------------------------------------------------

/** Closed unless the prompt is not the default: a reader who replaced it should see that
 *  they did, and where to undo it. */
const advancedOpen = ref(props.agent.settings.prompt !== null || props.agent.settings.prompt_preset_id !== null)
</script>

<template>
  <section class="section" :aria-labelledby="labelId">
    <div class="intro">
      <h3 :id="labelId" class="section-label">{{ t('agents.instructions.label') }}</h3>
      <p :id="descriptionId" class="note">{{ t('agents.instructions.description') }}</p>
      <div class="editor md-side" @click="onEditorClick">
        <!-- Read-only while Save is in flight: a keystroke then would land in a draft the
             answer is about to reload under it. -->
        <RichEditor
          :key="editorKey"
          ref="editor"
          variant="notes"
          :source="saved.instructions"
          :placeholder="t('agents.instructions.placeholder')"
          :label="t('agents.instructions.label')"
          :described-by="`${descriptionId} ${countId}`"
          :editable="!pending"
          @change="text = $event"
        />
      </div>
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
        <span :id="countId" class="count tabular" :class="{ over: overCap }">
          {{
            maxChars === null
              ? length.toLocaleString(locale)
              : t('agents.instructions.count', {
                count: length.toLocaleString(locale),
                max: maxChars.toLocaleString(locale),
              })
          }}
        </span>
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
      <p class="note commit-note">{{ t('agents.instructions.appliesNext') }}</p>
      <div class="commit-buttons">
        <UiButton variant="secondary" :disabled="!dirty || pending" @click="cancel">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton variant="primary" :loading="pending" :disabled="!dirty || overCap" @click="save">
          {{ t('common.save') }}
        </UiButton>
      </div>
    </div>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>

    <div class="advanced">
      <button
        type="button"
        class="disclosure"
        :aria-expanded="advancedOpen"
        :aria-controls="advancedId"
        @click="advancedOpen = !advancedOpen"
      >
        <UiIcon :name="advancedOpen ? 'chevron-down' : 'chevron-right'" class="disclosure-glyph" />
        {{ t('agents.instructions.advanced') }}
      </button>
      <div v-show="advancedOpen" :id="advancedId" class="advanced-body">
        <p class="advanced-note">{{ t('agents.instructions.advancedNote') }}</p>
        <AgentsPromptField
          :agent="agent"
          :title="title"
          :max-chars="promptMaxChars"
          @saved="emit('saved', agent.name, $event)"
        />
      </div>
    </div>

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

/* The field label's treatment: a section of the panel, not a heading of the page. */
.section-label {
  margin: 0;
  font-family: inherit;
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
}

/* A consequence the controls cannot show, in the cache line's voice. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* The notes' editor in a field's hairline box: the column's sizes, the headings as plain
   run-in labels — a rule above a heading would only cut the box in two. The box is as tall
   as what it holds, with room for a few lines when it is empty. */
.editor {
  --block-heading: var(--display-2xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);
  --rich-gap: var(--space-3);

  min-height: 7rem;
  margin-top: var(--space-1);
  padding: var(--space-4);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  cursor: text;
}

/* The editable surface draws no ring of its own, so the box carries the app's one. */
.editor:focus-within {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.editor :deep(.md-h2:not([data-level='1'])) {
  padding-top: 0;
  border-top: none;
}

.editor :deep(.md-p),
.editor :deep(.md-list) {
  line-height: 1.7;
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

.count {
  color: var(--faint);
  font-size: var(--text-xs);
}

/* Over the cap is the one state the count exists to show. */
.count.over {
  color: var(--danger);
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

.advanced {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* A disclosure is a quiet row, not a button box: it opens a part of this section. */
.disclosure {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  height: var(--control-height-sm);
  margin-left: calc(-1 * var(--space-2));
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.disclosure:hover {
  background: var(--rail);
  color: var(--ink);
}

.disclosure-glyph {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

.advanced-body {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3) var(--space-4);
}

/* What replacing the prompt gives up: bounded like a banner, neutral like one. */
.advanced-note {
  flex: 1 1 20rem;
  min-width: 0;
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--hair);
  background: var(--paper);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}
</style>
