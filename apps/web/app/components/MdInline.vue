<script setup lang="ts">
/**
 * One line of prose with its inline content rendered — `Bu$^{1,2}$` reads as typeset math
 * and `**96.5**` reads bold, not as source characters (docs/web.md § Result viewer).
 * The split lives in `parseInline`; the rendering, including the `data-tex`/`data-src`
 * sources copy-as-markdown reads back, lives in `MdSegments`. With `cite`, the line is an
 * answer's and its section links are read too.
 */
import { parseInline, type CiteResolver } from '~/lib/markdown'

const props = defineProps<{ text: string; cite?: CiteResolver }>()

const segments = computed(() => parseInline(props.text, props.cite ? { cites: true } : undefined))
</script>

<template>
  <MdSegments :segments="segments" :cite="cite" />
</template>
