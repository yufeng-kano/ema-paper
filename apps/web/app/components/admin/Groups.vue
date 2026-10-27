<script setup lang="ts">
/**
 * The console's *Groups* screen (docs/admin.md § Pages, *Groups*): the groups at the left,
 * the selected one (`?group=<id>`, the first by default) at the right — its permission matrix
 * and its members.
 *
 * Every control follows what the operator holds (`app/lib/admin.ts`): *Create group* and
 * *Rename* need `groups.write`; a matrix cell is editable only where everything the click
 * would change is held; members and *Delete group* need every permission the group gives;
 * the built-in group is never edited. The server checks each again, and its refusals are
 * said where the action was taken — on the group it was made on, never on one chosen since.
 * After every change, and after a refusal, the screen re-reads rather than patching what it
 * holds; a 404 that turns out to be the group deleted by someone else says so above the
 * groups and selects what is left.
 */
import {
  addAdminGroupMember,
  deleteAdminGroup,
  getAdminGroup,
  listAdminGroups,
  removeAdminGroupMember,
  updateAdminGroup,
  type AdminAreaLevels,
  type AdminGroup,
  type AdminGroupDetail,
  type AdminGroupsResponse,
  type AdminMember,
  type AdminPerson,
} from '~/lib/api'
import {
  adminFailure,
  can,
  canDeleteGroup,
  canManageMembers,
  canRenameGroup,
  canToggle,
  idOf,
  toggledPermissions,
  type AdminLevel,
} from '~/lib/admin'
import { formatCount, formatDate, formatDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const { t, te, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const adminConsole = useAdminConsole()
const { explain } = useAdminFailure()

const held = adminConsole.permissions
const canWrite = computed(() => can(held.value, 'groups', 'write'))

// --- the groups ------------------------------------------------------------------------

const list = shallowRef<AdminGroupsResponse | null>(null)
const listError = ref<string | null>(null)
/** Said above the groups until they are next read: the group a change was made on had been
 *  deleted by someone else. */
const notice = ref<string | null>(null)
/** Only the newest read may write: an answer read before a change must not undo it. */
let listSeq = 0

async function loadList(): Promise<void> {
  const mine = ++listSeq
  try {
    const loaded = await listAdminGroups()
    if (mine === listSeq) {
      list.value = loaded
      listError.value = null
      notice.value = null
    }
  } catch (error) {
    const message = await explain(error)
    if (mine === listSeq) {
      listError.value = message
    }
  }
}

/**
 * After a 404 about group `id`: reads the groups again and says whether it is gone — deleted
 * by someone else meanwhile — in which case the page says so above the groups and the
 * selection falls back to what is left. A group still there means the 404 was about something
 * else the request named: an address, or a member who already left.
 */
async function gone(id: number): Promise<boolean> {
  await loadList()
  if (!list.value || list.value.groups.some((group) => group.id === id)) {
    return false
  }
  notice.value = t('admin.groups.gone')
  return true
}

/** The group the query names, else the first — the built-in one. */
const selected = computed<AdminGroup | null>(() => {
  const groups = list.value?.groups ?? []
  const wanted = idOf(route.query.group)
  return groups.find((group) => group.id === wanted) ?? groups[0] ?? null
})

function select(id: number | null) {
  const query = { ...route.query }
  if (id === null) {
    delete query.group
  } else {
    query.group = String(id)
  }
  void router.replace({ query })
}

// --- the selected group's members ------------------------------------------------------

const detail = shallowRef<AdminGroupDetail | null>(null)
const detailError = ref<string | null>(null)
let detailSeq = 0

async function loadDetail(id: number): Promise<void> {
  const mine = ++detailSeq
  try {
    const loaded = await getAdminGroup(id)
    if (mine === detailSeq) {
      detail.value = loaded
      detailError.value = null
    }
  } catch (error) {
    // Deleted meanwhile, by someone else: the list read again selects what is left.
    if (adminFailure(error).kind === 'notFound') {
      await loadList()
      return
    }
    const message = await explain(error)
    if (mine === detailSeq) {
      detailError.value = message
    }
  }
}

/** The members of the group on screen — never the last group's while the next one loads. */
const members = computed<AdminMember[] | null>(() =>
  detail.value && detail.value.group.id === selected.value?.id ? detail.value.members : null,
)

async function refresh(): Promise<void> {
  await loadList()
  if (selected.value) {
    await loadDetail(selected.value.id)
  }
}

onMounted(loadList)

// --- labels ----------------------------------------------------------------------------

/** A person as the console names them: their name, else their account number. */
function personName(person: AdminPerson): string {
  return person.name?.trim() || t('admin.accountId', { id: person.id })
}

function areaLabel(key: string): string {
  return te(`admin.areas.${key}`) ? t(`admin.areas.${key}`) : key
}

function levelLabel(key: string, level: string): string {
  return te(`admin.permissions.${key}.${level}`) ? t(`admin.permissions.${key}.${level}`) : `${key}.${level}`
}

function groupSummary(group: AdminGroup): string {
  const people = t('admin.groups.memberCount', { n: formatCount(group.member_count, locale.value) }, group.member_count)
  const permissions = group.builtin
    ? t('admin.groups.allPermissions')
    : t('admin.groups.permissionCount', { n: formatCount(group.permissions.length, locale.value) }, group.permissions.length)
  return `${people} · ${permissions}`
}

const headline = computed(() => {
  const count = list.value?.groups.length ?? 0
  return t('admin.groups.count', { n: formatCount(count, locale.value) }, count)
})

// --- the permission matrix -------------------------------------------------------------

const matrixColumns = computed<TableColumn<AdminAreaLevels>[]>(() => [
  { key: 'area', header: t('admin.groups.area'), width: '22%' },
  { key: 'read', header: t('admin.groups.read') },
  { key: 'write', header: t('admin.groups.write') },
])

/** A tick is saving: every box is `locked` — inert, yet the one pressed keeps the focus. */
const saving = ref(false)
/** What a tick sent, and for which group: the matrix shows it while the save runs, on that
 *  group only. */
const sent = shallowRef<{ groupId: number; permissions: string[] } | null>(null)
const matrixError = ref<string | null>(null)

const shownPermissions = computed(() =>
  sent.value && sent.value.groupId === selected.value?.id ? sent.value.permissions : (selected.value?.permissions ?? []),
)

function editable(area: string, level: AdminLevel): boolean {
  const group = selected.value
  return !!group && !!list.value && canToggle(held.value, group, area, level, list.value.permissions)
}

async function toggle(area: string, level: AdminLevel, on: boolean) {
  const group = selected.value
  const all = list.value
  if (!group || !all || saving.value) {
    return
  }
  const next = toggledPermissions(group.permissions, area, level, on, all.permissions)
  saving.value = true
  sent.value = { groupId: group.id, permissions: next }
  matrixError.value = null
  try {
    // `based_on`: what this page edited from — a list changed meanwhile answers `stale`
    // rather than being overwritten.
    await updateAdminGroup(group.id, { permissions: next, based_on: [...group.permissions] })
    await refresh()
  } catch (error) {
    if (!(adminFailure(error).kind === 'notFound' && (await gone(group.id)))) {
      const message = await explain(error, t('admin.groups.gone'))
      if (message !== null) {
        // The boxes show what is stored now — someone else's change included.
        await loadList()
        if (selected.value?.id === group.id) {
          matrixError.value = message
        }
      }
    }
  } finally {
    saving.value = false
    sent.value = null
  }
}

// --- members ---------------------------------------------------------------------------

const manageable = computed(() => !!selected.value && canManageMembers(held.value, selected.value))

const memberColumns = computed<TableColumn<AdminMember>[]>(() => [
  { key: 'member', header: t('admin.groups.member') },
  { key: 'added', header: t('admin.groups.added') },
  ...(manageable.value
    ? [{ key: 'remove', header: '', srHeader: t('admin.groups.remove'), align: 'end' as const, width: '96px' }]
    : []),
])

function memberName(member: AdminMember): string {
  return member.name?.trim() || member.email
}

function addedLine(member: AdminMember): string {
  const date = formatDate(member.added_at, locale.value)
  return member.added_by
    ? t('admin.groups.addedBy', { name: personName(member.added_by), date })
    : t('admin.groups.addedByDeleted', { date })
}

const email = ref('')
const adding = ref(false)
const addError = ref<string | null>(null)

async function addMember() {
  const group = selected.value
  const address = email.value.trim()
  if (!group || !address || adding.value) {
    return
  }
  adding.value = true
  addError.value = null
  try {
    await addAdminGroupMember(group.id, { email: address })
    email.value = ''
    await refresh()
  } catch (error) {
    if (!(adminFailure(error).kind === 'notFound' && (await gone(group.id)))) {
      // The group is there, so a 404 is the address's.
      const message = await explain(error, t('admin.groups.noAccount', { name: t('app.name') }))
      if (selected.value?.id === group.id) {
        addError.value = message
      }
    }
  } finally {
    adding.value = false
  }
}

watch(email, () => {
  addError.value = null
})

/** The member *Remove* asks about. */
const removeTarget = ref<AdminMember | null>(null)
const removing = ref(false)
const removeError = ref<string | null>(null)

function askRemove(member: AdminMember) {
  removeError.value = null
  removeTarget.value = member
}

async function confirmRemove() {
  const group = selected.value
  const member = removeTarget.value
  if (!group || !member || removing.value) {
    return
  }
  removing.value = true
  removeError.value = null
  // Back would leave the dialog while the request runs, and its answer would land unseen.
  const release = router.beforeEach(() => false)
  let failure: unknown = null
  try {
    await removeAdminGroupMember(group.id, member.user_id)
  } catch (error) {
    failure = error
  } finally {
    release()
  }
  if (failure && adminFailure(failure).kind !== 'notFound') {
    removeError.value = await explain(failure)
  } else {
    // Done — or a 404: the member already left, or the group itself is gone; either way
    // they are not in it.
    removeTarget.value = null
    if (!(failure && (await gone(group.id)))) {
      await refresh()
    }
  }
  removing.value = false
}

// A new selection reads its members; a refusal said about the last group is not about it.
watch(
  () => selected.value?.id,
  (id) => {
    detailError.value = null
    matrixError.value = null
    addError.value = null
    if (id !== undefined) {
      void loadDetail(id)
    }
  },
)

// --- create, rename, delete ------------------------------------------------------------

/** The name dialog: a new group, or the selected one renamed. */
const nameDialog = ref<'create' | 'rename' | null>(null)

async function onCreated(group: AdminGroup) {
  nameDialog.value = null
  await loadList()
  select(group.id)
}

async function onRenamed() {
  nameDialog.value = null
  await refresh()
}

/** The group being renamed was deleted meanwhile. */
async function onRenameGone(id: number) {
  nameDialog.value = null
  if (!(await gone(id))) {
    await refresh()
  }
}

const deleteOpen = ref(false)
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function askDelete() {
  deleteError.value = null
  deleteOpen.value = true
}

async function confirmDelete() {
  const group = selected.value
  if (!group || deleting.value) {
    return
  }
  deleting.value = true
  deleteError.value = null
  const release = router.beforeEach(() => false)
  let failure: unknown = null
  try {
    await deleteAdminGroup(group.id)
  } catch (error) {
    failure = error
  } finally {
    release()
  }
  if (failure && adminFailure(failure).kind !== 'notFound') {
    deleteError.value = await explain(failure)
  } else {
    // Done — or a 404: someone else deleted it first, which is what was asked.
    deleteOpen.value = false
    select(null)
    await loadList()
  }
  deleting.value = false
}
</script>

<template>
  <UiScreen>
    <template v-if="canWrite" #toolbar>
      <UiButton variant="primary" size="lg" @click="nameDialog = 'create'">
        <template #icon><UiIcon name="plus" /></template>
        {{ t('admin.groups.create') }}
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.groups.title')">
        <span v-if="list" class="tabular">{{ headline }} · {{ t('admin.groups.union') }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="listError" class="state" tone="error">
        {{ listError }}
        <template #actions>
          <UiButton size="sm" @click="refresh">{{ t('common.retry') }}</UiButton>
        </template>
      </UiBanner>
      <UiBanner v-if="notice" class="state" tone="warn">{{ notice }}</UiBanner>
      <UiSkeleton v-if="!list && !listError" class="state" />
      <UiEmptyState v-else-if="list && !list.groups.length" :title="t('admin.groups.empty')" />

      <div v-else-if="list && selected" class="layout">
        <nav class="group-list" :aria-label="t('admin.groups.listLabel')">
          <NuxtLink
            v-for="group in list.groups"
            :key="group.id"
            class="group-row"
            :class="{ selected: group.id === selected.id }"
            :to="{ query: { ...route.query, group: String(group.id) } }"
            :aria-current="group.id === selected.id ? 'true' : undefined"
            replace
          >
            <span class="group-name">
              <span class="name">{{ group.name }}</span>
              <span v-if="group.builtin" class="builtin">{{ t('admin.groups.builtin') }}</span>
            </span>
            <span class="group-meta tabular">{{ groupSummary(group) }}</span>
          </NuxtLink>
        </nav>

        <section class="group" :aria-labelledby="`admin-group-${selected.id}`">
          <header class="group-head">
            <div class="group-title">
              <h2 :id="`admin-group-${selected.id}`" class="title">{{ selected.name }}</h2>
              <span v-if="selected.updated_by" class="changed">
                {{
                  t('admin.groups.changedBy', {
                    name: personName(selected.updated_by),
                    date: formatDateTime(selected.updated_at, locale),
                  })
                }}
              </span>
            </div>
            <div class="group-actions">
              <UiButton v-if="canRenameGroup(held, selected)" size="sm" @click="nameDialog = 'rename'">
                {{ t('admin.groups.rename') }}
              </UiButton>
              <UiButton v-if="canDeleteGroup(held, selected)" variant="danger" size="sm" @click="askDelete">
                {{ t('admin.groups.delete') }}
              </UiButton>
            </div>
          </header>

          <section class="block">
            <div class="block-head">
              <h3 class="block-title">{{ t('admin.groups.permissions') }}</h3>
              <span class="note">{{ t('admin.groups.permissionsNote') }}</span>
            </div>
            <UiBanner v-if="matrixError" tone="error">{{ matrixError }}</UiBanner>
            <UiDataTable
              class="matrix"
              :columns="matrixColumns"
              :rows="list.areas"
              :row-key="(area) => area.key"
              :caption="t('admin.groups.matrixCaption', { name: selected.name })"
            >
              <template #cell-area="{ row }">
                <span class="area">{{ areaLabel(row.key) }}</span>
              </template>
              <template #cell-read="{ row }">
                <UiCheckbox
                  v-if="row.levels.includes('read')"
                  :model-value="shownPermissions.includes(`${row.key}.read`)"
                  :disabled="!editable(row.key, 'read')"
                  :locked="saving"
                  @update:model-value="toggle(row.key, 'read', $event)"
                >
                  {{ levelLabel(row.key, 'read') }}
                </UiCheckbox>
              </template>
              <template #cell-write="{ row }">
                <UiCheckbox
                  v-if="row.levels.includes('write')"
                  :model-value="shownPermissions.includes(`${row.key}.write`)"
                  :disabled="!editable(row.key, 'write')"
                  :locked="saving"
                  @update:model-value="toggle(row.key, 'write', $event)"
                >
                  {{ levelLabel(row.key, 'write') }}
                </UiCheckbox>
                <span v-else class="no-write">
                  <span class="dash" aria-hidden="true">—</span>
                  <span>{{ t('admin.groups.noWrite') }}</span>
                </span>
              </template>
            </UiDataTable>
          </section>

          <section class="block">
            <div class="block-head">
              <h3 class="block-title">
                {{ t('admin.groups.members') }}
              </h3>
              <form v-if="manageable" class="add" novalidate @submit.prevent="addMember">
                <UiField
                  v-slot="{ id, describedBy, invalid }"
                  class="add-field"
                  :label="t('admin.groups.addByEmail')"
                  :error="addError ?? undefined"
                  label-hidden
                >
                  <UiTextInput
                    :id="id"
                    v-model="email"
                    type="email"
                    autocomplete="off"
                    :placeholder="t('admin.groups.addByEmail')"
                    :described-by="describedBy"
                    :invalid="invalid"
                    :disabled="adding"
                    :maxlength="320"
                  />
                </UiField>
                <UiButton variant="primary" type="submit" :loading="adding" :disabled="!email.trim()">
                  {{ t('admin.groups.add') }}
                </UiButton>
              </form>
            </div>
            <UiBanner v-if="detailError" tone="error">
              {{ detailError }}
              <template #actions>
                <UiButton size="sm" @click="refresh">{{ t('common.retry') }}</UiButton>
              </template>
            </UiBanner>
            <UiSkeleton v-if="!members && !detailError" :rows="2" />
            <UiEmptyState v-else-if="members && !members.length" compact :title="t('admin.groups.noMembers')" />
            <UiDataTable
              v-else-if="members"
              :columns="memberColumns"
              :rows="members"
              :row-key="(member) => String(member.user_id)"
              :caption="t('admin.groups.membersCaption', { name: selected.name })"
            >
              <template #cell-member="{ row }">
                <span class="person">
                  <span class="person-name cell-block">{{ memberName(row) }}</span>
                  <span v-if="row.name?.trim()" class="person-email cell-block">{{ row.email }}</span>
                </span>
              </template>
              <template #cell-added="{ row }">
                <span class="added">{{ addedLine(row) }}</span>
              </template>
              <template #cell-remove="{ row }">
                <UiButton size="xs" :label="t('admin.groups.removeNamed', { name: memberName(row) })" @click="askRemove(row)">
                  {{ t('admin.groups.remove') }}
                </UiButton>
              </template>
            </UiDataTable>
          </section>

          <p v-if="selected.builtin" class="rules">{{ t('admin.groups.builtinNote', { name: selected.name }) }}</p>
        </section>
      </div>
    </UiPanel>

    <AdminGroupDialog
      v-if="nameDialog === 'create'"
      @close="nameDialog = null"
      @saved="onCreated"
    />
    <AdminGroupDialog
      v-else-if="nameDialog === 'rename' && selected"
      :group="selected"
      @close="nameDialog = null"
      @saved="onRenamed"
      @gone="onRenameGone(selected.id)"
    />

    <UiConfirmDialog
      v-if="removeTarget && selected"
      :title="t('admin.groups.removeTitle', { name: memberName(removeTarget) })"
      :message="
        t(selected.builtin ? 'admin.groups.removeOwnerBody' : 'admin.groups.removeBody', {
          name: memberName(removeTarget),
          group: selected.name,
        })
      "
      :confirm-label="t('admin.groups.remove')"
      :pending="removing"
      :error="removeError"
      @confirm="confirmRemove"
      @cancel="removeTarget = null"
    />

    <UiConfirmDialog
      v-if="deleteOpen && selected"
      :title="t('admin.groups.deleteTitle', { name: selected.name })"
      :message="t('admin.groups.deleteBody', { n: formatCount(selected.member_count, locale) }, selected.member_count)"
      :confirm-label="t('admin.groups.delete')"
      :pending="deleting"
      :error="deleteError"
      @confirm="confirmDelete"
      @cancel="deleteOpen = false"
    />
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

/* The groups at the left, the selected one at the right; stacked under the breakpoint. */
.layout {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  align-items: start;
  gap: var(--space-6);
  padding-top: var(--space-1);
}

.group-list {
  display: grid;
  gap: 2px;
  padding: var(--space-2);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.group-row {
  display: grid;
  gap: 2px;
  min-width: 0;
  padding: 10px var(--space-3);
  border-radius: var(--radius-md);
  color: var(--ink);
  transition: background var(--duration-fast) var(--ease);
}

.group-row:hover {
  background: var(--fill-weak);
}

/* The chosen group: the accent's tint, as a chosen row in a source list. */
.group-row.selected {
  background: var(--accent-tint);
}

.group-name {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
}

.group-name .name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.builtin,
.group-meta {
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-normal);
}

.group {
  display: grid;
  gap: var(--space-6);
  min-width: 0;
}

.group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}

.group-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-3);
  min-width: 0;
}

.group-title .title {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: var(--display-sm);
  letter-spacing: var(--tracking-heading);
}

.changed {
  color: var(--muted);
  font-size: var(--text-xs);
}

.group-actions {
  display: flex;
  gap: var(--space-2);
}

/* Narrower floors than a screen's own list: these tables share the width with the groups. */
.block {
  --table-min: 480px;

  display: grid;
  /* A bounded track: the table's floor overflows into the panel's sideways scroll instead
     of widening the block, so the heading and the add field above it keep the screen's width. */
  grid-template-columns: minmax(0, 1fr);
  gap: var(--space-2);
  min-width: 0;
}

.block-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2) var(--space-3);
  padding: 0 var(--cell-x);
}

.block-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  letter-spacing: normal;
}

.note {
  color: var(--muted);
  font-size: var(--text-xs);
}

.matrix .area {
  font-weight: var(--weight-medium);
}

.no-write {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: var(--muted);
  font-size: var(--text-xs);
}

.dash {
  display: inline-flex;
  justify-content: center;
  width: 18px;
  color: var(--faint);
}

/* The matrix's descriptions are quiet beside their boxes. */
.matrix :deep(.text) {
  color: var(--muted);
  font-size: var(--text-xs);
}

.add {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 0;
}

.add-field {
  width: 260px;
  max-width: 100%;
}

.person {
  display: grid;
  min-width: 0;
  padding: 6px 0;
  line-height: 1.35;
}

.person-name {
  font-weight: var(--weight-medium);
}

.person-email {
  color: var(--muted);
  font-size: var(--text-xs);
}

.added {
  color: var(--muted);
}

.rules {
  padding: 0 var(--cell-x);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .add {
    width: 100%;
  }

  .add-field {
    flex: 1;
    width: auto;
  }
}
</style>
