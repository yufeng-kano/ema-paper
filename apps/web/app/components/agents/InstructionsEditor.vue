<script setup lang="ts">
/**
 * An agent's instructions box (docs/web.md § Pages): the notes' in-place markdown editor in
 * a field's hairline box, as tall as its text up to a cap past which the text scrolls
 * inside — long instructions never push the controls under it down the page. The chat
 * agent's Instructions and the check agent's both write in it.
 *
 * It opens on `source` and reports every edit as `change`; `reset()` puts `source` back,
 * which Cancel needs since the editor follows `source` only when it changes, and Cancel is
 * often a return to the same saved text.
 */
defineProps<{
  source: string
  placeholder: string
  /** The editor's accessible name. */
  label: string
  /** The ids that describe it: the section's note, the count. */
  describedBy: string
  /** Off while a save is in flight: a keystroke then would land in a draft the answer is
   *  about to reload under it. */
  editable: boolean
}>()

const emit = defineEmits<{ change: [text: string] }>()

const editorKey = ref(0)
const editor = ref<{ focus: () => void } | null>(null)

/** A click on the box below the last line still writes, as in the notes column. */
function onClick(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.rich')) {
    editor.value?.focus()
  }
}

defineExpose({
  reset() {
    editorKey.value += 1
  },
})
</script>

<template>
  <div class="editor md-side" @click="onClick">
    <RichEditor
      :key="editorKey"
      ref="editor"
      variant="notes"
      :source="source"
      :placeholder="placeholder"
      :label="label"
      :described-by="describedBy"
      :editable="editable"
      @change="emit('change', $event)"
    />
  </div>
</template>

<style scoped>
/* The column's sizes, the headings as plain run-in labels — a rule above a heading would
   only cut the box in two. Room for a few lines when it is empty. */
.editor {
  --block-heading: var(--display-2xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);
  --rich-gap: var(--space-3);

  min-height: 7rem;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  cursor: text;
}

/* The text scrolls, not the box: the slash menu and the TeX field, which the editor draws
   beside its text, stay whole past the box's edge rather than cut by it. */
.editor :deep(.rich-doc) {
  max-height: min(20rem, 50vh);
  overflow-y: auto;
  padding: var(--space-4);
}

/* The editable surface draws no ring of its own, so the box carries the app's one. */
.editor:focus-within {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.editor :deep(.md-h2:not([data-level='1'])) {
  padding-top: 0;
  border-top: none;
}

.editor :deep(.md-p),
.editor :deep(.md-list) {
  line-height: 1.7;
}
</style>
