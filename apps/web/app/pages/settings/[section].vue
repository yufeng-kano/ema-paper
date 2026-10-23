<script setup lang="ts">
/**
 * A deep link to a settings section: `/settings/agent`, where the Kano Proxy connect flow
 * lands, and the old `/agents`, `/keys`, `/connect` and `/dashboard` paths redirect
 * (`routeRules`). The dialog itself opens over a screen, so this page only forwards to the
 * file library with `?settings=<section>`, keeping the rest of the query (the connect
 * outcome, `?kano=`) — docs/web.md § Pages, *The settings dialog*.
 */
import { SETTINGS_QUERY } from '~/lib/settings'

definePageMeta({ layout: false })

const route = useRoute()
const localePath = useLocalePath()
const raw = route.params.section
const section = Array.isArray(raw) ? raw[0] : raw

await navigateTo(
  { path: localePath('/files'), query: { ...route.query, [SETTINGS_QUERY]: section } },
  { replace: true },
)
</script>

<template>
  <div />
</template>
