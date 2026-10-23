<script setup lang="ts">
/**
 * "Are you sure?" for the actions in this app that destroy something the user would have
 * to re-create: revoking an API key, deleting a skill, clearing every memory.
 *
 * A dialog rather than `window.confirm`: the native one cannot say which key, cannot be
 * styled to mark the action as destructive, and on some browsers is suppressed entirely.
 */
withDefaults(
  defineProps<{
    title: string
    message: string
    /** The destructive verb, e.g. "Revoke". Never a generic "OK". */
    confirmLabel: string
    pending?: boolean
    /**
     * Why the last attempt failed. It belongs in here rather than on the page behind: a
     * dialog stays open when its action fails, and a message under the overlay reads as
     * the button having done nothing at all.
     */
    error?: string | null
  }>(),
  { error: null },
)

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const { t } = useI18n()
</script>

<template>
  <UiModal :title="title" :busy="pending" @close="emit('cancel')">
    <p class="message">{{ message }}</p>
    <UiBanner v-if="error" class="failure" tone="error">{{ error }}</UiBanner>
    <template #footer>
      <UiButton variant="ghost" :disabled="pending" @click="emit('cancel')">
        {{ t('common.cancel') }}
      </UiButton>
      <UiButton variant="danger" :loading="pending" @click="emit('confirm')">
        {{ confirmLabel }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.message {
  color: var(--muted);
  line-height: 1.7;
}

.failure {
  margin-top: var(--space-4);
}
</style>
