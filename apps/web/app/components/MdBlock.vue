<script setup lang="ts">
/**
 * One parsed block, rendered (docs/web.md § Result viewer). Shared by the document pane
 * and the notes column, which are the same markdown drawn at two sizes.
 *
 * Every structural element keeps the class and `data-*` attribute copy-as-markdown and
 * find-in-document read: `md-h2`/`md-h3` with `data-level`, `md-p`, `md-list`, `md-table`,
 * `md-toc` with the leader's `data-src`, `md-figure` with its `data-md`, and the
 * KaTeX-typeset `MdMath` with its own. Moving the markup here changed none of that.
 *
 * A figure's crop is the caller's: `figureUrl` is the image to load, or null for the
 * dashed placeholder (no figure route, or a crop that already failed), and a load failure
 * is reported back so the caller can remember it per snapshot.
 */
import type { Bbox, ResultBlock } from '~/lib/markdown'

const props = defineProps<{
  block: ResultBlock
  /** The page the block came from — what addresses its figure crop. */
  page: number
  figureUrl?: string | null
}>()

const emit = defineEmits<{ 'figure-error': [page: number, bbox: Bbox] }>()

const { t } = useI18n()

/** The placeholder + caption this figure was rendered from — what a copy hands back. */
const figureSource = computed(() => {
  const block = props.block
  if (block.kind !== 'fig') {
    return ''
  }
  const placeholder = `![${block.id}](sightread://p${props.page}/${block.bbox.join(',')})`
  return block.caption ? `${placeholder}\n${block.caption}` : placeholder
})
</script>

<template>
  <h3 v-if="block.kind === 'h2'" class="md-h2" :data-level="block.level">
    <MdInline :text="block.text" />
  </h3>
  <h4 v-else-if="block.kind === 'h3'" class="md-h3" :data-level="block.level">
    <MdInline :text="block.text" />
  </h4>
  <p v-else-if="block.kind === 'p'" class="md-p"><MdInline :text="block.text" /></p>

  <!-- KaTeX-typeset, falling back to the verbatim source when it cannot parse. -->
  <MdMath v-else-if="block.kind === 'math'" :tex="block.text" />

  <pre
    v-else-if="block.kind === 'code'"
    class="md-math"
  ><code :class="block.lang ? `language-${block.lang}` : undefined">{{ block.text }}</code></pre>

  <component
    :is="block.ordered ? 'ol' : 'ul'"
    v-else-if="block.kind === 'list'"
    class="md-list"
    :start="block.start"
  >
    <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
      <MdInline :text="item" />
    </li>
  </component>

  <!-- Its own scroll axis: a table wider than the reading measure slides in place instead
       of stretching the page. -->
  <div v-else-if="block.kind === 'table'" class="md-table-wrap">
    <table class="md-table">
      <thead>
        <tr>
          <th v-for="(cell, column) in block.header" :key="column" scope="col">
            <MdInline :text="cell" />
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
          <td v-for="(cell, column) in row" :key="column">
            <MdInline :text="cell" />
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- A contents page, one row per source line. The leader is drawn, so it fills the
       column instead of wrapping; its `data-src` is the dots the line carried. -->
  <div v-else-if="block.kind === 'toc'" class="md-toc">
    <p v-for="(entry, entryIndex) in block.entries" :key="entryIndex" class="md-toc-row">
      <span class="md-toc-title"><MdInline :text="entry.title" /></span>
      <template v-if="entry.page !== null">
        <span
          class="md-toc-leader"
          :class="{ dotted: entry.leader.includes('.') }"
          :data-src="entry.leader"
          aria-hidden="true"
        />
        <span class="md-toc-page"><MdInline :text="entry.page" /></span>
      </template>
    </p>
  </div>

  <!-- The stored crop when one exists; the dashed frame when it does not. `data-md` is
       what a copy hands back. -->
  <figure v-else-if="block.kind === 'fig'" class="md-figure" :data-md="figureSource">
    <img
      v-if="figureUrl"
      class="figure-image"
      :src="figureUrl"
      :alt="block.caption || block.id"
      loading="lazy"
      @error="emit('figure-error', page, block.bbox)"
    >
    <div v-else class="figure-frame">
      <UiIcon name="scan-text" />
      <span>{{ t('viewer.figurePending') }}</span>
    </div>
    <!-- Only the caption the page carried: the generated `figN` name and the bbox are how
         a crop is addressed, not something a reader was asked to read. -->
    <figcaption v-if="block.caption" class="figure-caption">
      <MdInline :text="block.caption" />
    </figcaption>
  </figure>
</template>

<style scoped>
/*
 * The two reading sizes come from the surface the block sits on: the document pane sets
 * `--block-text` / `--block-text-sm` to the read scale, the notes column to the UI scale.
 */
.md-h2 {
  font-size: var(--block-heading, var(--display-lg));
  letter-spacing: var(--tracking-heading);
}

.md-h3 {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--block-subheading, var(--display-sm));
  font-weight: var(--weight-semibold);
  line-height: 1.3;
}

.md-p {
  color: var(--ink-soft);
  font-size: var(--block-text, var(--text-read));
  line-height: 1.75;
  text-wrap: pretty;
}

/* A formula is read, not scanned, so it keeps the body's colour and size — but it scrolls
   sideways rather than wrapping, because a re-wrapped equation is a different equation.
   Sideways only: `overflow-y` is stated because CSS computes a `visible` axis to `auto` the
   moment the other one is not, and an equation behind a vertical scrollbar is half an
   equation. */
.md-math {
  margin: 0;
  overflow-x: auto;
  overflow-y: hidden;
  color: var(--ink-soft);
  font-family: var(--mono);
  font-size: var(--block-text-sm, var(--text-read-sm));
  line-height: 1.7;
  white-space: pre;
}

/* A contents row: title left, page right, the leader stretched between them on the text's
   baseline. A long title wraps inside its own span and the number stays on the last line. */
.md-toc {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  color: var(--ink-soft);
  font-size: var(--block-text, var(--text-read));
  line-height: 1.75;
}

.md-toc-row {
  display: flex;
  align-items: last baseline;
  margin: 0;
}

.md-toc-leader {
  flex: 1;
  min-width: var(--space-5);
  margin-inline: var(--space-2);
}

.md-toc-leader.dotted {
  border-bottom: 1px dotted var(--faint);
}

.md-toc-page {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* Markers stay outside the measure, so the text edge lines up with the paragraphs above
   and below it rather than being indented away from them. */
.md-list {
  margin: 0;
  padding-left: var(--space-5);
  color: var(--ink-soft);
  font-size: var(--block-text, var(--text-read));
  line-height: 1.75;
}

.md-list li + li {
  margin-top: var(--space-2);
}

/* The table's own scroll axis: wider than the reading measure, it slides in place rather
   than stretching the page — the prose column's width is fixed (docs/web.md). */
.md-table-wrap {
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}

.md-table {
  width: max-content;
  min-width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: var(--block-text-sm, var(--text-read-sm));
  font-variant-numeric: tabular-nums;
}

/* Long prose cells still wrap; the cap is what keeps one wordy cell from dragging the
   whole table out to a single unreadable line. */
.md-table th,
.md-table td {
  max-width: 36ch;
}

.md-table th {
  padding: var(--cell-y-tight) var(--space-4) var(--cell-y-tight) 0;
  text-align: left;
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
  border-bottom: 1px solid var(--line-soft);
}

.md-table td {
  padding: var(--cell-y-tight) var(--space-4) var(--cell-y-tight) 0;
  border-bottom: 1px solid var(--line-soft);
  color: var(--ink-soft);
}

.md-table tbody tr:last-child td {
  border-bottom: none;
}

/* A figure is centered in the column, caption and all: a crop is narrower than the measure
   far more often than not, and left-aligning it hangs it off an edge the text does not
   share. */
.md-figure {
  margin: 0;
  text-align: center;
}

/* The stored crop, at the width the prose gives it and never more. */
.figure-image {
  display: block;
  margin-inline: auto;
  max-width: 100%;
  border: 1px solid var(--line-soft);
  background: var(--paper);
}

/* Dashed, because it is a frame around something that is not there yet. */
.figure-frame {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  height: 210px;
  border: 1px dashed var(--edge);
  background: var(--rail);
  color: var(--faint);
  font-size: var(--text-xs);
}

.figure-frame :deep(svg) {
  width: 22px;
  height: 22px;
}

/* Prose now that the coordinates have gone from beside it: it wraps to as many lines as
   the caption needs instead of being truncated to fit next to a readout. */
.figure-caption {
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--faint);
}
</style>
