<script setup lang="ts">
/**
 * One run of inline segments, rendered — recursive, because emphasis nests
 * (`_**Abstract**_` is an `em` holding a `strong`).
 *
 * Every styled element carries its exact source: `data-tex` on a math span, `data-src` on
 * strong/em/sup/sub/code/br — how copy-as-markdown hands back the markers the document
 * actually carries rather than the drawn glyphs (docs/web.md § Result viewer).
 *
 * Math is typeset by KaTeX; `v-html` is safe here because KaTeX escapes its input and
 * `trust` is off (app/lib/math.ts). TeX KaTeX cannot parse renders as its `$…$` source.
 */
import type { InlineSegment } from '~/lib/markdown'
import { renderTexHtml } from '~/lib/math'

const props = defineProps<{ segments: InlineSegment[] }>()

/** Each segment with its KaTeX HTML rendered once; `html: null` is the parse-failure
 *  fallback to source characters. */
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
      <MdSegments :segments="segment.segments" />
    </strong>
    <em v-else-if="segment.kind === 'em'" :data-src="segment.src">
      <MdSegments :segments="segment.segments" />
    </em>
    <sup v-else-if="segment.kind === 'sup'" class="md-sup" :data-src="segment.src">
      <MdSegments :segments="segment.segments" />
    </sup>
    <sub v-else-if="segment.kind === 'sub'" class="md-sub" :data-src="segment.src">
      <MdSegments :segments="segment.segments" />
    </sub>
    <code v-else-if="segment.kind === 'code'" class="md-inline-code" :data-src="segment.src">{{
      segment.text
    }}</code>
    <br v-else-if="segment.kind === 'br'" :data-src="segment.src">
    <template v-else-if="segment.kind === 'text'">{{ segment.text }}</template>
  </template>
</template>
