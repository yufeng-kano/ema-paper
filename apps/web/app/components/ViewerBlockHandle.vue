<script setup lang="ts">
/**
 * A block's handle in the result viewer (docs/web.md § Editing, *The block handle and its
 * menu*): the first child of its `.block`, drawn only where it can be used now — over the
 * block the pointer is on or focus is in, a block whose menu is open, selected or moving.
 *
 * Its own component so that what moves the handle — the pointer crossing blocks, focus moving
 * — re-renders the handles whose state flips, not the whole document. The viewer's scoped
 * styles reach it: a component's root carries its parent's scope.
 */
import type { Ref } from 'vue'
import type { ResultBlock } from '~/lib/markdown'

/** What the viewer shares with every handle: one object for the viewer's life. */
export interface BlockHandleState {
  editable: Readonly<Ref<boolean>>
  /** The block under the pointer and the block holding focus, by key. */
  hovered: Readonly<Ref<string | null>>
  focused: Readonly<Ref<string | null>>
  /** The handle whose menu is open, drawn pressed while it is, and whether a menu is open. */
  menuKey: Readonly<Ref<string | null>>
  menuOpen: Readonly<Ref<boolean>>
  /** The selected blocks and the blocks a drag carries, by key. */
  selected: Readonly<Ref<ReadonlySet<string>>>
  moving: Readonly<Ref<ReadonlySet<string>>>
  /** The handle's accessible name for its block. */
  label: (block: ResultBlock) => string
}

const props = defineProps<{
  blockKey: string
  block: ResultBlock
  state: BlockHandleState
}>()

const emit = defineEmits<{
  press: [event: MouseEvent, key: string]
  'drag-start': [event: DragEvent, key: string]
  'drag-end': []
}>()

const shown = computed(() => {
  const { editable, hovered, focused, menuKey, selected, moving } = props.state
  const key = props.blockKey
  return (
    editable.value &&
    (key === hovered.value ||
      key === focused.value ||
      key === menuKey.value ||
      selected.value.has(key) ||
      moving.value.has(key))
  )
})
const on = computed(() => props.state.menuKey.value === props.blockKey)
</script>

<template>
  <!-- A div, not a button: Firefox starts no drag from a button. -->
  <div
    v-if="shown"
    role="button"
    class="block-handle"
    :class="{ on }"
    data-chrome
    tabindex="-1"
    draggable="true"
    :aria-label="state.label(block)"
    aria-haspopup="menu"
    :aria-expanded="on && state.menuOpen.value ? 'true' : 'false'"
    @pointerdown.stop
    @mousedown.stop
    @mouseup.stop
    @click.stop="emit('press', $event, blockKey)"
    @dragstart="emit('drag-start', $event, blockKey)"
    @dragend="emit('drag-end')"
  >
    <UiIcon name="grip-vertical" />
  </div>
</template>
