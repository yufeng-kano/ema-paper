<script setup lang="ts">
/**
 * One signed-in screen: a toolbar row, the large title, and the content under them.
 *
 * The toolbar's controls float on the window — a segmented control at the left where the
 * screen has one (`toolbar-start`), the screen's actions at the right (`toolbar`) — and the
 * large title (`title`, usually `UiPageHeader`) sits under them. Neither scrolls: the content
 * does, on its own, so a long list never drags the title off the top of the window
 * (docs/web.md § Design system, *The frame*).
 *
 * `min-width: 0` and `min-height: 0` on the tracks are load-bearing: a grid track defaults
 * to `auto`, and one table's own minimum width would otherwise widen the screen past the
 * viewport rather than scrolling inside its panel.
 */
defineSlots<{
  default: () => unknown
  /** The large title and what is beside it. */
  title?: () => unknown
  /** Left of the toolbar row: a segmented control, or nothing. */
  'toolbar-start'?: () => unknown
  /** Right of the toolbar row: the screen's actions. */
  toolbar?: () => unknown
}>()
</script>

<template>
  <div class="screen">
    <div v-if="$slots.toolbar || $slots['toolbar-start']" class="toolbar">
      <div class="toolbar-start">
        <slot name="toolbar-start" />
      </div>
      <div class="toolbar-end">
        <slot name="toolbar" />
      </div>
    </div>
    <div v-if="$slots.title" class="head">
      <slot name="title" />
    </div>
    <div class="body">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.screen {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto auto minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
  background: var(--paper);
}

/* 14px from the window's top and 16px from its right edge, as the sidebar is inset: the two
   floating things share one margin. */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
  min-height: var(--control-height-lg);
  padding: 14px var(--space-4) 0 var(--space-6);
}

.toolbar-start,
.toolbar-end {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}

.toolbar-end {
  margin-left: auto;
}

/* The title sits over the list's text, not over its rounded rows: set in by a cell's own
   padding. */
.head {
  min-width: 0;
  padding: var(--space-6) calc(var(--space-8) + var(--cell-x)) var(--space-4) calc(var(--space-6) + var(--cell-x));
}

.screen > .toolbar + .head {
  padding-top: var(--space-5);
}

/* The panel inside sets no gutter of its own: this is the gutter, shared with the title. */
.body {
  --panel-x: 0px;

  display: grid;
  grid-template-rows: minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
  padding: 0 var(--space-8) 0 var(--space-6);
}

/* One scroll axis below the breakpoint: the content region around this scrolls as a whole,
   so the rows must not be bounded any more. */
@media (max-width: 900px) {
  .screen,
  .body {
    display: block;
  }

  .toolbar {
    padding: var(--space-3) var(--space-4) 0;
  }

  .head {
    padding: var(--space-4) calc(var(--space-4) + var(--cell-x)) var(--space-3);
  }

  .body {
    padding: 0 var(--space-2);
  }
}
</style>
