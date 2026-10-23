<script setup lang="ts">
import { createKey, listKeys, revokeKey, type ApiKeySummary, type CreatedApiKey } from '~/lib/api'
import { formatDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const { t, locale } = useI18n()

const { data, pending, errorMessage, refresh } = useAuthedData(() => listKeys())
const { resolve } = useApiError()

const newKeyName = ref('')
const creating = ref(false)
const showCreate = ref(false)
const mutationError = ref<string | null>(null)
const createdKey = ref<CreatedApiKey | null>(null)
/** The key the confirm dialog is asking about — revoking is irreversible. */
const revokeTarget = ref<ApiKeySummary | null>(null)
const revoking = ref(false)
/** The panel the reveal is inserted into — it scrolls independently of the page. */
const keysPanel = ref<{ scrollToTop: () => void } | null>(null)

// The example's origin is this deployment's own; a placeholder until the client knows it.
const origin = ref('https://<host>')
onMounted(() => {
  origin.value = window.location.origin
})

const curlExample = computed(
  () =>
    `curl -X POST ${origin.value}/v1/parse \\\n` +
    `  -H "Authorization: Bearer sr_your_api_key" \\\n` +
    `  -F file=@document.pdf`,
)

const columns = computed<TableColumn<ApiKeySummary>[]>(() => [
  { key: 'name', header: t('keys.columnName') },
  { key: 'prefix', header: t('keys.columnPrefix') },
  { key: 'created', header: t('keys.columnCreated'), numeric: true },
  { key: 'lastUsed', header: t('keys.columnLastUsed'), numeric: true },
  { key: 'revoke', header: '', srHeader: t('keys.revoke'), align: 'end', width: '88px' },
])

function openCreate() {
  newKeyName.value = ''
  mutationError.value = null
  showCreate.value = true
}

async function submitCreate() {
  const name = newKeyName.value.trim()
  if (!name || creating.value) {
    return
  }
  creating.value = true
  mutationError.value = null
  try {
    createdKey.value = await createKey(name)
    newKeyName.value = ''
    showCreate.value = false
    await refresh()
    // The plaintext is shown exactly once and is unrecoverable, so it must not be left
    // above the fold: Create key sits beside the section title, which is reachable at any
    // scroll depth of the column, and the reveal is inserted at the panel's top.
    await nextTick()
    keysPanel.value?.scrollToTop()
  } catch (error) {
    mutationError.value = await resolve(error)
  } finally {
    creating.value = false
  }
}

async function confirmRevoke() {
  const target = revokeTarget.value
  if (!target || revoking.value) {
    return
  }
  revoking.value = true
  mutationError.value = null
  try {
    await revokeKey(target.id)
    if (createdKey.value?.id === target.id) {
      createdKey.value = null
    }
    revokeTarget.value = null
    await refresh()
  } catch (error) {
    mutationError.value = await resolve(error)
  } finally {
    revoking.value = false
  }
}
</script>

<template>
  <SettingsSection
    :eyebrow="data ? t('keys.activeCount', { count: data.keys.length }) : undefined"
    :title="t('keys.headTitle')"
  >
    <!-- The section's primary action, beside its title: the one thing this section is for,
         reachable at any scroll depth of the sheet's column. -->
    <template #actions>
      <UiButton variant="primary" @click="openCreate">
        <template #icon><UiIcon name="plus" /></template>
        {{ t('keys.create') }}
      </UiButton>
    </template>

    <div class="region">
      <UiPanel ref="keysPanel" :title="t('keys.listTitle')">
        <template #meta>
          <span v-if="data">{{ t('keys.keyCount', { count: data.keys.length }) }}</span>
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('common.refresh')"
            :loading="pending"
            @click="refresh"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </template>

        <!-- The plaintext key exists on exactly one screen, once. It is the loudest block on
             the page for as long as it is there, and it is dismissed by the user rather than by
             the next navigation quietly taking it away. -->
        <UiBanner v-if="createdKey" class="state" tone="warn">
          <div class="reveal">
            <p class="reveal-title">{{ t('keys.createdTitle') }}</p>
            <code class="reveal-key mono">{{ createdKey.key }}</code>
            <p>{{ t('keys.createdWarning') }}</p>
          </div>
          <template #actions>
            <UiCopyButton :text="createdKey.key" variant="secondary" size="sm" labelled />
            <UiButton
              variant="ghost"
              size="sm"
              icon-only
              :label="t('common.close')"
              @click="createdKey = null"
            >
              <template #icon><UiIcon name="close" /></template>
            </UiButton>
          </template>
        </UiBanner>

        <UiBanner v-if="mutationError" class="state" tone="error">{{ mutationError }}</UiBanner>
        <!-- A failed refresh keeps the rows it already has; the banner says what went wrong. -->
        <UiBanner v-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>

        <!-- Not just `!data`: a failed first load leaves data null with `pending` back to
             false, and an animated skeleton beside a terminal error says the page is still
             working when it has stopped. -->
        <UiSkeleton v-if="!data && !errorMessage" class="state" />

        <!-- No action here: Create key is beside the title, present in every state. -->
        <UiEmptyState
          v-else-if="data && !data.keys.length"
          :title="t('keys.empty')"
          :body="t('keys.emptyBody')"
        />

        <UiDataTable
          v-else-if="data"
          :columns="columns"
          :rows="data.keys"
          :row-key="(key) => String(key.id)"
          :caption="t('keys.listTitle')"
        >
          <template #cell-name="{ row }">
            <span class="cell-block">{{ row.name }}</span>
          </template>
          <template #cell-prefix="{ row }">
            <code class="prefix mono">{{ row.prefix }}</code>
          </template>
          <template #cell-created="{ row }">{{ formatDateTime(row.created_at, locale) }}</template>
          <template #cell-lastUsed="{ row }">
            <span v-if="row.last_used_at">{{ formatDateTime(row.last_used_at, locale) }}</span>
            <span v-else class="never">{{ t('common.never') }}</span>
          </template>
          <!-- Revoke keeps its word rather than becoming a glyph: it destroys a credential the
               user would have to re-issue and re-deploy, and a bare icon makes them hover to
               find out which one does that. -->
          <template #cell-revoke="{ row }">
            <UiButton
              variant="danger"
              size="xs"
              :label="t('keys.revokeKey', { name: row.name })"
              @click="revokeTarget = row"
            >
              {{ t('keys.revoke') }}
            </UiButton>
          </template>
        </UiDataTable>
      </UiPanel>

      <!-- The keys' one consumer worth showing here: the REST call they authorize. The
           example carries the header, so no sentence restates it. -->
      <UiPanel :title="t('keys.restTitle')" sunken divided>
        <template #meta>
          <UiCopyButton :text="curlExample" variant="secondary" size="xs" labelled />
        </template>
        <pre class="code mono">{{ curlExample }}</pre>
      </UiPanel>
    </div>
    <UiModal v-if="showCreate" :title="t('keys.create')" size="md" @close="showCreate = false">
      <form id="create-key" @submit.prevent="submitCreate">
        <UiField v-slot="{ id }" :label="t('keys.nameLabel')">
          <UiTextInput :id="id" v-model="newKeyName" :maxlength="255" required />
        </UiField>
      </form>
      <template #footer>
        <UiButton variant="ghost" :disabled="creating" @click="showCreate = false">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton
          variant="primary"
          type="submit"
          form="create-key"
          :loading="creating"
          :disabled="!newKeyName.trim()"
        >
          {{ t('keys.create') }}
        </UiButton>
      </template>
    </UiModal>

    <UiConfirmDialog
      v-if="revokeTarget"
      :title="t('keys.revokeTitle')"
      :message="t('keys.revokeConfirm', { name: revokeTarget.name })"
      :confirm-label="t('keys.revoke')"
      :pending="revoking"
      @confirm="confirmRevoke"
      @cancel="revokeTarget = null"
    />
  </SettingsSection>
</template>

<style scoped>
.region {
  display: grid;
  min-width: 0;
}

.state {
  margin-top: var(--space-4);
}

.reveal {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
}

.reveal-title {
  font-weight: var(--weight-semibold);
}

.reveal-key {
  /* A key has no spaces to break at, and it must be readable in full: this is the only
     place it will ever be shown. */
  overflow-wrap: anywhere;
  font-size: var(--text-sm);
  color: var(--ink);
}

.prefix {
  color: var(--muted);
}

.never {
  color: var(--faint);
}

/* A command line must not be re-wrapped into something that no longer runs: it scrolls in
   its own box rather than growing the panel sideways. */
.code {
  margin: 0;
  overflow-x: auto;
  padding-top: var(--space-4);
  border-top: 1px solid var(--hair);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.8;
  white-space: pre;
}
</style>
