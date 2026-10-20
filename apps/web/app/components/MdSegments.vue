<script setup lang="ts">
/**
 * One run of inline segments, rendered — recursive, because emphasis nests
 * (`_**Abstract**_` is an `em` holding a `strong`).
 *
 * Every styled element carries its exact source: `data-tex` on a math span, `data-src` on
 * strong/em/sup/sub/code/br — how copy-as-markdown hands back the markers the document
 * actually carries rather than the drawn glyphs (docs/web.md § Result viewer).
 *
 * An answer's section link (only with `cite`) is a chip — a locate glyph and the heading —
 * whose click the agent panel takes from its thread; a link whose heading `cite` says the
 * document no longer has reads as the heading's plain text. Both keep the link in
 * `data-src`, so copying or replying to the answer keeps its markdown (docs/web.md § The
 * agent panel, *Section links*).
 *
 * Math is typeset by KaTeX; `v-html` is safe here because KaTeX escapes its input and
 * `trust` is off (app/lib/math.ts). TeX KaTeX cannot parse renders as its `$…$` source.
 */
import type { CiteResolver, InlineSegment } from '~/lib/markdown'
import { renderTexHtml } from '~/lib/math'

const props = defineProps<{ segments: InlineSegment[]; cite?: CiteResolver }>()

const { t } = useI18n()

/** Each segment with its KaTeX HTML rendered once; `html: null` is the parse-failure
 *  fallback to source characters. A link is resolved as it renders, so one whose heading
 *  comes back (or goes) is redrawn with the document. */
const rendered = computed(() =>
  props.segments.map((segment) => ({
    segment,
    html: segment.kind === 'math' ? renderTexHtml(segment.tex, false) : null,
  })),
)
</script>

<template>
  <template v-for="({ segment, html }, index) in rendered" :key="index">
    <!-- eslint-disable vue/no-v-html -- KaTeX escapes its input; `trust` is off -->
    <span
      v-if="segment.kind === 'math' && html !== null"
      class="md-inline-math"
      :data-tex="`$${segment.tex}$`"
      v-html="html"
    />
    <!-- eslint-enable vue/no-v-html -->
    <span
      v-else-if="segment.kind === 'math'"
      class="md-inline-math"
      :data-tex="`$${segment.tex}$`"
    >${{ segment.tex }}$</span>
    <strong v-else-if="segment.kind === 'strong'" :data-src="segment.src">
      <MdSegments :segments="segment.segments" :cite="cite" />
    </strong>
    <em v-else-if="segment.kind === 'em'" :data-src="segment.src">
      <MdSegments :segments="segment.segments" :cite="cite" />
    </em>
    <sup v-else-if="segment.kind === 'sup'" class="md-sup" :data-src="segment.src">
      <MdSegments :segments="segment.segments" :cite="cite" />
    </sup>
    <sub v-else-if="segment.kind === 'sub'" class="md-sub" :data-src="segment.src">
      <MdSegments :segments="segment.segments" :cite="cite" />
    </sub>
    <code v-else-if="segment.kind === 'code'" class="md-inline-code" :data-src="segment.src">{{
      segment.text
    }}</code>
    <br v-else-if="segment.kind === 'br'" :data-src="segment.src">
    <button
      v-else-if="segment.kind === 'cite' && cite?.(segment.label, segment.anchor)"
      type="button"
      class="md-cite"
      :data-cite-label="segment.label"
      :data-cite-anchor="segment.anchor"
      :data-src="segment.src"
      :aria-label="t('chat.context.cite', { heading: segment.label })"
    >
      <UiIcon name="locate" />
      <span class="md-cite-label"><MdSegments :segments="segment.segments" /></span>
    </button>
    <span v-else-if="segment.kind === 'cite'" class="md-cite-text" :data-src="segment.src">
      <MdSegments :segments="segment.segments" />
    </span>
    <template v-else-if="segment.kind === 'text'">{{ segment.text }}</template>
  </template>
</template>

<style scoped>
/* A section link: a small chip in the line, on the rail surface — the heading, after a
   locate glyph (docs/web.md § The agent panel, *Section links*). */
.md-cite {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  max-width: 100%;
  height: 20px;
  margin-inline: 2px;
  padding: 0 6px;
  border: 0;
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1;
  vertical-align: 1px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.md-cite:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.md-cite :deep(svg) {
  flex: none;
  width: 12px;
  height: 12px;
}

/* A heading longer than the column is cut, not wrapped: the chip stays one line. */
.md-cite-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
