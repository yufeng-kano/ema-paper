<script setup lang="ts">
/**
 * Convert to document… (docs/web.md § Files): a file kept as it is — a PDF or an image —
 * becomes a document the reader can read, mark and talk to. It stays where it is under its
 * name; the parse agent's model converts it, and each page costs the plan's credits, as an
 * upload converted on arrival does (docs/jobs.md § Converting a file). A balance that cannot
 * cover it (402) closes the dialog into the credits dialog, and the file stays a file.
 */
import { convertDocument, creditsRefusalOf, type CreditsRefusal, type LibraryDocument } from '~/lib/api'
import { formatBytes } from '~/lib/format'

const props = defineProps<{ document: LibraryDocument }>()

const emit = defineEmits<{ close: []; converted: [id: number]; refused: [refusal: CreditsRefusal] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const kindLabel = useKindLabel()

const facts = computed(() => {
  const kind = kindLabel(props.document)
  const size = props.document.size_bytes
  return size === null ? kind : `${kind} · ${formatBytes(size, locale.value)}`
})
const error = ref<string | null>(null)
const busy = ref(false)

async function start() {
  busy.value = true
  error.value = null
  try {
    const converted = await convertDocument(props.document.id, null)
    emit('converted', converted.id)
  } catch (failure) {
    const refusal = creditsRefusalOf(failure)
    if (refusal) {
      emit('refused', refusal)
      return
    }
    error.value = await resolve(failure)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <UiModal :title="t('files.convertDialog.title')" size="sm" :busy="busy" @close="emit('close')">
    <template #title>
      <h2 class="title">{{ t('files.convertDialog.title') }}</h2>
      <p class="subject">{{ document.name }}</p>
      <p class="subject">{{ facts }}</p>
    </template>

    <div class="body">
      <p class="note">{{ t('files.convertDialog.note') }}</p>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </div>

    <template #footer>
      <UiButton variant="ghost" :disabled="busy" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton variant="primary" :loading="busy" @click="start">
        {{ t('files.convertDialog.submit') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.title {
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* The file, under the verb — its name, then its kind and size, as in the re-convert dialog. */
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

.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.5;
}
</style>
