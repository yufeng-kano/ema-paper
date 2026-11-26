<script setup lang="ts">
/**
 * A model picker's options inside the caller's `UiSelect`: the current choice when the
 * catalog does not list it, then the catalog in the order given. The caller keeps its own
 * leading option (*None*, *Follows the conversation*).
 */
import type { AgentModel } from '~/lib/api'
import { injectedModel, modelLabel } from '~/lib/models'

const props = defineProps<{
  /** The catalog; null while it loads or when it failed. */
  models: AgentModel[] | null
  current: string | null | undefined
}>()

const injected = computed(() => injectedModel(props.current, props.models))
</script>

<template>
  <option v-if="injected" :value="injected">{{ injected }}</option>
  <option v-for="model in models ?? []" :key="model.id" :value="model.id">{{ modelLabel(model) }}</option>
</template>
