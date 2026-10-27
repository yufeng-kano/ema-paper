<script setup lang="ts">
/**
 * The Usage page's daily cost (docs/admin.md § Pages, *Usage*): one line in the accent over a
 * faint wash of it, one point per UTC day, hairline gridlines at round dollar amounts
 * labelled at the left, a few dates under the axis and the last day's point marked. Drawn in
 * SVG here — no chart library (docs/web.md) — at the width it is given, measured, so the
 * labels keep their size; the geometry is `app/lib/chart.ts`'s.
 *
 * The pointer snaps to the nearest day: a hairline, that day's point, and over the chart its
 * date and cost with the model that cost most, the rest as *others*. To assistive technology
 * the chart is one image named by `label`; the table under it on the page is its data.
 */
import {
  areaPath,
  chartPoints,
  labelIndexes,
  linePath,
  nearestIndex,
  niceTicks,
  tickY,
  type ChartBox,
  type CostDay,
} from '~/lib/chart'
import { formatUsd, formatUtcDay } from '~/lib/admin'

const props = defineProps<{
  days: CostDay[]
  /** The chart's accessible name. */
  label: string
}>()

const { t, locale } = useI18n()

const HEIGHT = 200
/** Room for the dollar labels at the left, the dates under the axis, and the last point's
 *  ring at the right and the top. */
const INSET = { left: 56, right: 12, top: 12, bottom: 28 }

const frame = ref<HTMLElement | null>(null)
const width = ref(640)
let observer: ResizeObserver | undefined

onMounted(() => {
  const element = frame.value
  if (!element) {
    return
  }
  width.value = Math.max(240, element.clientWidth)
  observer = new ResizeObserver(([entry]) => {
    if (entry) {
      width.value = Math.max(240, Math.floor(entry.contentRect.width))
    }
  })
  observer.observe(element)
})

onBeforeUnmount(() => observer?.disconnect())

const box = computed<ChartBox>(() => ({ width: width.value, height: HEIGHT, ...INSET }))
const ticks = computed(() => niceTicks(Math.max(0, ...props.days.map((day) => day.cost))))
const ceiling = computed(() => ticks.value[ticks.value.length - 1] ?? 1)
const points = computed(() => chartPoints(props.days.map((day) => day.cost), box.value, ceiling.value))
const floor = computed(() => HEIGHT - INSET.bottom)
const line = computed(() => linePath(points.value))
const area = computed(() => areaPath(points.value, floor.value))
const last = computed(() => points.value[points.value.length - 1] ?? null)

/** A gridline's amount: whole dollars without cents, anything smaller as `formatUsd` says. */
function tickLabel(value: number): string {
  return Number.isInteger(value)
    ? new Intl.NumberFormat(locale.value, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value)
    : formatUsd(value, locale.value)
}

const gridlines = computed(() =>
  ticks.value.map((value) => ({ value, y: tickY(value, box.value, ceiling.value), label: tickLabel(value) })),
)

const dates = computed(() =>
  labelIndexes(props.days.length, width.value < 480 ? 3 : 5).map((index, position, all) => ({
    index,
    x: points.value[index]?.x ?? 0,
    label: formatUtcDay(props.days[index]?.day ?? '', locale.value, true),
    anchor: all.length > 1 && position === 0 ? 'start' : all.length > 1 && position === all.length - 1 ? 'end' : 'middle',
  })),
)

// --- the pointer -----------------------------------------------------------------------

const hovered = ref<number | null>(null)

function onPointerMove(event: PointerEvent) {
  const svg = event.currentTarget as SVGSVGElement
  const index = nearestIndex(event.clientX - svg.getBoundingClientRect().left, points.value)
  hovered.value = index >= 0 ? index : null
}

const readout = computed(() => {
  const index = hovered.value
  const day = index === null ? undefined : props.days[index]
  const point = index === null ? undefined : points.value[index]
  if (!day || !point) {
    return null
  }
  return {
    point,
    date: formatUtcDay(day.day, locale.value),
    cost: formatUsd(day.cost, locale.value),
    top: day.top ? { model: day.top.model, cost: formatUsd(day.top.cost, locale.value) } : null,
    others: day.others > 0 ? formatUsd(day.others, locale.value) : null,
    // The readout stays inside the chart: pinned to the side the point is nearest.
    style: point.x > width.value / 2 ? { right: `${width.value - point.x + 12}px` } : { left: `${point.x + 12}px` },
  }
})
</script>

<template>
  <div ref="frame" class="chart">
    <svg
      role="img"
      :aria-label="label"
      :width="width"
      :height="HEIGHT"
      :viewBox="`0 0 ${width} ${HEIGHT}`"
      @pointermove="onPointerMove"
      @pointerleave="hovered = null"
    >
      <g class="grid">
        <template v-for="gridline in gridlines" :key="gridline.value">
          <line :x1="INSET.left" :x2="width - INSET.right" :y1="gridline.y" :y2="gridline.y" />
          <text :x="INSET.left - 8" :y="gridline.y" text-anchor="end" dominant-baseline="middle">
            {{ gridline.label }}
          </text>
        </template>
      </g>
      <g class="dates">
        <text v-for="date in dates" :key="date.index" :x="date.x" :y="HEIGHT - 8" :text-anchor="date.anchor">
          {{ date.label }}
        </text>
      </g>
      <path class="wash" :d="area" />
      <path class="line" :d="line" />
      <circle v-if="last" class="dot" :cx="last.x" :cy="last.y" r="4" />
      <g v-if="readout" class="crosshair">
        <line :x1="readout.point.x" :x2="readout.point.x" :y1="INSET.top" :y2="floor" />
        <circle class="dot" :cx="readout.point.x" :cy="readout.point.y" r="4" />
      </g>
    </svg>
    <div v-if="readout" class="readout" :style="readout.style" aria-hidden="true">
      <p class="readout-head">
        <span>{{ readout.date }}</span>
        <span class="tabular">{{ readout.cost }}</span>
      </p>
      <p v-if="readout.top" class="readout-row">
        <span class="mono">{{ readout.top.model }}</span>
        <span class="tabular">{{ readout.top.cost }}</span>
      </p>
      <p v-if="readout.others" class="readout-row">
        <span>{{ t('admin.usage.others') }}</span>
        <span class="tabular">{{ readout.others }}</span>
      </p>
    </div>
  </div>
</template>

<style scoped>
.chart {
  position: relative;
  min-width: 0;
}

svg {
  display: block;
  width: 100%;
  overflow: visible;
  touch-action: pan-y;
}

.grid line {
  stroke: var(--line);
  stroke-width: 1;
  shape-rendering: crispEdges;
}

.grid text,
.dates text {
  fill: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.wash {
  fill: var(--accent-wash);
}

.line {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

/* The marked point: the accent with a ring of the surface, so it stands off the line. */
.dot {
  fill: var(--accent);
  stroke: var(--paper-sunken);
  stroke-width: 2;
}

.crosshair line {
  stroke: var(--line-strong);
  stroke-width: 1;
  shape-rendering: crispEdges;
}

.readout {
  position: absolute;
  top: 0;
  display: grid;
  gap: 2px;
  min-width: 11rem;
  max-width: 18rem;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  font-size: var(--text-xs);
  pointer-events: none;
}

.readout-head,
.readout-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}

.readout-head {
  color: var(--ink);
  font-weight: var(--weight-semibold);
}

.readout-row {
  color: var(--muted);
}

.readout-row .mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
