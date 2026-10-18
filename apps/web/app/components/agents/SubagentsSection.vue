<script setup lang="ts">
/**
 * The chat agent's subagents on the Agent page (docs/web.md § Pages, docs/chat.md
 * § Subagents): one table, the built-ins marked, each row's model (or *Follows the
 * conversation*) and how many read-only tools it holds, an edit control on every row. No
 * row has a delete: deleting is the edit dialog's.
 */
import { listSubagents, type ConnectionModel, type Subagent } from '~/lib/api'
import type { TableColumn } from '~/lib/table'

defineProps<{
  /** The chat agent's catalog, for the dialog's model picker. */
  models: ConnectionModel[] | null
  maxChars: number | null
}>()

const { t } = useI18n()
const { resolve } = useApiError()

const subagents = ref<Subagent[] | null>(null)
const error = ref<string | null>(null)
/** The dialog: a subagent to edit, `new` for one of the reader's own, null when closed. */
const editing = ref<Subagent | 'new' | null>(null)

onMounted(async () => {
  try {
    subagents.value = (await listSubagents()).subagents
  } catch (failure) {
    error.value = await resolve(failure)
  }
})

const columns = computed<TableColumn<Subagent>[]>(() => [
  { key: 'name', header: t('agents.nameLabel'), width: '12rem' },
  { key: 'description', header: t('agents.skillDescription') },
  { key: 'model', header: t('agents.subagentModel'), width: '11rem' },
  { key: 'tools', header: t('agents.subagentTools'), width: '8.5rem' },
  // The edit button's width past the column's left padding: any narrower and the button
  // overflows the table's edge, which then scrolls sideways.
  {
    key: 'actions',
    header: '',
    srHeader: t('agents.actions'),
    width: 'calc(var(--cell-x) + var(--control-height-xs))',
    align: 'end',
  },
])

const rowKey = (row: Subagent) => row.name

function onSaved(saved: Subagent, previous: string | null) {
  const list = subagents.value ?? []
  subagents.value = previous === null ? [...list, saved] : list.map((row) => (row.name === previous ? saved : row))
  editing.value = null
}

function onDeleted(name: string) {
  subagents.value = (subagents.value ?? []).filter((row) => row.name !== name)
  editing.value = null
}
</script>

<template>
  <section class="section" aria-labelledby="agents-subagents">
    <div class="section-head">
      <h3 id="agents-subagents" class="section-label">{{ t('agents.subagentsLabel') }}</h3>
      <UiButton variant="ghost" size="sm" @click="editing = 'new'">
        <template #icon><UiIcon name="plus" /></template>
        {{ t('agents.subagentNew') }}
      </UiButton>
    </div>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    <UiSkeleton v-if="subagents === null && !error" :rows="2" />
    <div v-else-if="subagents" class="table-scroll">
      <UiDataTable :columns="columns" :rows="subagents" :row-key="rowKey" :caption="t('agents.subagentsCaption')">
        <template #cell-name="{ row }">
          <span class="cell-block">
            <span class="mono name">{{ row.name }}</span>
            <span v-if="row.builtin" class="builtin">{{ t('agents.builtIn') }}</span>
          </span>
        </template>
        <template #cell-description="{ row }">
          <span class="cell-block description" :title="row.description">{{ row.description }}</span>
        </template>
        <template #cell-model="{ row }">
          <span v-if="row.model" class="cell-block mono model" :title="row.model">{{ row.model }}</span>
          <span v-else class="cell-block follows">{{ t('agents.followsConversation') }}</span>
        </template>
        <template #cell-tools="{ row }">
          <span class="tools">{{
            t(row.writes ? 'agents.subagentToolsWrites' : 'agents.subagentToolsCount', { n: row.tools.length }, row.tools.length)
          }}</span>
        </template>
        <template #cell-actions="{ row }">
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('agents.subagentEdit', { name: row.name })"
            @click="editing = row"
          >
            <template #icon><UiIcon name="edit" /></template>
          </UiButton>
        </template>
      </UiDataTable>
    </div>

    <AgentsSubagentDialog
      v-if="editing"
      :subagent="editing === 'new' ? null : editing"
      :models="models"
      :max-chars="maxChars"
      @close="editing = null"
      @saved="onSaved"
      @deleted="onDeleted"
    />
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
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

.table-scroll {
  --table-min: 36rem;

  overflow-x: auto;
}

/* The description takes what the fixed columns leave, cut to one line. */
.table-scroll :deep(table) {
  table-layout: fixed;
}

.name {
  color: var(--ink);
}

.builtin {
  margin-left: var(--space-2);
  color: var(--faint);
  font-size: var(--text-2xs);
}

.description {
  color: var(--ink-soft);
}

.model {
  color: var(--ink-soft);
}

.follows,
.tools {
  color: var(--muted);
  font-size: var(--text-xs);
}
</style>
