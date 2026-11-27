<script setup lang="ts">
/**
 * `/pdf-probe` (docs/pdf-viewer.md § The device check): what this browser's WebGPU can do,
 * measured on the device that opens the page. It is linked from nowhere — it exists so the
 * PDF viewer's design can be checked on real phones and laptops before the renderer is built,
 * and it goes when the renderer is the default.
 *
 * The page only draws and lists: the checks are `app/lib/pdf/probe-gpu.ts`, and what they
 * record and how it is copied is `app/lib/pdf/probe.ts`.
 */
import {
  PROBE_COLORS,
  PROBE_SURFACES,
  errorText,
  reportText,
  type ProbeReport,
  type ProbeStatus,
  type ProbeSurface,
} from '~/lib/pdf/probe'
import type { ProbeRun } from '~/lib/pdf/probe-gpu'

definePageMeta({ middleware: 'authed' })

const { t } = useI18n()

const once = useTemplateRef<HTMLCanvasElement[]>('once')
const single = useTemplateRef<HTMLCanvasElement[]>('single')
/**
 * How many runs have started. Every canvas is keyed by it, so each run draws on canvases no
 * earlier run configured, drew or handed to a worker.
 */
const runs = ref(0)
const state = ref<'idle' | 'running' | 'done' | 'failed'>('idle')
const report = ref<ProbeReport | null>(null)
/** Why a run could not finish, in the browser's own words. */
const failure = ref('')
let run: ProbeRun | null = null
let left = false

const TONES: Record<ProbeStatus, 'ok' | 'danger' | 'neutral'> = { pass: 'ok', fail: 'danger', skipped: 'neutral' }

const summary = computed(() => {
  if (state.value === 'running') {
    return t('pdfProbe.running')
  }
  if (state.value === 'failed') {
    return t('pdfProbe.failed')
  }
  if (!report.value) {
    return t('pdfProbe.notRun')
  }
  const { checks } = report.value
  return t('pdfProbe.summary', { passed: checks.filter((check) => check.status === 'pass').length, total: checks.length })
})

const text = computed(() => (report.value ? reportText(report.value) : ''))

async function start() {
  if (state.value === 'running') {
    return
  }
  state.value = 'running'
  // The last run's answers go before the canvases are drawn again: nothing stale to copy.
  report.value = null
  failure.value = ''
  run?.dispose()
  run = null
  runs.value += 1
  try {
    await nextTick()
    // A list of refs comes in no promised order, so each canvas says which it is.
    const fives = [...(once.value ?? [])].sort((a, b) => Number(a.dataset.index) - Number(b.dataset.index))
    const singles = single.value ?? []
    if (fives.length !== PROBE_COLORS.length || singles.length !== PROBE_SURFACES.length) {
      // The canvases are not on the page: nothing to run on, and no words of the browser's to show.
      state.value = 'failed'
      return
    }
    // Its own chunk: no other page asks for a GPU device.
    const { runProbe } = await import('~/lib/pdf/probe-gpu')
    const bySurface = Object.fromEntries(
      singles.map((canvas) => [canvas.dataset.surface, canvas]),
    ) as Record<ProbeSurface, HTMLCanvasElement>
    run = await runProbe({ once: fives, ...bySurface })
    if (left) {
      // The page went while the checks ran: nothing is left to show, so the device goes too.
      run.dispose()
      return
    }
    report.value = run.report
    state.value = 'done'
  } catch (error) {
    failure.value = errorText(error)
    state.value = 'failed'
  }
}

onBeforeUnmount(() => {
  left = true
  run?.dispose()
})
</script>

<template>
  <UiScreen>
    <template #title>
      <UiPageHeader :title="t('pdfProbe.title')">{{ summary }}</UiPageHeader>
    </template>

    <UiPanel>
      <div class="probe">
        <p class="intro">{{ t('pdfProbe.intro') }}</p>
        <div class="actions">
          <UiButton variant="primary" :loading="state === 'running'" @click="start">
            {{ state === 'idle' ? t('pdfProbe.run') : t('pdfProbe.runAgain') }}
          </UiButton>
          <UiCopyButton v-if="report" :text="text" size="sm" labelled :label="t('pdfProbe.copy')" />
        </div>
        <!-- The browser's own words, printed as they are. -->
        <p v-if="failure" class="note">{{ failure }}</p>

        <section class="group" :aria-label="t('pdfProbe.canvasesTitle')">
          <h2 class="group-title">{{ t('pdfProbe.canvasesTitle') }}</h2>
          <p class="note">{{ t('pdfProbe.canvasesNote') }}</p>
          <!-- Each way of drawing has canvases of its own; the dot under a canvas is the color it should show. -->
          <div class="surface">
            <span class="surface-name">{{ t('pdfProbe.surfaces.once') }}</span>
            <div class="swatches">
              <span v-for="(color, index) in PROBE_COLORS" :key="`once-${index}-${runs}`" class="pair">
                <canvas
                  ref="once"
                  class="swatch"
                  role="img"
                  :data-index="index"
                  :aria-label="t('pdfProbe.canvas', { n: index + 1 })"
                />
                <span class="expected" :style="{ background: `rgb(${color[0]} ${color[1]} ${color[2]})` }" />
              </span>
            </div>
          </div>
          <div v-for="(surface, index) in PROBE_SURFACES" :key="`${surface}-${runs}`" class="surface">
            <span class="surface-name">{{ t(`pdfProbe.surfaces.${surface}`) }}</span>
            <span class="pair">
              <canvas
                ref="single"
                class="swatch"
                role="img"
                :data-surface="surface"
                :aria-label="t(`pdfProbe.surfaces.${surface}`)"
              />
              <span
                class="expected"
                :style="{ background: `rgb(${PROBE_COLORS[index]![0]} ${PROBE_COLORS[index]![1]} ${PROBE_COLORS[index]![2]})` }"
              />
            </span>
          </div>
        </section>

        <section v-if="report" class="group" :aria-label="t('pdfProbe.checksTitle')">
          <h2 class="group-title">{{ t('pdfProbe.checksTitle') }}</h2>
          <ul class="checks">
            <li v-for="check in report.checks" :key="check.id" class="check">
              <span class="check-name">{{ t(`pdfProbe.checks.${check.id}`) }}</span>
              <UiStatusDot :tone="TONES[check.status]" :label="t(`pdfProbe.status.${check.status}`)" />
              <span v-if="check.ms !== undefined" class="check-time">
                {{ t('pdfProbe.duration', { ms: Math.round(check.ms) }) }}
              </span>
              <span v-for="note in check.notes" :key="note.code" class="check-detail">
                {{ t(`pdfProbe.notes.${note.code}`, note.params ?? {}) }}
              </span>
              <!-- The browser's own words, printed as they are. -->
              <span v-if="check.detail" class="check-detail">{{ check.detail }}</span>
            </li>
          </ul>
        </section>

        <section v-if="report" class="group" :aria-label="t('pdfProbe.deviceTitle')">
          <h2 class="group-title">{{ t('pdfProbe.deviceTitle') }}</h2>
          <!-- What Copy copies: facts under the browser's own names, so not translated. -->
          <pre class="report">{{ text }}</pre>
        </section>
      </div>
    </UiPanel>
  </UiScreen>
</template>

<style scoped>
/* Set in as the title is, so the text starts on the same line down the screen. */
.probe {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
  max-width: 720px;
  padding: 0 calc(var(--space-8) + var(--cell-x)) var(--space-8) calc(var(--space-6) + var(--cell-x));
}

.intro,
.note {
  margin: 0;
  color: var(--muted);
}

.note {
  font-size: var(--text-xs);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.group {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}

.group-title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
}

.swatches {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.surface {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-4);
}

.surface-name {
  flex: 1 1 220px;
  min-width: 0;
}

/* A canvas over the dot of the color it should show. */
.pair {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-1);
}

.expected {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}

/* 44px on the screen whatever the backing size: the check draws 64 device pixels a side. */
.swatch {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-sm);
  background: var(--fill);
}

.checks {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.check {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-4);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--line-soft);
}

.check-name {
  flex: 1 1 220px;
  min-width: 0;
}

.check-time {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.check-detail {
  flex-basis: 100%;
  color: var(--muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.report {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  font-size: var(--text-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
}
</style>
