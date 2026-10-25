<script setup lang="ts">
/**
 * What opening a failed parse shows (docs/web.md § Files): it has no result to read, so the
 * dialog lists the pages a retry would send — each as the original shows it, with why it
 * failed and on which model — the model to send them to, and the retry itself.
 *
 * Only the pages without a result go; the ones that succeeded are kept. The retry reserves
 * the parse's credits again, charged only if it succeeds, and a balance that cannot cover
 * them is the credits dialog's to say (docs/billing.md § A parse). A parse that failed only
 * its format check has every page: its retry sends none, so the dialog asks for no model
 * and is titled for the whole document. The page opens the document by itself once the
 * retry has every page and its check. A failed re-convert's retry — its document carries
 * it as `reconvert` — replaces the version the document has instead, which opens all along
 * (docs/jobs.md § Re-converting a document).
 */
import {
  creditsRefusalOf,
  getDocumentPages,
  listAgentModels,
  listAgents,
  retryDocument,
  type ConnectionModel,
  type CreditsRefusal,
  type DocumentPage,
  type LibraryDocument,
} from '~/lib/api'
import { PAGE_FAILURE_REASONS, RETRY_TILES_MAX, checkFailureReason } from '~/lib/library'

const PARSE_AGENT = 'parse'
const CHECK_AGENT = 'check'

const props = defineProps<{ document: LibraryDocument }>()

const emit = defineEmits<{ close: []; retried: []; refused: [refusal: CreditsRefusal] }>()

const { t } = useI18n()
const { resolve } = useApiError()

const pages = ref<DocumentPage[] | null>(null)
const pagesError = ref<string | null>(null)
const models = ref<ConnectionModel[] | null>(null)
const modelsError = ref<string | null>(null)
/** The parse agent's fallback, the first choice when there is one. */
const fallback = ref<string | null>(null)
/** The reader's pick; until there is one, the default below. */
const chosen = ref<string | null>(null)
/** Whether the format check is on now — a retry checks with the settings of its moment.
 *  Null until the agents have loaded. */
const checkOn = ref<boolean | null>(null)
const retryError = ref<string | null>(null)
const busy = ref(false)
/** Pages whose image could not be drawn — the original gone, a page Poppler refused. */
const broken = reactive(new Set<number>())

/** A failed re-convert's retry: its success replaces a version that opens already. */
const replacing = computed(() => props.document.reconvert?.status === 'failed')
/** Every page of the selection without a result: what a retry sends. */
const missing = computed(() => (pages.value ?? []).filter((page) => page.status !== 'succeeded'))
/** Known before the pages load, from the row: a parse with no page missing retries whole. */
const title = computed(() => t(props.document.failed_pages ? 'files.retry.title' : 'files.retry.titleWhole'))
const tiles = computed(() => missing.value.slice(0, RETRY_TILES_MAX))
/** The job's own error, when no page carries one: something ended it before any page did,
 *  or its format check failed after every page — said in the reader's language. */
const jobError = computed(() => {
  if (!pages.value || missing.value.some((page) => page.reason)) {
    return null
  }
  const check = checkFailureReason(props.document.error)
  if (check === null) {
    return props.document.error
  }
  const key = PAGE_FAILURE_REASONS[check]
  return t('files.retry.checkFailed', { reason: key ? t(`files.retry.reasons.${key}`) : check })
})

/** The fallback, else the model the failed pages last ran on. */
const model = computed(
  () =>
    chosen.value ??
    (fallback.value || missing.value.find((page) => page.model)?.model || props.document.model || ''),
)
/** A model the catalog does not list (delisted, or not loaded) is still the choice shown. */
const modelInjected = computed(() =>
  model.value && !(models.value ?? []).some((entry) => entry.id === model.value) ? model.value : null,
)

const submitLabel = computed(() => {
  const [only, ...rest] = missing.value
  if (only && !rest.length) {
    return t('files.retry.submitOne', { n: only.page })
  }
  return only ? t('files.retry.submitMany', { n: missing.value.length }) : t('files.retry.submit')
})

function reasonLabel(page: DocumentPage): string {
  if (page.status === 'pending') {
    return t('files.retry.notReached')
  }
  const key = page.reason ? PAGE_FAILURE_REASONS[page.reason] : undefined
  return key ? t(`files.retry.reasons.${key}`) : (page.reason ?? '')
}

async function loadPages() {
  try {
    pages.value = (await getDocumentPages(props.document.id)).pages
  } catch (error) {
    pagesError.value = await resolve(error)
  }
}

/** The picker's catalog and the fallback: without them the default model still retries. */
async function loadModels() {
  try {
    const [agents, catalog] = await Promise.all([listAgents(), listAgentModels(PARSE_AGENT)])
    fallback.value = agents.agents.find((agent) => agent.name === PARSE_AGENT)?.settings.fallback_model ?? null
    checkOn.value = agents.agents.find((agent) => agent.name === CHECK_AGENT)?.settings.auto_check ?? false
    models.value = [...catalog.data].sort((left, right) => left.id.localeCompare(right.id))
  } catch (error) {
    modelsError.value = await resolve(error)
  }
}

onMounted(() => {
  void loadPages()
  void loadModels()
})

async function retry() {
  busy.value = true
  retryError.value = null
  try {
    // No model for a retry that sends no page.
    await retryDocument(props.document.id, missing.value.length ? model.value || null : null)
    emit('retried')
  } catch (error) {
    const refusal = creditsRefusalOf(error)
    if (refusal) {
      emit('refused', refusal)
      return
    }
    retryError.value = await resolve(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <UiModal :title="title" size="md" :busy="busy" @close="emit('close')">
    <template #title>
      <h2 class="title">{{ title }}</h2>
      <p class="subject">{{ document.name }}</p>
    </template>

    <UiBanner v-if="pagesError" tone="error">{{ pagesError }}</UiBanner>
    <UiSkeleton v-else-if="pages === null" :rows="3" />
    <div v-else class="body">
      <p v-if="jobError" class="job-error">{{ jobError }}</p>
      <ul v-if="tiles.length" class="tiles" :aria-label="t('files.retry.pages')">
        <li v-for="page in tiles" :key="page.page" class="tile">
          <img
            v-if="!broken.has(page.page)"
            class="page-image"
            :src="`/api/library/documents/${document.id}/pages/${page.page}/image`"
            :alt="t('files.retry.pageImage', { n: page.page })"
            loading="lazy"
            decoding="async"
            @error="broken.add(page.page)"
          >
          <span v-else class="page-image missing"><UiIcon name="image" /></span>
          <p class="page-number tabular">{{ t('files.retry.page', { n: page.page }) }}</p>
          <p class="reason">{{ reasonLabel(page) }}</p>
          <p v-if="page.model" class="page-model mono" :title="page.model">{{ page.model }}</p>
        </li>
      </ul>
      <p v-if="missing.length > tiles.length" class="more">
        {{ t('files.retry.more', { n: missing.length - tiles.length }, missing.length - tiles.length) }}
      </p>

      <template v-if="missing.length">
        <UiField v-slot="{ id }" :label="t('files.retry.model')">
          <UiSelect :id="id" :model-value="model" :disabled="busy" @update:model-value="chosen = $event">
            <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
            <option v-for="entry in models ?? []" :key="entry.id" :value="entry.id">
              {{ entry.name ? `${entry.name} · ${entry.id}` : entry.id }}
            </option>
          </UiSelect>
        </UiField>
        <p class="note">{{ t(replacing ? 'files.retry.noteReconvert' : 'files.retry.note') }}</p>
        <UiBanner v-if="modelsError" tone="error">{{ modelsError }}</UiBanner>
      </template>
      <p v-else class="note">
        {{
          t(replacing ? 'files.retry.noteReconvertWhole' : checkOn ? 'files.retry.noteCheck' : 'files.retry.noteWhole')
        }}
      </p>
      <p class="note">{{ t('files.retry.charge') }}</p>
      <UiBanner v-if="retryError" tone="error">{{ retryError }}</UiBanner>
    </div>

    <template #footer>
      <UiButton variant="ghost" :disabled="busy" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton variant="primary" :loading="busy" :disabled="pages === null" @click="retry">
        {{ submitLabel }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.title {
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* The document, under the verb — a fact, as in the move dialog. */
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

.job-error {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-sm);
}

/* Bounded and scrolling, like the move dialog's tree: twelve pages must not push the
   buttons off the screen. */
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(7.5rem, 1fr));
  gap: var(--space-3);
  max-height: 360px;
  margin: 0;
  padding: var(--space-2);
  overflow-y: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--line-strong);
  background: var(--rail);
  list-style: none;
}

.tile {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

/* A page's proportions, whatever the render: letterboxed on paper. */
.page-image {
  display: block;
  width: 100%;
  aspect-ratio: 3 / 4;
  object-fit: contain;
  border: 1px solid var(--edge);
  background: var(--paper);
}

.page-image.missing {
  display: grid;
  place-items: center;
  color: var(--faint);
}

.page-image.missing svg {
  width: 22px;
  height: 22px;
}

.page-number {
  margin: 0;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.reason {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-2xs);
}

.page-model {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-size: var(--text-2xs);
}

.more {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* The one line under the model: what the retry sends and when the document opens. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
