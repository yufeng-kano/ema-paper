<script setup lang="ts">
/**
 * Usage (docs/web.md § Pages, `/settings/usage`): three sections behind a tab row under
 * the title — Calls, Conversations, Sandbox — each tab carrying its headline figure, the
 * column below showing the selected one only. Billed, estimated and unpriced never share a figure
 * (docs/api.md § Usage aggregates): a cost with no billed call behind it reads "—", an
 * estimate is always prefixed "≈", and an unpriced call is a count.
 */
import {
  getSandbox,
  getUsage,
  getUsageConversations,
  resetSandbox,
  stopSandbox,
  type SandboxState,
  type UsageConversation,
} from '~/lib/api'
import { formatBytes, formatCost, formatCount, formatDay, formatShortDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'
import { cacheReadRatio, summarizeUsage, type UsageRow } from '~/lib/usage'

const USAGE_DAYS = 30

const { t, locale } = useI18n()
const { resolve } = useApiError()

type Section = 'calls' | 'conversations' | 'sandbox'
const SECTIONS: readonly Section[] = ['calls', 'conversations', 'sandbox']
const section = ref<Section>('calls')

/** Arrow keys move between tabs, as a tab list is expected to. */
function onTabKeydown(event: KeyboardEvent, index: number) {
  const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
  if (!step) {
    return
  }
  event.preventDefault()
  const nextIndex = (index + step + SECTIONS.length) % SECTIONS.length
  section.value = SECTIONS[nextIndex]!
  const list = (event.currentTarget as HTMLElement).parentElement
  list?.querySelectorAll<HTMLElement>('[role="tab"]')[nextIndex]?.focus()
}

// --- calls ---------------------------------------------------------------------------

const { data, pending, errorMessage, refresh } = useAuthedData(() => getUsage(USAGE_DAYS))
const summary = computed(() => (data.value ? summarizeUsage(data.value) : null))

/**
 * The per-day table. The meter column carries no visible header — it ranks the cost column
 * beside it — but keeps an accessible name, since a blank `<th>` is an unnamed column to a
 * screen reader reading the table's structure.
 */
const dayColumns = computed<TableColumn<UsageRow>[]>(() => [
  { key: 'label', header: t('dashboard.columnDate') },
  { key: 'share', header: '', srHeader: t('dashboard.columnShare'), width: '120px' },
  { key: 'agents', header: t('dashboard.columnAgents') },
  { key: 'cost', header: t('dashboard.columnCost'), numeric: true },
  // Estimates and unpriced calls get columns of their own, and only in a period that has
  // any: they are never summed into the billed cost beside them (docs/web.md § Pages).
  ...(summary.value?.totalEstimatedCalls
    ? [{ key: 'estimated', header: t('dashboard.columnEstimated'), numeric: true }]
    : []),
  ...(summary.value?.totalUnpricedCalls
    ? [{ key: 'unpriced', header: t('dashboard.columnUnpriced'), numeric: true }]
    : []),
  { key: 'prompt', header: t('dashboard.columnPrompt'), numeric: true },
  { key: 'completion', header: t('dashboard.columnCompletion'), numeric: true },
])

/** Billed cost, or a dash where nothing was billed: $0 would claim a bill that never existed. */
function billedCost(row: UsageRow): string {
  return row.billedCalls > 0 ? formatCost(row.cost, locale.value) : t('dashboard.none')
}

/** An estimate, prefixed as one — or a dash where no call reported a cost. */
function estimate(cost: number, calls: number): string {
  return calls > 0
    ? t('dashboard.estimatedValue', { cost: formatCost(cost, locale.value) })
    : t('dashboard.none')
}

function percent(ratio: number | null): string {
  return ratio === null
    ? t('dashboard.none')
    : new Intl.NumberFormat(locale.value, { style: 'percent', maximumFractionDigits: 0 }).format(ratio)
}

// --- conversations -------------------------------------------------------------------

const {
  data: conversationsData,
  pending: conversationsPending,
  errorMessage: conversationsError,
  refresh: refreshConversations,
} = useAuthedData(() => getUsageConversations(USAGE_DAYS))

const conversations = computed(() => conversationsData.value?.conversations ?? [])

const conversationTotals = computed(() => {
  const rows = conversations.value
  const sum = (pick: (row: UsageConversation) => number) =>
    rows.reduce((total, row) => total + pick(row), 0)
  const promptTokens = sum((row) => row.prompt_tokens)
  const cacheReadTokens = sum((row) => row.cache_read_tokens)
  const cacheWriteTokens = sum((row) => row.cache_write_tokens)
  return {
    count: rows.length,
    turns: sum((row) => row.turns),
    estimatedCost: sum((row) => row.estimated_cost),
    estimatedCalls: sum((row) => row.estimated_calls),
    unpricedCalls: sum((row) => row.unpriced_calls),
    promptTokens,
    cacheReadTokens,
    cacheRatio: cacheReadRatio(cacheReadTokens, promptTokens, cacheWriteTokens),
  }
})

const conversationColumns = computed<TableColumn<UsageConversation>[]>(() => [
  { key: 'title', header: t('dashboard.columnConversation') },
  { key: 'document', header: t('dashboard.columnDocument') },
  { key: 'turns', header: t('dashboard.columnTurns'), numeric: true },
  { key: 'prompt', header: t('dashboard.columnPrompt'), numeric: true },
  { key: 'cache', header: t('dashboard.columnCacheRead'), numeric: true },
  { key: 'completion', header: t('dashboard.columnCompletion'), numeric: true },
  { key: 'estimated', header: t('dashboard.columnEstimated'), numeric: true },
])

// --- sandbox -------------------------------------------------------------------------

const {
  data: sandbox,
  pending: sandboxPending,
  errorMessage: sandboxError,
  refresh: refreshSandbox,
} = useAuthedData(() => getSandbox())

const sandboxAvailable = computed(() => sandbox.value !== null && sandbox.value.state !== 'unavailable')

function stateTone(state: SandboxState): 'ok' | 'info' | 'neutral' {
  return state === 'running' ? 'ok' : state === 'starting' ? 'info' : 'neutral'
}

/** Hours to one decimal past an hour, minutes under it — the reading a VM's uptime gets. */
function duration(seconds: number): string {
  if (seconds >= 3600) {
    return t('dashboard.hours', {
      count: new Intl.NumberFormat(locale.value, { maximumFractionDigits: 1 }).format(seconds / 3600),
    })
  }
  return t('dashboard.minutes', { count: formatCount(Math.round(seconds / 60), locale.value) })
}

const workspaceShare = computed(() => {
  const workspace = sandbox.value?.workspace
  return workspace && workspace.quota_bytes > 0 ? workspace.bytes / workspace.quota_bytes : 0
})

const breakdownColumns = computed<TableColumn<{ path: string; bytes: number }>[]>(() => [
  { key: 'path', header: t('dashboard.columnPath') },
  { key: 'bytes', header: t('dashboard.columnSize'), numeric: true },
])

const sandboxActionPending = ref(false)
const sandboxActionError = ref<string | null>(null)
const confirmingReset = ref(false)

async function runSandboxAction(action: () => Promise<void>) {
  sandboxActionPending.value = true
  sandboxActionError.value = null
  try {
    await action()
    confirmingReset.value = false
    await refreshSandbox()
  } catch (error) {
    sandboxActionError.value = await resolve(error)
  } finally {
    sandboxActionPending.value = false
  }
}
</script>

<template>
  <SettingsSection :eyebrow="t('dashboard.period', { days: USAGE_DAYS })" :title="t('dashboard.headTitle')">
    <!-- The three sections, each tab carrying its headline figure: an estimate for the
         two that cost something, the VM's state for the third. -->
    <div class="tabs-row">
      <div class="tabs" role="tablist" :aria-label="t('dashboard.sections')">
        <button
          type="button"
          role="tab"
          class="tab"
          :class="{ active: section === 'calls' }"
          :aria-selected="section === 'calls'"
          :tabindex="section === 'calls' ? 0 : -1"
          @click="section = 'calls'"
          @keydown="onTabKeydown($event, 0)"
        >
          <span class="tab-label">{{ t('dashboard.tabCalls') }}</span>
          <span v-if="summary" class="tab-figure mono tabular">
            {{ estimate(summary.totalEstimatedCost, summary.totalEstimatedCalls) }}
          </span>
        </button>
        <button
          type="button"
          role="tab"
          class="tab"
          :class="{ active: section === 'conversations' }"
          :aria-selected="section === 'conversations'"
          :tabindex="section === 'conversations' ? 0 : -1"
          @click="section = 'conversations'"
          @keydown="onTabKeydown($event, 1)"
        >
          <span class="tab-label">{{ t('dashboard.tabConversations') }}</span>
          <span v-if="conversationsData" class="tab-figure mono tabular">
            {{ estimate(conversationTotals.estimatedCost, conversationTotals.estimatedCalls) }}
          </span>
        </button>
        <button
          type="button"
          role="tab"
          class="tab"
          :class="{ active: section === 'sandbox' }"
          :aria-selected="section === 'sandbox'"
          :tabindex="section === 'sandbox' ? 0 : -1"
          @click="section = 'sandbox'"
          @keydown="onTabKeydown($event, 2)"
        >
          <span class="tab-label">{{ t('dashboard.tabSandbox') }}</span>
          <UiStatusDot
            v-if="sandbox"
            class="tab-dot"
            :tone="stateTone(sandbox.state)"
            :label="t(`dashboard.sandboxStates.${sandbox.state}`)"
          />
        </button>
      </div>
      <p class="tabs-note">{{ t('dashboard.estimateNote') }}</p>
    </div>

    <div class="region">
      <!-- ---------- Calls ---------- -->
      <UiPanel v-if="section === 'calls'" :title="t('dashboard.tabCalls')" flush>
        <template #meta>
          <span>{{ t('dashboard.callsMeta') }}</span>
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

        <div class="section-body">
          <!-- A failed refresh keeps the figures it already has: the banner says what went
               wrong, and blanking the page on top of that helps nobody. -->
          <UiBanner v-if="errorMessage" class="gutter" tone="error">{{ errorMessage }}</UiBanner>
          <UiSkeleton v-if="!summary && !errorMessage" class="gutter" :rows="4" />

          <UiEmptyState
            v-else-if="summary?.isEmpty"
            :title="t('dashboard.empty')"
            :body="t('dashboard.emptyBody')"
          />

          <template v-else-if="summary">
            <!-- Figures on rules, not in boxes. The estimate is the accent: it is the number
                 this page exists to answer, and it is data. -->
            <dl class="metrics">
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalEstimatedCost') }}</dt>
                <dd class="figure cost tabular">
                  {{ estimate(summary.totalEstimatedCost, summary.totalEstimatedCalls) }}
                </dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalPromptTokens') }}</dt>
                <dd class="figure tabular">{{ formatCount(summary.totalPromptTokens, locale) }}</dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalCompletionTokens') }}</dt>
                <dd class="figure tabular">{{ formatCount(summary.totalCompletionTokens, locale) }}</dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalUnpriced') }}</dt>
                <dd class="figure tabular">{{ formatCount(summary.totalUnpricedCalls, locale) }}</dd>
              </div>
            </dl>

            <div class="subhead">
              <h3 class="subtitle">{{ t('dashboard.perDay') }}</h3>
              <span class="submeta">{{ t('dashboard.daysRecorded', { count: summary.days.length }) }}</span>
            </div>
            <div class="table-gutter">
              <UiDataTable
                :columns="dayColumns"
                :rows="summary.days"
                :row-key="(row) => row.label"
                :caption="t('dashboard.perDay')"
                density="tight"
              >
                <template #cell-label="{ row }">{{ formatDay(row.label, locale) }}</template>
                <template #cell-share="{ row }">
                  <UiUsageBar :share="row.costShare" :label="formatDay(row.label, locale)" />
                </template>
                <template #cell-agents="{ row }">
                  <span class="mono agents">{{ row.agents.join(' · ') || t('dashboard.none') }}</span>
                </template>
                <template #cell-cost="{ row }">{{ billedCost(row) }}</template>
                <template #cell-estimated="{ row }">
                  {{ estimate(row.estimatedCost, row.estimatedCalls) }}
                </template>
                <template #cell-unpriced="{ row }">
                  {{ row.unpricedCalls ? formatCount(row.unpricedCalls, locale) : t('dashboard.none') }}
                </template>
                <template #cell-prompt="{ row }">{{ formatCount(row.promptTokens, locale) }}</template>
                <template #cell-completion="{ row }">
                  {{ formatCount(row.completionTokens, locale) }}
                </template>
              </UiDataTable>
            </div>

            <!-- Not a table: a model id is long and its token figures are secondary to its
                 cost, so each model gets a small block of its own. -->
            <div class="subhead">
              <h3 class="subtitle">{{ t('dashboard.perModel') }}</h3>
              <span class="submeta">{{ t('dashboard.modelsRecorded', { count: summary.models.length }) }}</span>
            </div>
            <ul class="models">
              <li v-for="row in summary.models" :key="row.label" class="model">
                <div class="model-main">
                  <p class="model-id mono" :title="row.label">{{ row.label }}</p>
                  <UiUsageBar :share="row.costShare" :label="row.label" />
                  <p class="model-tokens tabular">
                    {{
                      t('dashboard.modelTokens', {
                        prompt: formatCount(row.promptTokens, locale),
                        completion: formatCount(row.completionTokens, locale),
                      })
                    }}
                    <template v-if="row.cacheReadTokens">
                      · {{ t('dashboard.modelCache', { ratio: percent(cacheReadRatio(row.cacheReadTokens, row.promptTokens, row.cacheWriteTokens)) }) }}
                    </template>
                  </p>
                  <p v-if="row.estimatedCalls" class="model-tokens tabular">
                    {{ t('dashboard.modelEstimated', { cost: formatCost(row.estimatedCost, locale) }) }}
                  </p>
                  <p v-if="row.unpricedCalls" class="model-tokens tabular">
                    {{ t('dashboard.modelUnpriced', { count: formatCount(row.unpricedCalls, locale) }) }}
                  </p>
                </div>
                <p class="model-cost tabular">{{ billedCost(row) }}</p>
              </li>
            </ul>
          </template>
        </div>
      </UiPanel>

      <!-- ---------- Conversations ---------- -->
      <UiPanel v-else-if="section === 'conversations'" :title="t('dashboard.tabConversations')" flush>
        <template #meta>
          <span>{{ t('dashboard.conversationsMeta') }}</span>
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('common.refresh')"
            :loading="conversationsPending"
            @click="refreshConversations"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </template>

        <div class="section-body">
          <UiBanner v-if="conversationsError" class="gutter" tone="error">{{ conversationsError }}</UiBanner>
          <UiSkeleton v-if="!conversationsData && !conversationsError" class="gutter" :rows="4" />

          <UiEmptyState
            v-else-if="conversationsData && !conversations.length"
            :title="t('dashboard.conversationsEmpty')"
            :body="t('dashboard.conversationsEmptyBody')"
          />

          <template v-else-if="conversationsData">
            <dl class="metrics">
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalEstimatedCost') }}</dt>
                <dd class="figure cost tabular">
                  {{ estimate(conversationTotals.estimatedCost, conversationTotals.estimatedCalls) }}
                </dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalConversations') }}</dt>
                <dd class="figure tabular">{{ formatCount(conversationTotals.count, locale) }}</dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.totalTurns') }}</dt>
                <dd class="figure tabular">{{ formatCount(conversationTotals.turns, locale) }}</dd>
              </div>
              <!-- The figure that says whether the prompt cache is working (docs/chat.md). -->
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.cacheRatio') }}</dt>
                <dd class="figure cost tabular">{{ percent(conversationTotals.cacheRatio) }}</dd>
                <dd class="metric-note">{{ t('dashboard.cacheRatioNote') }}</dd>
              </div>
            </dl>

            <div class="subhead">
              <h3 class="subtitle">{{ t('dashboard.perConversation') }}</h3>
              <span class="submeta">{{ t('dashboard.conversationCount', { count: conversations.length }) }}</span>
            </div>
            <div class="table-gutter">
              <UiDataTable
                :columns="conversationColumns"
                :rows="conversations"
                :row-key="(row) => String(row.id)"
                :caption="t('dashboard.perConversation')"
                density="tight"
              >
                <template #cell-title="{ row }">
                  <span class="cell-block">{{ row.title || t('dashboard.untitledConversation') }}</span>
                </template>
                <template #cell-document="{ row }">
                  <span class="cell-block document">{{ row.document_name || t('dashboard.none') }}</span>
                </template>
                <template #cell-turns="{ row }">{{ formatCount(row.turns, locale) }}</template>
                <template #cell-prompt="{ row }">{{ formatCount(row.prompt_tokens, locale) }}</template>
                <template #cell-cache="{ row }">
                  {{ percent(cacheReadRatio(row.cache_read_tokens, row.prompt_tokens, row.cache_write_tokens)) }}
                </template>
                <template #cell-completion="{ row }">{{ formatCount(row.completion_tokens, locale) }}</template>
                <template #cell-estimated="{ row }">
                  <span class="estimate">{{ estimate(row.estimated_cost, row.estimated_calls) }}</span>
                </template>
              </UiDataTable>
            </div>
          </template>
        </div>
      </UiPanel>

      <!-- ---------- Sandbox ---------- -->
      <UiPanel v-else :title="t('dashboard.tabSandbox')" flush>
        <template #meta>
          <template v-if="sandboxAvailable">
            <UiButton
              variant="ghost"
              size="xs"
              :disabled="sandboxActionPending"
              @click="confirmingReset = true"
            >
              {{ t('dashboard.sandboxReset') }}
            </UiButton>
            <UiButton
              variant="secondary"
              size="xs"
              :disabled="sandboxActionPending || sandbox?.state === 'stopped'"
              :loading="sandboxActionPending && !confirmingReset"
              @click="runSandboxAction(stopSandbox)"
            >
              {{ t('dashboard.sandboxStop') }}
            </UiButton>
          </template>
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('common.refresh')"
            :loading="sandboxPending"
            @click="refreshSandbox"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </template>

        <div class="section-body">
          <UiBanner v-if="sandboxError" class="gutter" tone="error">{{ sandboxError }}</UiBanner>
          <UiBanner v-if="sandboxActionError" class="gutter" tone="error">{{ sandboxActionError }}</UiBanner>
          <UiSkeleton v-if="!sandbox && !sandboxError" class="gutter" :rows="4" />

          <!-- A deployment without a runner: a state, not an error. -->
          <UiEmptyState
            v-else-if="sandbox && !sandboxAvailable"
            :title="t('dashboard.sandboxStates.unavailable')"
            :body="t('dashboard.sandboxUnavailable')"
          />

          <template v-else-if="sandbox">
            <dl class="metrics">
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.sandboxState') }}</dt>
                <dd class="figure state">
                  <UiStatusDot
                    :tone="stateTone(sandbox.state)"
                    :label="t(`dashboard.sandboxStates.${sandbox.state}`)"
                  />
                </dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.sandboxLastUsed') }}</dt>
                <dd class="figure tabular">
                  {{ sandbox.last_used_at ? formatShortDateTime(sandbox.last_used_at, locale) : t('dashboard.none') }}
                </dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.sandboxUptime') }}</dt>
                <dd class="figure tabular">{{ duration(sandbox.uptime_seconds) }}</dd>
              </div>
              <div class="metric">
                <dt class="eyebrow">{{ t('dashboard.sandboxShells') }}</dt>
                <dd class="figure tabular">{{ formatCount(sandbox.shells, locale) }}</dd>
              </div>
            </dl>

            <div class="subhead">
              <h3 class="subtitle">{{ t('dashboard.workspace') }}</h3>
              <span class="submeta mono tabular">
                {{
                  t('dashboard.workspaceOf', {
                    used: formatBytes(sandbox.workspace.bytes, locale),
                    quota: formatBytes(sandbox.workspace.quota_bytes, locale),
                  })
                }}
              </span>
            </div>
            <div class="gutter">
              <UiUsageBar :share="workspaceShare" :label="t('dashboard.workspaceMeter')" />
            </div>
            <div v-if="sandbox.workspace.breakdown.length" class="table-gutter">
              <UiDataTable
                :columns="breakdownColumns"
                :rows="sandbox.workspace.breakdown"
                :row-key="(row) => row.path"
                :caption="t('dashboard.workspace')"
                density="tight"
              >
                <template #cell-path="{ row }"><span class="mono">{{ row.path }}</span></template>
                <template #cell-bytes="{ row }">{{ formatBytes(row.bytes, locale) }}</template>
              </UiDataTable>
            </div>
            <UiEmptyState v-else compact :title="t('dashboard.workspaceEmpty')" />
          </template>
        </div>
      </UiPanel>
    </div>

    <UiConfirmDialog
      v-if="confirmingReset"
      :title="t('dashboard.sandboxReset')"
      :message="t('dashboard.sandboxResetConfirm')"
      :confirm-label="t('dashboard.sandboxReset')"
      :pending="sandboxActionPending"
      :error="sandboxActionError"
      @confirm="runSandboxAction(resetSandbox)"
      @cancel="confirmingReset = false"
    />
  </SettingsSection>
</template>

<style scoped>
/* --- The tabs ------------------------------------------------------------------ */

.tabs-row {
  display: grid;
  gap: var(--space-3);
  padding: var(--space-6) 0 var(--space-2);
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}

/* The same row the sidebar's nav uses: a fill and a step up in weight say "open",
   nothing else (docs/web.md § Design system). */
.tab {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  height: var(--control-height);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.tab:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.tab.active {
  background: var(--rail-active);
  color: var(--ink);
  font-weight: var(--weight-medium);
}

.tab-label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tab-figure {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-2xs);
  font-weight: var(--weight-normal);
}

.tab-dot {
  flex-shrink: 0;
  font-size: var(--text-2xs);
  font-weight: var(--weight-normal);
}

.tab-dot :deep(.label) {
  color: var(--faint);
}

/* One line under the tabs: what the cost figures are. */
.tabs-note {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

/* --- Region -------------------------------------------------------------------- */

/* The panel body is flush so the metrics band and the tables can run to the panel's
   edges; each part sets its own gutter. */
.section-body {
  display: flex;
  flex-direction: column;
  padding-bottom: var(--space-8);
}

.gutter {
  margin: 0 var(--panel-x) var(--space-4);
}

/* Four figures in a band under the header, on a rule. */
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-6);
  margin: 0;
  padding: var(--space-6) var(--panel-x) var(--space-7);
  border-bottom: 1px solid var(--line);
}

.metric {
  min-width: 0;
}

.metric-note {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--muted);
}

.figure {
  margin: var(--space-2) 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-display);
  font-size: var(--display-lg);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-tight);
  line-height: 1.1;
}

/* Cost is data, and the largest thing on the page. */
.figure.cost {
  color: var(--accent);
}

.figure.state {
  display: flex;
  align-items: center;
}

.figure.state :deep(.dot) {
  width: 9px;
  height: 9px;
}

.subhead {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-6) var(--panel-x) var(--space-2);
}

.subtitle {
  font-size: var(--display-xs);
  letter-spacing: normal;
}

.submeta {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-xs);
}

/* Tables run inside the panel's gutter and scroll sideways there when they must. */
.table-gutter {
  min-width: 0;
  overflow-x: auto;
  padding: 0 var(--panel-x);
}

.agents {
  color: var(--muted);
}

.document {
  color: var(--muted);
}

.estimate {
  color: var(--accent);
}

.models {
  list-style: none;
  margin: 0;
  padding: 0 var(--panel-x);
}

.model {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px;
  align-items: baseline;
  gap: var(--space-4);
  padding: var(--space-4) 0;
  border-top: 1px solid var(--hair);
}

.model-main {
  min-width: 0;
}

.model-id {
  margin-bottom: var(--space-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink);
}

.model-tokens {
  margin-top: var(--space-2);
  font-size: var(--text-2xs);
  color: var(--faint);
}

.model-cost {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--display-sm);
  font-weight: var(--weight-semibold);
  text-align: right;
}

@media (max-width: 900px) {
  .metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .rail-note {
    margin-top: var(--space-4);
  }
}
</style>
