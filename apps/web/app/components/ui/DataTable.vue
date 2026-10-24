<!--
  The one place table markup lives.

  What a page's own `<table>` reliably gets wrong, all handled here:

  1. It scrolls in place. The table carries a `min-width` and never wraps a cell, so a
     narrow region gives it a horizontal scrollbar inside its panel instead of squeezing
     columns until the row rhythm breaks.
  2. Sticky header — the column names stay while the body scrolls inside its panel, on an
     inset rule rather than a border (a sticky cell's border scrolls away from the cell).
  3. Alignment — numeric columns are right-aligned with tabular numerals, header and cells
     together, declared once per column rather than re-specified per cell.
  4. Rhythm — Finder's list: every row the same height, every cell set in from the row's
     rounded ends by the same padding, every other row on its own faint fill, so the eye
     follows a row across without a rule under each one.

  Cells render through a per-column slot named `cell-<key>`, falling back to the column's
  `value()`. A row can also carry attributes of its own through `rowAttrs` — what the file
  library needs to make rows draggable and folder rows droppable, without a second table
  living on that page.
-->
<script setup lang="ts" generic="Row">
import type { TableColumn } from '~/lib/table'

withDefaults(
  defineProps<{
    columns: TableColumn<Row>[]
    rows: Row[]
    rowKey: (row: Row) => string
    /** Accessible caption. Visually hidden — the panel header carries the visible title. */
    caption: string
    /** `tight` is the usage tables' denser row; `normal` is history, keys and files. */
    density?: 'normal' | 'tight'
    /**
     * Extra attributes for one row's `<tr>` — `class`, `draggable`, `onDragover` and the
     * rest. Bound with `v-bind`, so listeners go in as `onEventname` keys; `class` merges
     * with this component's own rather than replacing it.
     */
    rowAttrs?: (row: Row) => Record<string, unknown>
  }>(),
  { density: 'normal', rowAttrs: undefined },
)

defineSlots<Record<string, (props: { row: Row }) => unknown>>()
</script>

<template>
  <table class="table" :class="density">
    <caption class="sr-only">{{ caption }}</caption>
    <thead>
      <tr>
        <th
          v-for="column in columns"
          :key="column.key"
          scope="col"
          :class="{ numeric: column.numeric, end: column.align === 'end' }"
          :style="column.width ? { width: column.width } : undefined"
        >
          <span v-if="!column.header && column.srHeader" class="sr-only">{{ column.srHeader }}</span>
          <template v-else>{{ column.header }}</template>
        </th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="row in rows" :key="rowKey(row)" v-bind="rowAttrs?.(row)">
        <td
          v-for="column in columns"
          :key="column.key"
          :class="{ numeric: column.numeric, end: column.align === 'end' }"
        >
          <slot :name="`cell-${column.key}`" :row="row">{{ column.value?.(row) ?? '' }}</slot>
        </td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.table {
  width: 100%;
  /* The floor a row needs before its cells would have to wrap. Past it the panel body
     scrolls sideways — see the file comment. */
  min-width: var(--table-min);
  border-collapse: separate;
  border-spacing: 0;
  font-size: var(--text-sm);
  white-space: nowrap;
}

.table th {
  position: sticky;
  top: 0;
  z-index: 1;
  height: var(--control-height-sm);
  padding: 0 var(--cell-x);
  text-align: left;
  vertical-align: middle;
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--muted);
  /* Opaque, not translucent: rows scrolling under a semi-transparent header smear into the
     labels. Every list sits on paper. */
  background: var(--paper);
  /* An inset shadow, not `border-bottom`: a sticky cell's border scrolls away independently
     of the cell itself, leaving the header floating unruled. */
  box-shadow: inset 0 -1px 0 var(--line);
}

.table td {
  height: var(--row-height);
  padding: 0 var(--cell-x);
  vertical-align: middle;
  color: var(--ink);
}

.table.tight td {
  height: var(--row-height-tight);
}

/* A breath between the header's rule and the first row. */
.table tbody tr:first-child td {
  border-top: var(--space-1) solid transparent;
  background-clip: padding-box;
}

/* Every other row on a faint fill, its ends rounded, as Finder's list draws it. The parity
   sits in `:where()` so it adds no weight: a caller's row states (Files' selection, drop
   target) must win over the stripe on even rows too. */
.table tbody tr:where(:nth-child(even)) td {
  background: var(--row-alt);
}

.table td:first-child {
  border-radius: var(--radius-md) 0 0 var(--radius-md);
}

.table td:last-child {
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
}

.table td:only-child {
  border-radius: var(--radius-md);
}

/*
 * Column alignment, applied to the header and its cells as one. Written as `.table th` /
 * `.table td` rather than a bare `.numeric`, because `.table th`'s `text-align: left` sits
 * at a higher specificity and would otherwise win — leaving every numeric header
 * left-aligned over right-aligned figures.
 */
.table th.numeric,
.table td.numeric {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* A control column: the header sits over the control rather than at the far edge. */
.table th.end,
.table td.end {
  text-align: right;
}

/*
 * A link or any block a cell holds must be told to ellipsize. Left inline it produces a
 * second line box inside the cell, and rows then differ in height by a pixel or two down
 * the whole table.
 */
.table td :deep(.cell-block) {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
