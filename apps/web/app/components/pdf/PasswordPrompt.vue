<script setup lang="ts">
/**
 * A PDF's password, asked for in the pane (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer,
 * *Password*): never the browser's dialog. The password goes from the field to pdf.js through
 * `submit` and is kept nowhere else; the field holds it only until the prompt goes.
 *
 * Each ask focuses the field. A wrong password keeps the text, selected, under a `role="alert"`
 * line the field is described by; the first ask shows no error.
 */
import type { PdfPasswordPrompt } from '~/composables/usePdfDocument'

const props = defineProps<{ prompt: PdfPasswordPrompt }>()

const emit = defineEmits<{ submit: [password: string] }>()

const { t } = useI18n()

const inputId = useId()
const errorId = useId()
const password = ref('')
const form = ref<HTMLFormElement | null>(null)

const wrong = computed(() => props.prompt.reason === 'incorrect')

function focusField() {
  const input = form.value?.querySelector('input')
  input?.focus()
  if (wrong.value) {
    input?.select()
  }
}

function onSubmit() {
  if (!props.prompt.checking) {
    emit('submit', password.value)
  }
}

// Every ask, the first on mount and each one after a wrong password, puts the reader back in
// the field.
watch(
  () => props.prompt.attempt,
  () => void nextTick(focusField),
)
onMounted(focusField)
</script>

<template>
  <form ref="form" class="prompt" @submit.prevent="onSubmit">
    <UiIcon name="lock" class="glyph" />
    <h3 class="title">{{ t('viewer.pdf.password.title') }}</h3>
    <p class="body">{{ t('viewer.pdf.password.body') }}</p>
    <label :for="inputId" class="sr-only">{{ t('viewer.pdf.password.label') }}</label>
    <UiTextInput
      :id="inputId"
      v-model="password"
      class="password"
      type="password"
      :invalid="wrong"
      :described-by="wrong ? errorId : undefined"
    />
    <p v-if="wrong" :id="errorId" class="error" role="alert">{{ t('viewer.pdf.password.wrong') }}</p>
    <UiButton variant="primary" type="submit" class="submit" :loading="prompt.checking">
      {{ t('viewer.pdf.password.submit') }}
    </UiButton>
  </form>
</template>

<style scoped>
.prompt {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  width: min(420px, 100%);
  text-align: center;
}

.glyph {
  width: 48px;
  height: 48px;
  margin-bottom: var(--space-2);
  color: var(--faint);
}

.title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.body {
  max-width: 44ch;
  margin-bottom: var(--space-2);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.55;
}

.prompt .password {
  width: min(260px, 100%);
}

.error {
  color: var(--danger);
  font-size: var(--text-xs);
}

.submit {
  margin-top: var(--space-3);
}
</style>
