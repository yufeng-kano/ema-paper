<script setup lang="ts">
/**
 * One display-math block, typeset by KaTeX — `\begin{cases}` reads as the braced system
 * it is, not as source code (docs/web.md § Result viewer).
 *
 * `data-md` carries the block's `$$…$$` source, which is what copy-as-markdown hands
 * back. TeX KaTeX cannot parse renders as a verbatim `<pre class="md-math">` — a wrong
 * formula is worse than a visible one — which copy serializes as `$$…$$` (app/lib/copy.ts).
 * Both forms are styled by markdown.css, so they size with the surface they sit on.
 */
import { renderTexHtml } from '~/lib/math'

const props = defineProps<{ tex: string }>()

const html = computed(() => renderTexHtml(props.tex, true))
const source = computed(() => `$$\n${props.tex}\n$$`)

/** Identifies this *occurrence*: a document can repeat a formula, and the copy handler's
 *  partial-selection set must not tar every twin with one boundary cut (docs/web.md). */
const blockId = useId()
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- KaTeX escapes its input; `trust` is off -->
  <div
    v-if="html !== null"
    class="md-math-block"
    :data-md="source"
    :data-md-id="blockId"
    v-html="html"
  />
  <!-- eslint-enable vue/no-v-html -->
  <pre v-else class="md-math">{{ tex }}</pre>
</template>
