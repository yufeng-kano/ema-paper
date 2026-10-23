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
    <div class="region">
      <h2 class="heading">{{ t('connect.mcpTitle') }}</h2>
      <div class="endpoint">
        <span class="endpoint-url mono">{{ mcpUrl }}</span>
        <UiCopyButton :text="mcpUrl" variant="secondary" size="sm" labelled />
      </div>

      <p class="eyebrow steps-label">{{ t('connect.howTitle') }}</p>
      <ol class="steps">
        <li v-for="(step, index) in STEPS" :key="step" class="step">
          <span class="step-number tabular">{{ String(index + 1).padStart(2, '0') }}</span>
          <p class="step-body">{{ t(step) }}</p>
        </li>
      </ol>
    </div>
  </SettingsSection>
</template>

<style scoped>
.region {
  padding-top: var(--space-8);
}

.heading {
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* Rule-bounded rather than boxed — the same treatment every value in this design gets. */
.endpoint {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-top: var(--space-4);
  padding: var(--space-4) 0;
  border-top: 1px solid var(--hair);
  border-bottom: 1px solid var(--hair);
}

.endpoint-url {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-base);
  color: var(--ink);
}

.steps-label {
  margin-top: var(--space-9);
}

.steps {
  list-style: none;
  margin: var(--space-4) 0 0;
  padding: 0;
}

.step {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr);
  gap: var(--space-4);
  padding: var(--space-4) 0;
  border-top: 1px solid var(--hair);
}

.step:last-child {
  border-bottom: 1px solid var(--hair);
}

/* The step number is the only accent on this page: it is the sequence, which is the data
   the page carries. */
.step-number {
  font-family: var(--font-display);
  font-size: var(--display-sm);
  font-weight: var(--weight-semibold);
  color: var(--accent);
}

.step-body {
  max-width: 60ch;
  color: var(--muted);
  line-height: 1.7;
}
</style>
