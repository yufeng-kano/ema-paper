<script setup lang="ts">
const { t } = useI18n()

// The connector URL is this deployment's own origin; Caddy routes /mcp to the API.
const mcpUrl = ref('https://<host>/mcp')
onMounted(() => {
  mcpUrl.value = `${window.location.origin}/mcp`
})

/** Numbered rather than bulleted: the order is the point — the URL, then the consent, then
 *  what Claude does with it. */
const STEPS = ['connect.step1', 'connect.step2', 'connect.step3'] as const
</script>

<template>
  <SettingsSection :eyebrow="t('connect.eyebrow')" :title="t('connect.headTitle')">
    <SettingsGroup :title="t('connect.mcpTitle')">
      <div class="endpoint">
        <span class="endpoint-url mono">{{ mcpUrl }}</span>
        <UiCopyButton :text="mcpUrl" variant="secondary" size="sm" labelled />
      </div>
    </SettingsGroup>

    <SettingsGroup :title="t('connect.howTitle')">
      <ol class="steps">
        <li v-for="(step, index) in STEPS" :key="step" class="step">
          <span class="step-number tabular" aria-hidden="true">{{ index + 1 }}</span>
          <p class="step-body">{{ t(step) }}</p>
        </li>
      </ol>
    </SettingsGroup>
  </SettingsSection>
</template>

<style scoped>
.endpoint {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-height: 56px;
  padding: var(--space-3) 0;
}

.endpoint-url {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  color: var(--ink);
}

.steps {
  list-style: none;
  margin: 0;
  padding: 0;
}

.step {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-4) 0;
}

.step + .step {
  border-top: 1px solid var(--line);
}

/* The sequence is the data this list carries: the number on a small accent disc. */
.step-number {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  background: var(--accent-tint);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.step-body {
  max-width: 60ch;
  color: var(--ink-soft);
  line-height: 1.6;
}
</style>
