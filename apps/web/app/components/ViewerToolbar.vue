<script setup lang="ts">
/**
 * The toolbar a text selection in the document pane gets (docs/web.md § Editing, *The
 * selection toolbar*): Ask the agent, Translate, Quote in notes, Copy and More, over the
 * selection's first line and centered on it, held `--space-2` inside the pane. Flipped below
 * where it would leave the pane's top; under the selection on a coarse pointer, the side the
 * platform's own selection callout leaves free. Where it is still wider than the pane its
 * buttons drop their words, which stay their names.
 *
 * A press on it never moves focus, so the selection — in the open editor or over the rendered
 * blocks — is still there for the action to read. Its buttons are not tab stops: the keyboard
 * reaches the same actions through the block menu.
 */
import type { IconName } from '~/lib/icons'

/** A box on screen, in viewport coordinates. */
export interface ToolbarRect {
  left: number
  top: number
  right: number
  bottom: number
}

const props = defineProps<{
  /** The selection's first line and last line. */
  first: ToolbarRect
  last: ToolbarRect
  /** The pane the toolbar is drawn in. */
  bounds: ToolbarRect
  coarse: boolean
  translate: boolean
  more: boolean
  /** More's menu is open: the button keeps its pressed look. */
  moreOpen: boolean
}>()

const emit = defineEmits<{
  ask: []
  translate: []
  quote: []
  copy: []
  more: [event: MouseEvent]
}>()

const { t } = useI18n()

/** The gap between the toolbar and the line it sits over, and between it and the pane's edge. */
const GAP = 8

const box = ref<HTMLElement | null>(null)
const place = ref({ left: -9999, top: -9999 })
const compact = ref(false)

const buttons = computed(() => {
  const all: { key: 'ask' | 'translate' | 'quote' | 'copy'; icon: IconName; label: string }[] = [
    { key: 'ask', icon: 'message-circle', label: t('viewer.toolbar.ask') },
    { key: 'translate', icon: 'languages', label: t('viewer.toolbar.translate') },
    { key: 'quote', icon: 'notebook-pen', label: t('viewer.toolbar.quote') },
    { key: 'copy', icon: 'copy', label: t('viewer.toolbar.copy') },
  ]
  return all.filter((button) => button.key !== 'translate' || props.translate)
})

async function position() {
  const element = box.value
  if (!element) {
    return
  }
  const { first, last, bounds } = props
  compact.value = false
  await nextTick()
  const room = bounds.right - bounds.left - 2 * GAP
  if (element.offsetWidth > room) {
    compact.value = true
    await nextTick()
  }
  const width = element.offsetWidth
  const height = element.offsetHeight
  const center = (first.left + first.right) / 2
  const left = Math.min(Math.max(center - width / 2, bounds.left + GAP), bounds.right - GAP - width)
  const over = first.top - GAP - height
  // Under the selection where the pane has room for it there, else under its first line.
  const under = last.bottom + GAP + height <= bounds.bottom - GAP ? last.bottom + GAP : first.bottom + GAP
  const top = props.coarse || over < bounds.top + GAP ? under : over
  place.value = { left: left - bounds.left, top: top - bounds.top }
}

watch(
  () => [props.first, props.last, props.bounds, props.coarse, buttons.value.length, props.more],
  () => void position(),
)

onMounted(() => void position())

function press(key: 'ask' | 'translate' | 'quote' | 'copy') {
  if (key === 'ask') {
    emit('ask')
  } else if (key === 'translate') {
    emit('translate')
  } else if (key === 'quote') {
    emit('quote')
  } else {
    emit('copy')
  }
}
</script>

<template>
  <div
    ref="box"
    class="toolbar"
    :class="{ compact }"
    role="toolbar"
    :aria-label="t('viewer.toolbar.label')"
    :style="{ left: `${place.left}px`, top: `${place.top}px` }"
    @mousedown.prevent
  >
    <button
      v-for="button in buttons"
      :key="button.key"
      type="button"
      class="tool"
      tabindex="-1"
      :aria-label="compact ? button.label : undefined"
      :title="compact ? button.label : undefined"
      @click="press(button.key)"
    >
      <UiIcon :name="button.icon" />
      <span v-if="!compact" class="tool-label">{{ button.label }}</span>
    </button>
    <template v-if="more">
      <span class="rule" aria-hidden="true" />
      <button
        type="button"
        class="tool icon"
        :class="{ on: moreOpen }"
        tabindex="-1"
        :aria-label="t('viewer.toolbar.more')"
        :title="t('viewer.toolbar.more')"
        aria-haspopup="menu"
        :aria-expanded="moreOpen ? 'true' : 'false'"
        @click="emit('more', $event)"
      >
        <UiIcon name="ellipsis" />
      </button>
    </template>
  </div>
</template>

<style scoped>
/* The menu's floating surface with 4px of padding (docs/web.md § Design system). */
.toolbar {
  position: absolute;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  white-space: nowrap;
  animation: toolbar-in var(--duration-fast) var(--ease-enter);
}

@supports not (backdrop-filter: blur(1px)) {
  .toolbar {
    background: var(--paper);
  }
}

@keyframes toolbar-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .toolbar {
    animation: none;
  }
}

/* A hovered button is the context menu's highlighted row on another surface: the accent. */
.tool {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  height: 30px;
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.tool.icon,
.toolbar.compact .tool {
  justify-content: center;
  width: 30px;
  padding: 0;
}

.tool:hover {
  background: var(--accent);
  color: var(--accent-fg);
}

.tool.on {
  background: var(--rail-active);
}

.tool > :deep(svg) {
  flex: none;
  width: 15px;
  height: 15px;
}

.rule {
  flex: none;
  width: 1px;
  height: 18px;
  margin-inline: 2px;
  background: var(--menu-edge);
}
</style>
