<script setup lang="ts">
/**
 * The Original view's page indicator (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer,
 * *Page indicator*): a pill at the pane's foot reading "3 / 14", which turns into the page
 * field when pressed.
 *
 * It shows when the viewer calls `reveal` (on every scroll) and fades 2 s later, by opacity
 * alone, so it stays in the Tab and VoiceOver order; a faded pill takes no pointer events, so a
 * tap meant for the page never lands on it. Hover, focus inside it and the field being open
 * hold it shown.
 *
 * The field follows `pageInputTarget` for Enter and for leaving alike; where focus goes after
 * a jump is the viewer's, so `jump` says which way it was committed. The field's wrapper owns
 * Escape (`data-escape-owner`), so the dialog stays open while Escape closes the field.
 */
import { pageInputTarget } from '~/lib/pdf/page-input'

const props = defineProps<{
  /** The current page, from 1. */
  page: number
  total: number
  phone: boolean
}>()

const emit = defineEmits<{ jump: [page: number, by: 'enter' | 'leave'] }>()

const { t } = useI18n()

/** How long the pill stays after the last scroll. */
const SHOWN_MS = 2000

const hintId = useId()
const inputId = useId()

const shown = ref(false)
const hovered = ref(false)
const focused = ref(false)
const editing = ref(false)
const text = ref('')
const root = ref<HTMLElement | null>(null)
const pill = ref<HTMLButtonElement | null>(null)
const field = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined

const visible = computed(() => shown.value || hovered.value || focused.value || editing.value)

/** Starts the fade's clock, unless something holds the pill shown. */
function time() {
  clearTimeout(timer)
  if (!hovered.value && !focused.value && !editing.value) {
    timer = setTimeout(() => (shown.value = false), SHOWN_MS)
  }
}

/** Shows the pill and starts its 2 s again: the viewer calls it on every scroll. */
function reveal() {
  shown.value = true
  time()
}

function onHover(over: boolean) {
  hovered.value = over
  if (over) {
    shown.value = true
  }
  time()
}

function onFocusIn() {
  focused.value = true
  shown.value = true
  time()
}

function onFocusOut(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && event.currentTarget instanceof Node && event.currentTarget.contains(next)) {
    return
  }
  focused.value = false
  time()
}

async function openField() {
  text.value = String(props.page)
  editing.value = true
  await nextTick()
  const input = field.value?.querySelector('input')
  input?.focus()
  input?.select()
}

/** Closes the field; `back` returns focus to the pill (Escape, or an Enter that went nowhere). */
async function closeField(back: boolean) {
  editing.value = false
  time()
  await nextTick()
  if (back) {
    pill.value?.focus()
  } else {
    // A removed field may take focus with it without a `focusout`, as Chrome's does.
    focused.value = root.value?.contains(document.activeElement) ?? false
    time()
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The field's Escape, not the dialog's: the field is gone from the page before the key
    // reaches the dialog's listener, so `data-escape-owner` alone cannot hold it back.
    event.stopPropagation()
    event.preventDefault()
    void closeField(true)
  } else if (event.key === 'Enter' && !event.isComposing) {
    event.preventDefault()
    const target = pageInputTarget(text.value, props.page, props.total)
    // Closed first, so the blur its removal causes finds the field already committed.
    void closeField(target === null)
    if (target !== null) {
      emit('jump', target, 'enter')
    }
  }
}

/** Leaving the field (Tab, a click elsewhere, iPhone's *完成*) commits as Enter does, and
 *  leaves focus where it went. */
function onBlur() {
  if (!editing.value) {
    return
  }
  const target = pageInputTarget(text.value, props.page, props.total)
  void closeField(false)
  if (target !== null) {
    emit('jump', target, 'leave')
  }
}

onBeforeUnmount(() => clearTimeout(timer))

defineExpose({ reveal })
</script>

<template>
  <div
    ref="root"
    class="indicator"
    :class="{ faded: !visible, phone }"
    @pointerenter="onHover(true)"
    @pointerleave="onHover(false)"
    @focusin="onFocusIn"
    @focusout="onFocusOut"
  >
    <div v-if="editing" ref="field" class="pill field" data-escape-owner>
      <label :for="inputId" class="field-label">
        <span aria-hidden="true">{{ t('viewer.pdf.pageField') }}</span>
        <span class="sr-only">{{ t('viewer.pdf.pageFieldLabel', { total }, total) }}</span>
      </label>
      <UiTextInput
        :id="inputId"
        v-model="text"
        class="page-input"
        inputmode="numeric"
        @keydown="onKeydown"
        @blur="onBlur"
      />
      <span class="field-total" aria-hidden="true">{{ t('viewer.pdf.pageTotal', { total }) }}</span>
    </div>
    <button
      v-else
      ref="pill"
      type="button"
      class="pill"
      :aria-label="t('viewer.pdf.indicator', { page, total })"
      :aria-describedby="hintId"
      @click="openField"
    >
      {{ t('viewer.pdf.pageOf', { page, total }) }}
    </button>
    <span :id="hintId" class="sr-only">{{ t('viewer.pdf.indicatorHint') }}</span>
  </div>
</template>

<style scoped>
/* Centred at the pane's foot, over the pages: the viewer's root is its positioned box. */
.indicator {
  position: absolute;
  bottom: var(--space-5);
  left: 50%;
  z-index: 2;
  transform: translateX(-50%);
  transition: opacity var(--duration-slow) var(--ease);
}

.indicator.phone {
  bottom: var(--space-6);
}

/* Faded by opacity alone, so it stays in the Tab and VoiceOver order; a press meant for the
   page under it goes through. Focus inside shows it again whatever the timer says. */
.indicator.faded:not(:focus-within) {
  opacity: 0;
  pointer-events: none;
}

/* Opaque, with a hairline and no shadow: it sits on white paper and has to read there in
   both themes (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Colours*). */
.pill {
  display: flex;
  align-items: center;
  height: var(--control-height);
  padding: 0 var(--space-4);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-full);
  background: var(--menu-surface);
  color: var(--ink);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* A phone's pill is a finger's 44px, at 15px. */
.indicator.phone .pill {
  height: var(--control-height-lg);
  padding: 0 18px;
  font-size: var(--text-base);
}

button.pill {
  cursor: pointer;
}

.field {
  gap: var(--space-2);
  padding: 0 14px 0 var(--space-3);
  font-weight: var(--weight-normal);
}

.field-label,
.field-total {
  color: var(--muted);
}

/* 22px tall, so the 2px ring at its 2px offset clears the pill's edge; under a coarse pointer
   `UiTextInput` keeps its text at 16px, so Safari on iOS does not zoom the page. */
.field .page-input {
  width: 44px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--fill);
  color: var(--ink);
  font-weight: var(--weight-semibold);
  text-align: center;
}

@media (prefers-reduced-motion: reduce) {
  .indicator {
    transition: none;
  }
}
</style>
