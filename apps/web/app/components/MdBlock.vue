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
 * is reported back so the caller can remember it per snapshot. A figure is addressed by its
 * own page — the one its placeholder names — whatever page the block now sits in, since a
 * move can carry it across a page marker. An image the reader inserted is drawn the same way,
 * its address the caller's too (the document's images route), and copies back as its line.
 * `figureNote` is the caller's too: the note under a figure the figure check could not
 * confirm, drawn as chrome (`data-chrome`), so copy and find pass over it.
 *
 * A quote draws its inner blocks through this same component, one level deep: what the
 * parser reads inside a quote never holds another quote or a figure.
 *
 * `cite` is the agent panel's alone: given, an answer's links to the document's sections
 * are read and drawn as chips — those whose heading it says the document still has
 * (docs/web.md § The agent panel, *Section links*). So is `links`: given, an answer's links
 * to the web are drawn as links (*Links*).
 */
import type { CiteResolver, ResultBlock } from '~/lib/markdown'

const props = defineProps<{
  block: ResultBlock
  figureUrl?: string | null
  figureNote?: string | null
  cite?: CiteResolver
  links?: boolean
}>()

const emit = defineEmits<{ 'figure-error': [] }>()

const { t } = useI18n()

/** The placeholder + caption this figure was rendered from — what a copy hands back. */
const figureSource = computed(() => {
  const block = props.block
  if (block.kind !== 'fig') {
    return ''
  }
  const placeholder = `![${block.id}](sightread://p${block.page}/${block.bbox.join(',')})`
  return block.caption ? `${placeholder}\n${block.caption}` : placeholder
})
</script>

<template>
  <h3 v-if="block.kind === 'h2'" class="md-h2" :data-level="block.level">
    <MdInline :text="block.text" :cite="cite" :links="links" />
  </h3>
  <h4 v-else-if="block.kind === 'h3'" class="md-h3" :data-level="block.level">
    <MdInline :text="block.text" :cite="cite" :links="links" />
  </h4>
  <p v-else-if="block.kind === 'p'" class="md-p"><MdInline :text="block.text" :cite="cite" :links="links" /></p>

  <!-- KaTeX-typeset, falling back to the verbatim source when it cannot parse. -->
  <MdMath v-else-if="block.kind === 'math'" :tex="block.text" />

  <!-- A page's footnote rule; copy hands its source back. -->
  <hr v-else-if="block.kind === 'hr'" class="md-hr" :data-md="block.src">

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
      <MdInline :text="item" :cite="cite" :links="links" />
    </li>
  </component>

  <!-- Its own scroll axis: a table wider than the reading measure slides in place instead
       of stretching the page. -->
  <div v-else-if="block.kind === 'table'" class="md-table-wrap">
    <table class="md-table">
      <thead>
        <tr>
          <th v-for="(cell, column) in block.header" :key="column" scope="col">
            <MdInline :text="cell" :cite="cite" :links="links" />
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
          <td v-for="(cell, column) in row" :key="column">
            <MdInline :text="cell" :cite="cite" :links="links" />
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- A contents page, one row per source line. The leader is drawn, so it fills the
       column instead of wrapping; its `data-src` is the dots the line carried. -->
  <div v-else-if="block.kind === 'toc'" class="md-toc">
    <p v-for="(entry, entryIndex) in block.entries" :key="entryIndex" class="md-toc-row">
      <span class="md-toc-title"><MdInline :text="entry.title" :cite="cite" :links="links" /></span>
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

  <blockquote v-else-if="block.kind === 'quote'" class="md-quote">
    <MdBlock v-for="(inner, innerIndex) in block.blocks" :key="innerIndex" :block="inner" :cite="cite" :links="links" />
  </blockquote>

  <!-- The stored crop when one exists; the dashed frame when it does not. `data-md` is
       what a copy hands back. -->
  <figure v-else-if="block.kind === 'fig'" class="md-figure" :data-md="figureSource">
    <img
      v-if="figureUrl"
      class="figure-image"
      :src="figureUrl"
      :alt="block.caption || block.id"
      loading="lazy"
      @error="emit('figure-error')"
    >
    <div v-else class="figure-frame">
      <UiIcon name="scan-text" />
      <span>{{ t('viewer.figurePending') }}</span>
    </div>
    <p v-if="figureNote" class="figure-note" data-chrome>{{ figureNote }}</p>
    <!-- Only the caption the page carried: the generated `figN` name and the bbox are how
         a crop is addressed, not something a reader was asked to read. -->
    <figcaption v-if="block.caption" class="figure-caption">
      <MdInline :text="block.caption" :cite="cite" :links="links" />
    </figcaption>
  </figure>

  <!-- An inserted image: the reader's own picture, its line what a copy hands back. -->
  <figure v-else-if="block.kind === 'img'" class="md-figure" :data-md="block.src">
    <img
      v-if="figureUrl"
      class="figure-image"
      :src="figureUrl"
      :alt="block.caption || block.alt"
      loading="lazy"
      @error="emit('figure-error')"
    >
    <div v-else class="figure-frame">
      <UiIcon name="image" />
      <span>{{ t('viewer.imagePending') }}</span>
    </div>
    <figcaption v-if="block.caption" class="figure-caption">
      <MdInline :text="block.caption" :cite="cite" :links="links" />
    </figcaption>
  </figure>
</template>

<style scoped>
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

/* A label on the picture, not the document's text: filled rather than outlined, so it
   reads as a tag and not as a button, and set apart from the faint caption under it. */
.figure-note {
  display: inline-block;
  margin: var(--space-2) 0 0;
  padding: 0 var(--space-2);
  background: var(--rail-active);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.6;
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
