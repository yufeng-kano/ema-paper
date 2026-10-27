<script setup lang="ts">
/**
 * The console's *Users* screen (docs/admin.md § Pages, *Users*; § Users and grants): every
 * account, the newest sign-up first, filtered by plan, searched by address or name and paged,
 * at the left; the chosen one (`?user=<id>`) at the right — what it has, never what it wrote
 * — with *Add credits* and *Add days* for `users.write` and *Add to group* for `groups.write`.
 *
 * The filter, the search, the page and the choice live in the route query
 * (`?plan=&q=&page=&user=`), so Back and a reload return to them; nothing of the list is kept
 * in browser storage. After a grant or an addition the screen reads again rather than
 * patching what it holds.
 */
import {
  ApiRequestError,
  getAdminUser,
  listAdminGroups,
  listAdminUsers,
  type AdminGroup,
  type AdminUser,
  type AdminUserDetail,
  type AdminUsersResponse,
} from '~/lib/api'
import { adminFailure, canManageMembers, idOf, pageOf } from '~/lib/admin'
import { formatBytes, formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

/** How long typing rests before the search runs. */
const SEARCH_DELAY_MS = 300
/** The longest search the field takes: an address is at most 320 characters. */
const SEARCH_MAX = 320
/** Free's id, the API's own plan: every other is a `planId` of `KANO_PAY_PLANS_JSON`. */
const FREE_PLAN = 'free'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const adminConsole = useAdminConsole()
const { explain } = useAdminFailure()

const held = adminConsole.permissions
const canWrite = computed(() => adminConsole.can('users', 'write'))
const canAddToGroups = computed(() => adminConsole.can('groups', 'write') && adminConsole.can('users', 'read'))

const plan = computed(() => (typeof route.query.plan === 'string' && route.query.plan ? route.query.plan : null))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q.trim() : ''))
const page = computed(() => pageOf(route.query.page))
const chosenId = computed(() => idOf(route.query.user))

/** Moves the query; a filter or a search starts again at the first page. */
function setQuery(patch: Record<string, string | null>, replace = false) {
  const query = Object.fromEntries(Object.entries({ ...route.query, ...patch }).filter(([, value]) => value !== null))
  void (replace ? router.replace({ query }) : router.push({ query }))
}

function choosePlan(value: string | null) {
  if (value !== plan.value) {
    setQuery({ plan: value, page: null })
  }
}

function goToPage(next: number) {
  setQuery({ page: next > 1 ? String(next) : null })
}

function choose(id: number) {
  if (id !== chosenId.value) {
    setQuery({ user: String(id) }, true)
  }
}

// --- the search, typed ahead of the query ------------------------------------------------

const search = ref(q.value)
let searchTimer: ReturnType<typeof setTimeout> | undefined

watch(search, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    const next = value.trim()
    if (next !== q.value) {
      setQuery({ q: next || null, page: null }, true)
    }
  }, SEARCH_DELAY_MS)
})

// Back and Forward move the query under the field: the field follows.
watch(q, (value) => {
  if (value !== search.value.trim()) {
    search.value = value
  }
})

onBeforeUnmount(() => clearTimeout(searchTimer))

// --- the list ----------------------------------------------------------------------------

const list = shallowRef<AdminUsersResponse | null>(null)
const listPending = ref(false)
const listError = ref<string | null>(null)
/** Only the newest read may write. */
let listSeq = 0

async function loadList(): Promise<void> {
  const mine = ++listSeq
  listPending.value = true
  try {
    const loaded = await listAdminUsers({ page: page.value, plan: plan.value ?? undefined, q: q.value || undefined })
    if (mine !== listSeq) {
      return
    }
    const last = Math.max(1, Math.ceil(loaded.total / loaded.page_size))
    if (!loaded.items.length && page.value > last) {
      // A page past the end — a link from a longer list: its last page instead, which the
      // query's change reads.
      setQuery({ page: last > 1 ? String(last) : null }, true)
      return
    }
    list.value = loaded
    listError.value = null
  } catch (failure) {
    if (failure instanceof ApiRequestError && failure.status === 422 && plan.value !== null) {
      // A plan no longer configured — a link from before: every plan instead.
      if (mine === listSeq) {
        setQuery({ plan: null, page: null }, true)
      }
      return
    }
    const message = await explain(failure)
    if (mine === listSeq) {
      listError.value = message
    }
  } finally {
    if (mine === listSeq) {
      listPending.value = false
    }
  }
}

watch([plan, q, page], () => {
  void loadList()
})

/** Every account, whatever the filters say: the plans' counts together. */
const accounts = computed(() => (list.value?.plans ?? []).reduce((sum, entry) => sum + entry.accounts, 0))
const paidPlans = computed(() => (list.value?.plans ?? []).filter((entry) => entry.id !== FREE_PLAN))
const filtered = computed(() => plan.value !== null || q.value !== '')

const headline = computed(() => {
  const all = t('admin.users.count', { n: formatCount(accounts.value, locale.value) }, accounts.value)
  const onPlans = paidPlans.value.map((entry) =>
    t('admin.users.onPlan', { n: formatCount(entry.accounts, locale.value), plan: entry.id }),
  )
  return [all, ...onPlans].join(' · ')
})

function planName(id: string): string {
  return id === FREE_PLAN ? t('admin.users.free') : id
}

/** The plan, and what pays for its period: *Free*, or *pro · Subscription*. */
function planLine(row: { id: string; source: string }): string {
  return row.source === 'subscription' || row.source === 'grant'
    ? `${row.id} · ${t(`admin.users.paidBy.${row.source}`)}`
    : planName(row.id)
}

const columns = computed<TableColumn<AdminUser>[]>(() => [
  { key: 'user', header: t('admin.users.user') },
  { key: 'plan', header: t('admin.users.plan') },
  { key: 'credits', header: t('admin.users.credits'), numeric: true },
  { key: 'storage', header: t('admin.users.storage'), numeric: true },
  { key: 'jobs', header: t('admin.users.jobs'), numeric: true },
  { key: 'signedUp', header: t('admin.users.signedUp') },
])

function rowAttrs(row: AdminUser): Record<string, unknown> {
  return {
    class: ['account', { chosen: row.id === chosenId.value }],
    tabindex: 0,
    'aria-current': row.id === chosenId.value ? 'true' : undefined,
    onClick: () => choose(row.id),
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        choose(row.id)
      }
    },
  }
}

// --- the chosen account --------------------------------------------------------------------

const detail = shallowRef<AdminUserDetail | null>(null)
const detailError = ref<string | null>(null)
let detailSeq = 0

async function loadDetail(id: number): Promise<void> {
  const mine = ++detailSeq
  try {
    const loaded = await getAdminUser(id)
    if (mine === detailSeq) {
      detail.value = loaded
      detailError.value = null
    }
  } catch (failure) {
    if (adminFailure(failure).kind === 'notFound') {
      // Deleted meanwhile: the choice goes, and the list is read again without it. A console
      // turned off answers the list's read with its own 404.
      if (mine === detailSeq) {
        setQuery({ user: null }, true)
        void loadList()
      }
      return
    }
    const message = await explain(failure)
    if (mine === detailSeq) {
      detailError.value = message
    }
  }
}

/** The account on screen — never the last one's while the next one loads. */
const shown = computed(() => (detail.value && detail.value.user.id === chosenId.value ? detail.value : null))

watch(chosenId, (id) => {
  detailError.value = null
  if (id !== null) {
    void loadDetail(id)
  }
})

// --- add to group --------------------------------------------------------------------------

const groups = shallowRef<AdminGroup[]>([])

/** The groups *Add to group* may offer. A failed read offers none: the button stays away. */
async function loadGroups(): Promise<void> {
  if (!canAddToGroups.value) {
    groups.value = []
    return
  }
  try {
    groups.value = (await listAdminGroups()).groups
  } catch {
    groups.value = []
  }
}

/** Those whose members the operator may manage and that do not have the account yet. */
const joinable = computed(() => {
  const member = shown.value?.groups ?? []
  return groups.value.filter(
    (group) => canManageMembers(held.value, group) && !member.some((joined) => joined.id === group.id),
  )
})

watch(canAddToGroups, () => {
  void loadGroups()
})

async function refresh(): Promise<void> {
  void loadGroups()
  await loadList()
  if (chosenId.value !== null) {
    await loadDetail(chosenId.value)
  }
}

onMounted(refresh)

// --- the dialogs ---------------------------------------------------------------------------

/** The grant dialog, opened on credits or on days. */
const grantKind = ref<'credits' | 'days' | null>(null)
const groupDialog = ref(false)

async function onGranted() {
  grantKind.value = null
  await refresh()
}

async function onAdded() {
  groupDialog.value = false
  if (chosenId.value !== null) {
    await loadDetail(chosenId.value)
  }
}
</script>

<template>
  <UiScreen>
    <template #toolbar-start>
      <UiSegmented v-if="list" class="filter-wide" glass role="radiogroup" :aria-label="t('admin.users.filter')">
        <button
          type="button"
          role="radio"
          class="segment"
          :class="{ active: plan === null }"
          :aria-checked="plan === null"
          @click="choosePlan(null)"
        >
          {{ t('admin.users.all') }}
        </button>
        <button
          v-for="option in list.plans"
          :key="option.id"
          type="button"
          role="radio"
          class="segment"
          :class="{ active: plan === option.id }"
          :aria-checked="plan === option.id"
          @click="choosePlan(option.id)"
        >
          {{ planName(option.id) }}
        </button>
      </UiSegmented>
      <!-- The same choice where the segments do not fit beside the search. -->
      <UiSelect
        v-if="list"
        class="filter-narrow"
        :model-value="plan ?? ''"
        :aria-label="t('admin.users.filter')"
        @update:model-value="choosePlan($event || null)"
      >
        <option value="">{{ t('admin.users.all') }}</option>
        <option v-for="option in list.plans" :key="option.id" :value="option.id">{{ planName(option.id) }}</option>
      </UiSelect>
    </template>

    <template #toolbar>
      <UiSearchField
        v-model="search"
        class="users-search"
        :label="t('admin.users.search')"
        :placeholder="t('admin.users.searchPlaceholder')"
        :maxlength="SEARCH_MAX"
      />
      <UiButton icon-only :label="t('common.refresh')" :loading="listPending" @click="refresh">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.users.title')">
        <span v-if="list" class="tabular">{{ headline }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <div class="layout">
        <div class="accounts">
          <UiBanner v-if="listError" class="state" tone="error">
            {{ listError }}
            <template #actions>
              <UiButton size="sm" @click="refresh">{{ t('common.retry') }}</UiButton>
            </template>
          </UiBanner>
          <UiSkeleton v-if="!list && !listError" class="state" />
          <UiEmptyState
            v-else-if="list && !list.items.length"
            :title="filtered ? t('admin.users.noMatch') : t('admin.users.empty')"
          />
          <UiDataTable
            v-else-if="list"
            :columns="columns"
            :rows="list.items"
            :row-key="(row) => String(row.id)"
            :row-attrs="rowAttrs"
            :caption="t('admin.users.title')"
          >
            <template #cell-user="{ row }">
              <span class="person">
                <span class="person-name cell-block">{{ row.name?.trim() || row.email }}</span>
                <span v-if="row.name?.trim()" class="person-email cell-block">{{ row.email }}</span>
              </span>
            </template>
            <template #cell-plan="{ row }">{{ planLine(row.plan) }}</template>
            <template #cell-credits="{ row }">{{ formatCount(row.credits_available, locale) }}</template>
            <template #cell-storage="{ row }">
              <span v-if="row.storage_bytes === null" class="muted">{{ t('admin.users.unknown') }}</span>
              <template v-else>{{ formatBytes(row.storage_bytes, locale) }}</template>
            </template>
            <template #cell-jobs="{ row }">{{ formatCount(row.jobs, locale) }}</template>
            <template #cell-signedUp="{ row }">
              <span class="muted tabular">{{ formatDate(row.created_at, locale) }}</span>
            </template>
          </UiDataTable>
          <footer v-if="list && list.total" class="foot">
            <UiPager :page="list.page" :page-size="list.page_size" :total="list.total" @update:page="goToPage" />
          </footer>
        </div>

        <aside class="chosen" :aria-label="shown ? (shown.user.name?.trim() || shown.user.email) : undefined">
          <UiBanner v-if="detailError" tone="error">
            {{ detailError }}
            <template #actions>
              <UiButton size="sm" @click="chosenId !== null && loadDetail(chosenId)">{{ t('common.retry') }}</UiButton>
            </template>
          </UiBanner>
          <p v-if="chosenId === null" class="choose">{{ t('admin.users.choose') }}</p>
          <UiSkeleton v-else-if="!shown && !detailError" :rows="4" />
          <AdminAccountDetail
            v-else-if="shown"
            :detail="shown"
            :can-grant="canWrite"
            :can-grant-days="paidPlans.length > 0"
            :can-add-to-group="canAddToGroups && joinable.length > 0"
            @grant="grantKind = $event"
            @add-to-group="groupDialog = true"
          />
        </aside>
      </div>
    </UiPanel>

    <AdminGrantDialog
      v-if="grantKind && shown && list"
      :user="shown.user"
      :available="shown.credits.available"
      :facts="list.grant"
      :plans="paidPlans.map((entry) => entry.id)"
      :kind="grantKind"
      @close="grantKind = null"
      @granted="onGranted"
    />
    <AdminAddToGroupDialog
      v-if="groupDialog && shown && joinable.length"
      :user="shown.user"
      :groups="joinable"
      @close="groupDialog = false"
      @added="onAdded"
    />
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

.filter-narrow {
  display: none;
  width: auto;
}

.users-search {
  width: 240px;
}

.muted {
  color: var(--muted);
}

/* The list at the left, the chosen account at the right; stacked under the breakpoint. */
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  align-items: start;
  gap: var(--space-6);
  padding-top: var(--space-1);
}

.accounts {
  --table-min: 640px;

  display: grid;
  grid-template-columns: minmax(0, 1fr);
  min-width: 0;
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

/* A row chooses its account: the pointer says so, the keyboard reaches it, and the chosen
   one carries the accent's tint. */
:deep(tr.account) {
  cursor: pointer;
}

:deep(tr.account.chosen) {
  background: var(--accent-tint);
}

.foot {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-3) var(--cell-x) 0;
}

.chosen {
  display: grid;
  gap: var(--space-3);
  min-width: 0;
  padding: var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.choose {
  color: var(--muted);
  font-size: var(--text-sm);
}

@media (max-width: 1180px) {
  .filter-wide {
    display: none;
  }

  .filter-narrow {
    display: block;
  }
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .users-search {
    width: auto;
    flex: 1;
  }
}
</style>
