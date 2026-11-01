<script setup lang="ts">
/**
 * The outline where its ticks do not fit (docs/web.md § Result viewer, *Outline, collapse and
 * the way back*): a narrow window, the stacked layout, *Body margins* at *None* or *Narrow*.
 * An *Outline* button in the header's capsule opens the ticks' panel (`ViewerOutlinePanel`)
 * under it, as More opens its menu: a press opens and closes it, it is no hover region.
 *
 * A click opens it with focus left on the button; Enter, Space or ↓ open it with focus on the
 * current row. Escape, a press elsewhere, focus leaving the button and the panel, a jump and
 * a row action close it, focus that was in the panel going back to the button.
 */
import type { ComponentPublicInstance } from 'vue'
import { dockOutlinePanel } from '~/lib/blocks'
import type { OutlineEntry } from '~/lib/markdown'
import { modalPanelKey } from '~/lib/menu'

defineProps<{
  entries: readonly OutlineEntry[]
  /** The current section, by outline index; null above the first heading. */
  current: number | null
  /** The row actions — Files, once the document can be edited. */
  actions: boolean
  /** A user-invocable `translate` skill exists: the row offers it. */
  translate: boolean
}>()

const emit = defineEmits<{
  jump: [index: number]
  translate: [index: number]
  context: [index: number]
}>()

const { t } = useI18n()

/** The panel's room: this far under the button and from the window's sides, and this far
 *  above the window's foot — where the cards end. */
const GAP_PX = 8
const FOOT_PX = 16
const PANEL_WIDTH_PX = 264

// Over the dialog's panel, as the menus are: still above every scroll container, and inside
// the dialog's focus trap.
const modalPanel = inject(modalPanelKey, null)
const panelId = useId()

const open = ref(false)
const place = ref({ top: 0, left: 0, maxHeight: 0 })
const button = ref<ComponentPublicInstance | null>(null)
const nav = ref<HTMLElement | null>(null)
const panel = ref<{ root: HTMLElement | null; focusCurrent: () => void } | null>(null)
/** The pointer is down on the button or in the panel: the focus such a press takes is not the
 *  reader leaving — Safari, which focuses no pressed button, blurs whatever had focus — and
 *  the click that follows does what the press meant. */
let pressing = false

function buttonEl(): HTMLElement | null {
  const element: unknown = button.value?.$el
  return element instanceof HTMLElement ? element : null
}

function within(target: EventTarget | null): boolean {
  return target instanceof Node && Boolean(buttonEl()?.contains(target) || nav.value?.contains(target))
}

async function show(focusRow: boolean) {
  const box = buttonEl()?.getBoundingClientRect()
  if (!box) {
    return
  }
  place.value = dockOutlinePanel(
    box,
    { width: window.innerWidth, height: window.innerHeight },
    { width: PANEL_WIDTH_PX, gap: GAP_PX, foot: FOOT_PX },
  )
  open.value = true
  await nextTick()
  if (focusRow) {
    panel.value?.focusCurrent()
  }
}

function hide(refocus: boolean) {
  open.value = false
  if (refocus) {
    buttonEl()?.focus()
  }
}

/** Closed by what the reader did in it: focus that was in the panel goes back to the button. */
function hideFromPanel() {
  hide(nav.value?.contains(document.activeElement) ?? false)
}

/** A click the keyboard made carries no pointer detail: Enter and Space open onto the
 *  current row. */
function onClick(event: MouseEvent) {
  if (open.value) {
    hide(false)
  } else {
    void show(event.detail === 0)
  }
}

function onButtonKey(event: KeyboardEvent) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    if (open.value) {
      panel.value?.focusCurrent()
    } else {
      void show(true)
    }
  } else if (event.key === 'Escape' && open.value) {
    // The panel's Escape, not the dialog's.
    event.stopPropagation()
    event.preventDefault()
    hide(false)
  }
}

function onFocusOut(event: FocusEvent) {
  if (open.value && !pressing && !within(event.relatedTarget)) {
    hide(false)
  }
}

function onPointerDownOutside(event: PointerEvent) {
  if (open.value && !within(event.target)) {
    hide(false)
  }
}

function onJump(index: number) {
  hideFromPanel()
  emit('jump', index)
}

function onTranslate(index: number) {
  hideFromPanel()
  emit('translate', index)
}

function onContext(index: number) {
  hideFromPanel()
  emit('context', index)
}

/** The panel stands where the button was; a window that changes size moves the button. */
function onResize() {
  hide(false)
}

function onPress() {
  pressing = true
}

/** After the release's own click, which reads it. */
function onPointerUp() {
  if (pressing) {
    setTimeout(() => (pressing = false), 0)
  }
}

onMounted(() => {
  window.addEventListener('pointerdown', onPointerDownOutside, true)
  window.addEventListener('pointerup', onPointerUp, true)
  window.addEventListener('pointercancel', onPointerUp, true)
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onPointerDownOutside, true)
  window.removeEventListener('pointerup', onPointerUp, true)
  window.removeEventListener('pointercancel', onPointerUp, true)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <UiButton
    ref="button"
    variant="ghost"
    icon-only
    :label="t('viewer.outline.button')"
    :aria-expanded="open ? 'true' : 'false'"
    :aria-controls="open ? panelId : undefined"
    @pointerdown="onPress"
    @click="onClick"
    @keydown="onButtonKey"
    @focusout="onFocusOut"
  >
    <template #icon><UiIcon name="list-tree" /></template>
  </UiButton>
  <Teleport :to="modalPanel ?? 'body'">
    <nav
      v-if="open"
      :id="panelId"
      ref="nav"
      class="docked"
      :aria-label="t('viewer.outline.nav')"
      :style="{ top: `${place.top}px`, left: `${place.left}px` }"
      @pointerdown="onPress"
      @focusout="onFocusOut"
    >
      <ViewerOutlinePanel
        ref="panel"
        :entries="entries"
        :current="current"
        :max-height="place.maxHeight"
        :actions="actions"
        :translate="translate"
        @jump="onJump"
        @translate="onTranslate"
        @context="onContext"
        @escape="hide(true)"
      />
    </nav>
  </Teleport>
</template>

<style scoped>
/* Over the document and the cards, under the button, as a menu stands. */
.docked {
  position: fixed;
  z-index: 90;
}
</style>
