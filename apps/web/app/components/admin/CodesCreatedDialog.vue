<script setup lang="ts">
/**
 * The created codes, in full, this once (docs/admin.md § Pages, *Redeem codes*): *Copy all*
 * puts them on the clipboard a line each, *Download CSV* saves them with the batch's fields,
 * built here from the answer. There is no close button, and Escape, the scrim and Back do
 * nothing — only *Done* closes it; reloading or closing the tab asks first. The codes live in this dialog's props alone: never in
 * browser storage, and gone with it.
 */
import type { AdminCodesCreated } from '~/lib/api'
import { CODES_CSV_COLUMNS, codesCsv, formatRedeemBy } from '~/lib/admin'
import { formatCount } from '~/lib/format'

const props = defineProps<{ created: AdminCodesCreated }>()

const emit = defineEmits<{ close: [] }>()

const { t, locale } = useI18n()
const router = useRouter()

// A reload or a closed tab would take the codes away: the browser asks first. Registered
// before the build hold, so it is gone by the time that hold's release reloads after Done.
function warnBeforeUnload(event: BeforeUnloadEvent) {
  event.preventDefault()
  // What older browsers read instead of `preventDefault`.
  event.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', warnBeforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', warnBeforeUnload))

// Neither a new build's reload nor Back may take the codes away while they are shown.
useBuildHold(() => true)
const release = router.beforeEach(() => false)
onBeforeUnmount(release)

const count = computed(() => props.created.codes.length)
const lines = computed(() => props.created.codes.map((item) => item.code).join('\n'))

/** The batch in a line: its label, what each code grants, its uses and its last day. */
const subtitle = computed(() => {
  const created = props.created
  const grant =
    created.kind === 'credits'
      ? t(
          'admin.codes.grantCredits',
          { credits: formatCount(created.credits ?? 0, locale.value), n: formatCount(created.valid_days ?? 0, locale.value) },
          created.valid_days ?? 0,
        )
      : t('admin.codes.grantPlan', { n: formatCount(created.days ?? 0, locale.value), plan: created.plan_id ?? '' }, created.days ?? 0)
  return [
    created.label,
    grant,
    t('admin.codes.usesEach', { n: formatCount(created.max_uses, locale.value) }, created.max_uses),
    created.redeem_by ? `${t('admin.codes.redeemBy')} ${formatRedeemBy(created.redeem_by, locale.value)}` : '',
  ]
    .filter(Boolean)
    .join(' · ')
})

function download() {
  const codes = props.created.codes
  const url = URL.createObjectURL(new Blob([codesCsv(props.created)], { type: 'text/csv;charset=utf-8' }))
  const anchor = window.document.createElement('a')
  anchor.href = url
  anchor.download = `redeem-codes-${codes[0]!.id}-${codes[codes.length - 1]!.id}.csv`
  anchor.click()
  // Revoked once the browser has taken the file: a revoke in the same task can cancel it.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <UiModal :title="t('admin.codes.createdTitle', { n: formatCount(count, locale) }, count)" size="md" persistent>
    <template #title>
      <div class="heading">
        <h2 class="heading-title">{{ t('admin.codes.createdTitle', { n: formatCount(count, locale) }, count) }}</h2>
        <p class="heading-sub">{{ subtitle }}</p>
      </div>
    </template>

    <div class="body">
      <UiBanner tone="error">{{ t('admin.codes.createdWarning') }}</UiBanner>
      <div class="bar">
        <span class="muted">{{ t('admin.codes.createdCount', { n: formatCount(count, locale) }, count) }}</span>
        <span class="actions">
          <UiCopyButton :text="lines" labelled :label="t('admin.codes.copyAll')" variant="secondary" size="sm" />
          <UiButton variant="secondary" size="sm" @click="download">
            <template #icon><UiIcon name="download" /></template>
            {{ t('admin.codes.downloadCsv') }}
          </UiButton>
        </span>
      </div>
      <ol class="codes" :aria-label="t('admin.codes.createdList')" tabindex="0">
        <li v-for="item in created.codes" :key="item.id">{{ item.code }}</li>
      </ol>
      <p class="muted">{{ t('admin.codes.csvColumns', { columns: CODES_CSV_COLUMNS.join(', ') }) }}</p>
    </div>

    <template #footer>
      <UiButton variant="primary" @click="emit('close')">{{ t('admin.codes.done') }}</UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.heading {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.heading-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-heading);
}

.heading-sub {
  color: var(--muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.body {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
  font-size: var(--text-xs);
}

.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.actions {
  display: inline-flex;
  gap: var(--space-2);
}

.muted {
  color: var(--muted);
}

/* The codes as they are typed: monospace, a line each, scrolling past a screen. */
.codes {
  max-height: 240px;
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  font-family: var(--mono);
  line-height: 1.8;
  list-style: none;
}
</style>
