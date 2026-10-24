<script setup lang="ts">
/**
 * The chat agent's skills on the Agent page (docs/web.md § Pages, docs/chat.md § Skills):
 * one table, the built-ins marked, the two switches saved on change, an edit control on
 * every row. No row has a delete: deleting is the edit dialog's, and a built-in is never
 * deleted, only restored.
 */
import { listSkills, updateSkill, type Skill } from '~/lib/api'
import type { TableColumn } from '~/lib/table'

defineProps<{ maxChars: number | null }>()

const { t } = useI18n()
const { resolve } = useApiError()
/** A skill the agent may load is listed in its catalogue block. */
const contextVersion = useAgentContextVersion()

const skills = ref<Skill[] | null>(null)
const error = ref<string | null>(null)
/** The rows whose switch is being saved. */
const saving = reactive(new Set<string>())
/** The dialog: a skill to edit, `new` for one of the reader's own, null when closed. */
const editing = ref<Skill | 'new' | null>(null)

onMounted(async () => {
  try {
    skills.value = (await listSkills()).skills
  } catch (failure) {
    error.value = await resolve(failure)
  }
})

const columns = computed<TableColumn<Skill>[]>(() => [
  { key: 'name', header: t('agents.nameLabel'), width: '13rem' },
  { key: 'description', header: t('agents.skillDescription') },
  { key: 'slash', header: t('agents.skillSlash'), width: '7rem' },
  { key: 'auto', header: t('agents.skillAuto'), width: '5rem' },
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

const rowKey = (row: Skill) => row.name

function replace(saved: Skill, previous: string | null) {
  const list = skills.value ?? []
  skills.value = previous === null ? [...list, saved] : list.map((row) => (row.name === previous ? saved : row))
}

async function toggle(skill: Skill, field: 'user_invocable' | 'model_invocable', event: Event) {
  const box = event.target as HTMLInputElement
  saving.add(skill.name)
  error.value = null
  try {
    const change = field === 'user_invocable' ? { user_invocable: box.checked } : { model_invocable: box.checked }
    replace(await updateSkill(skill.name, change), skill.name)
    contextVersion.changed()
  } catch (failure) {
    // The box shows what is stored, not the click that was refused.
    box.checked = skill[field]
    error.value = await resolve(failure)
  } finally {
    saving.delete(skill.name)
  }
}

function onSaved(saved: Skill, previous: string | null) {
  replace(saved, previous)
  editing.value = null
  contextVersion.changed()
}

function onDeleted(name: string) {
  skills.value = (skills.value ?? []).filter((row) => row.name !== name)
  editing.value = null
  contextVersion.changed()
}
</script>

<template>
  <section class="section" aria-labelledby="agents-skills">
    <div class="section-head">
      <h3 id="agents-skills" class="section-label">{{ t('agents.skillsLabel') }}</h3>
      <UiButton variant="ghost" size="sm" @click="editing = 'new'">
        <template #icon><UiIcon name="plus" /></template>
        {{ t('agents.skillNew') }}
      </UiButton>
    </div>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    <UiSkeleton v-if="skills === null && !error" :rows="2" />
    <div v-else-if="skills" class="table-scroll">
      <UiDataTable :columns="columns" :rows="skills" :row-key="rowKey" :caption="t('agents.skillsCaption')">
        <template #cell-name="{ row }">
          <span class="cell-block">
            <span class="mono name">{{ row.name }}</span>
            <span v-if="row.builtin" class="builtin">{{ t('agents.builtIn') }}</span>
          </span>
        </template>
        <template #cell-description="{ row }">
          <span class="cell-block description" :title="row.description">{{ row.description }}</span>
        </template>
        <template #cell-slash="{ row }">
          <input
            type="checkbox"
            class="check"
            :checked="row.user_invocable"
            :disabled="saving.has(row.name)"
            :aria-label="t('agents.skillSlashAria', { name: row.name })"
            @change="toggle(row, 'user_invocable', $event)"
          >
        </template>
        <template #cell-auto="{ row }">
          <input
            type="checkbox"
            class="check"
            :checked="row.model_invocable"
            :disabled="saving.has(row.name)"
            :aria-label="t('agents.skillAutoAria', { name: row.name })"
            @change="toggle(row, 'model_invocable', $event)"
          >
        </template>
        <template #cell-actions="{ row }">
          <UiButton variant="ghost" size="xs" icon-only :label="t('agents.skillEdit', { name: row.name })" @click="editing = row">
            <template #icon><UiIcon name="edit" /></template>
          </UiButton>
        </template>
      </UiDataTable>
    </div>

    <AgentsSkillDialog
      v-if="editing"
      :skill="editing === 'new' ? null : editing"
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
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
}

/* Narrower than the page tables: two short columns of switches, inside the panel's column. */
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

.check {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--accent);
  cursor: pointer;
}

.check:disabled {
  cursor: not-allowed;
}
</style>
