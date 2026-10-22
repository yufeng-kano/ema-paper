<script setup lang="ts">
/**
 * Re-convert… (docs/web.md § Files): the model a new parse of the document's original runs
 * on — the live catalog, the parse agent's own model selected — and what happens to the
 * version open now. It stays until the new parse succeeds and is then replaced; the edits
 * made meanwhile are replaced too and stay in its revisions, and the notes stay
 * (docs/jobs.md § Re-converting a document).
 */
import {
  listAgentModels,
  listAgents,
  reconvertDocument,
  type ConnectionModel,
  type LibraryDocument,
} from '~/lib/api'

const PARSE_AGENT = 'parse'

const props = defineProps<{ document: LibraryDocument }>()

const emit = defineEmits<{ close: []; started: [] }>()

const { t } = useI18n()
const { resolve } = useApiError()

const models = ref<ConnectionModel[] | null>(null)
const modelsError = ref<string | null>(null)
/** The parse agent's own model, the first choice. */
const own = ref<string | null>(null)
/** The reader's pick; until there is one, the default below. */
const chosen = ref<string | null>(null)
const startError = ref<string | null>(null)
const busy = ref(false)

/** The agent's own model, else the one the document was parsed on. */
const model = computed(() => chosen.value ?? (own.value || props.document.model || ''))
/** A model the catalog does not list (delisted, or not loaded) is still the choice shown. */
const modelInjected = computed(() =>
  model.value && !(models.value ?? []).some((entry) => entry.id === model.value) ? model.value : null,
)

/** The picker's catalog and the agent's model: without them the server's default still runs. */
async function loadModels() {
  try {
    const [agents, catalog] = await Promise.all([listAgents(), listAgentModels(PARSE_AGENT)])
    own.value = agents.agents.find((agent) => agent.name === PARSE_AGENT)?.settings.model ?? null
    models.value = [...catalog.data].sort((left, right) => left.id.localeCompare(right.id))
  } catch (error) {
    modelsError.value = await resolve(error)
  }
}

onMounted(() => {
  void loadModels()
})

async function start() {
  busy.value = true
  startError.value = null
  try {
    await reconvertDocument(props.document.id, model.value || null)
    emit('started')
  } catch (error) {
    startError.value = await resolve(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <UiModal :title="t('files.reconvertDialog.title')" size="sm" :busy="busy" @close="emit('close')">
    <template #title>
      <h2 class="title">{{ t('files.reconvertDialog.title') }}</h2>
      <p class="subject">{{ document.name }}</p>
    </template>

    <div class="body">
      <UiField v-slot="{ id }" :label="t('files.reconvertDialog.model')">
        <UiSelect :id="id" :model-value="model" :disabled="busy" @update:model-value="chosen = $event">
          <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
          <option v-for="entry in models ?? []" :key="entry.id" :value="entry.id">
            {{ entry.name ? `${entry.name} · ${entry.id}` : entry.id }}
          </option>
        </UiSelect>
      </UiField>
      <p class="note">{{ t('files.reconvertDialog.note') }}</p>
      <UiBanner v-if="modelsError" tone="error">{{ modelsError }}</UiBanner>
      <UiBanner v-if="startError" tone="error">{{ startError }}</UiBanner>
    </div>

    <template #footer>
      <UiButton variant="ghost" :disabled="busy" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton variant="primary" :loading="busy" @click="start">
        {{ t('files.reconvertDialog.submit') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.title {
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* The document, under the verb — a fact, as in the retry dialog. */
.subject {
  margin-top: var(--space-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--faint);
  font-size: var(--text-xs);
}

.body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* What happens to the version open now, under the model. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
