<script setup lang="ts">
/**
 * The shell the skill, subagent and prompt dialogs share (docs/web.md § Pages): the heading
 * with the item's state beside it, the form the caller's fields fill, the last failure, and
 * the footer — **Restore default** on a built-in (the prompt dialog's case, which has no
 * Delete), **Delete** on an existing one of the reader's own, then Cancel and Save. Delete
 * asks first, in a confirmation that stands in for the dialog rather than over it: two
 * dialogs would both answer Escape and both trap Tab. The fields keep their state behind
 * it, since the caller holds them.
 *
 * The caller owns the request and its `pending` and `error`; this asks for them by event.
 */
const props = defineProps<{
  heading: string
  /** The item being edited; null for a new one. A built-in offers Restore default and never
   *  Delete; an existing one of the reader's own offers Delete. */
  item: { builtin: boolean; modified: boolean } | null
  /** The state beside the heading, in place of *Built-in* or *Custom* and whether edited. */
  state?: string
  /** The form's id, which Save submits. */
  formId: string
  /** Every field is the built-in's default: Restore has nothing to put back. */
  isDefault: boolean
  /** Delete's words, the confirmation's title too; needed only where Delete is offered. */
  deleteLabel?: string
  /** The confirmation's question, naming the item. */
  deleteConfirm?: string
  pending: boolean
  error: string | null
  saveDisabled: boolean
}>()

const emit = defineEmits<{
  close: []
  save: []
  restore: []
  /** Confirmed: the caller deletes, and closes the dialog once it is gone. */
  delete: []
  /** The confirmation opened or was cancelled: the failure it showed is no longer current. */
  clearError: []
}>()

const { t } = useI18n()

const builtin = computed(() => props.item?.builtin === true)
const deletable = computed(() => props.item !== null && !props.item.builtin)
const shownState = computed(() => {
  if (props.state !== undefined) {
    return props.state
  }
  if (!props.item) {
    return ''
  }
  const kind = props.item.builtin ? t('agents.builtIn') : t('agents.custom')
  return props.item.modified ? `${kind} ${t('agents.editedSuffix')}` : kind
})

const confirming = ref(false)

function askDelete() {
  confirming.value = true
  emit('clearError')
}

function cancelDelete() {
  confirming.value = false
  emit('clearError')
}
</script>

<template>
  <UiConfirmDialog
    v-if="confirming && deletable"
    :title="deleteLabel ?? ''"
    :message="deleteConfirm ?? ''"
    :confirm-label="t('common.delete')"
    :pending="pending"
    :error="error"
    @confirm="emit('delete')"
    @cancel="cancelDelete"
  />
  <UiModal v-else size="wide" :title="heading" :busy="pending" @close="emit('close')">
    <template #title>
      <div class="heading">
        <h2 class="heading-title">{{ heading }}</h2>
        <span v-if="shownState" class="heading-state" role="status">{{ shownState }}</span>
      </div>
    </template>

    <form :id="formId" class="form" @submit.prevent="emit('save')">
      <slot />
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>

    <template #footer>
      <UiButton v-if="builtin" variant="ghost" class="foot-start" :disabled="pending || isDefault" @click="emit('restore')">
        <template #icon><UiIcon name="rotate-ccw" /></template>
        {{ t('agents.promptRestore') }}
      </UiButton>
      <UiButton v-else-if="deletable" variant="ghost" class="foot-start danger" :disabled="pending" @click="askDelete">
        <template #icon><UiIcon name="trash" /></template>
        {{ deleteLabel }}
      </UiButton>
      <UiButton variant="secondary" :disabled="pending" @click="emit('close')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton variant="primary" type="submit" :form="formId" :loading="pending" :disabled="saveDisabled">
        {{ t('common.save') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.heading {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  min-width: 0;
}

.heading-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-sm);
  letter-spacing: normal;
}

.heading-state {
  flex-shrink: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

/* The fields the dialogs lay out the same way. They are the caller's elements, some inside
   a `UiField` of its own, so they are styled as descendants of the form: a plain scoped rule
   would not reach them, and a `:slotted` one stops at the first component in between. */
.form :deep(.pair) {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
}

@media (max-width: 560px) {
  .form :deep(.pair) {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* The field label's treatment, for a legend and a label outside `UiField`. */
.form :deep(.legend) {
  padding: 0;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
}

/* Same skin as UiTextInput's `.control`, for a text of several lines. */
.form :deep(.area) {
  width: 100%;
  min-width: 0;
  padding: var(--space-2) 10px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.5;
  resize: vertical;
  transition: border-color var(--duration-fast) var(--ease);
}

.form :deep(.mono-area) {
  padding: 10px var(--space-3);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.form :deep(.area:disabled) {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

@media (pointer: coarse) {
  .form :deep(.area) {
    font-size: 16px;
  }
}

/* The count under the long text, at its right. */
.form :deep(.end-count) {
  text-align: right;
}

/* A chip is a button that types for you: a hairline and the mono of the text it goes into. */
.form :deep(.token) {
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.form :deep(.token:hover) {
  background: var(--rail);
  color: var(--ink);
}

.form :deep(.token:disabled) {
  cursor: not-allowed;
  opacity: 0.5;
}

/* The footer's left action stands apart from Cancel and Save. */
.foot-start {
  margin-right: auto;
}

.foot-start.danger {
  color: var(--danger);
}
</style>
