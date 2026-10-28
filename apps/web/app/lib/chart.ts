/**
 * The geometry of the console's daily cost chart (docs/admin.md § Pages, *Usage*), drawn in
 * SVG by the page itself — no chart library (docs/web.md): the round values its gridlines sit
 * at, where each day's point lands, the line and the wash under it, which days are labelled
 * under the axis, and which day the pointer is nearest. Framework-free numbers in, numbers
 * and path strings out.
 */

/** The plot inside the SVG: its size and the room kept around it for the labels. */
export interface ChartBox {
  width: number
  height: number
  left: number
  right: number
  top: number
  bottom: number
}

export interface ChartPoint {
  x: number
  y: number
}

/** One day of the chart, as the Usage page hands it over. */
export interface CostDay {
  /** `YYYY-MM-DD`, UTC. */
  day: string
  cost: number
  /** The model that cost most that day; null for a day without cost. */
  top: { model: string; cost: number } | null
  /** Every other model's cost that day together. */
  others: number
}

/**
 * The gridlines' values, from 0 up to the first one at or above `max`: a step of 1, 2, 2.5 or
 * 5 times a power of ten, so about `count` lines land on round amounts. A chart with nothing
 * above 0 gets 0 and 1, so the line lies on its floor rather than in the middle.
 */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) {
    return [0, 1]
  }
  const rough = max / count
  const power = 10 ** Math.floor(Math.log10(rough))
  const scaled = rough / power
  const step = (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10) * power
  const steps = Math.ceil(max / step - 1e-9)
  // `toPrecision` drops the float noise a step of 0.1 picks up (0.30000000000000004).
  return Array.from({ length: steps + 1 }, (_, index) => Number((index * step).toPrecision(12)))
}

/** Where each value lands: evenly across the plot, oldest at the left, a lone value in the
 *  middle; `ceiling` at the top of the plot and 0 on its floor. */
export function chartPoints(values: readonly number[], box: ChartBox, ceiling: number): ChartPoint[] {
  const plotWidth = box.width - box.left - box.right
  const plotHeight = box.height - box.top - box.bottom
  const floor = box.height - box.bottom
  return values.map((value, index) => ({
    x: values.length === 1 ? box.left + plotWidth / 2 : box.left + (plotWidth * index) / (values.length - 1),
    y: floor - (ceiling > 0 ? (plotHeight * Math.max(0, value)) / ceiling : 0),
  }))
}

/** The y of a gridline's value. */
export function tickY(value: number, box: ChartBox, ceiling: number): number {
  return box.height - box.bottom - (box.height - box.top - box.bottom) * (ceiling > 0 ? value / ceiling : 0)
}

function round(value: number): string {
  return String(Math.round(value * 10) / 10)
}

/** The line through the points. */
export function linePath(points: readonly ChartPoint[]): string {
  return points.map((point, index) => `${index ? 'L' : 'M'}${round(point.x)} ${round(point.y)}`).join(' ')
}

/** The line closed down to the floor: the wash under it. Empty without points. */
export function areaPath(points: readonly ChartPoint[], floor: number): string {
  if (!points.length) {
    return ''
  }
  const first = points[0]!
  const last = points[points.length - 1]!
  return `${linePath(points)} L${round(last.x)} ${round(floor)} L${round(first.x)} ${round(floor)} Z`
}

/** Which of `n` days get a date under the axis: at most `max` of them, evenly spaced, the
 *  first and the last always among them. */
export function labelIndexes(n: number, max = 5): number[] {
  if (n <= 0) {
    return []
  }
  if (n <= max) {
    return Array.from({ length: n }, (_, index) => index)
  }
  const indexes = Array.from({ length: max }, (_, index) => Math.round((index * (n - 1)) / (max - 1)))
  return [...new Set(indexes)]
}

/** The point nearest the pointer's x: what the crosshair snaps to. -1 without points. */
export function nearestIndex(x: number, points: readonly ChartPoint[]): number {
  let best = -1
  let distance = Infinity
  points.forEach((point, index) => {
    const away = Math.abs(point.x - x)
    if (away < distance) {
      best = index
      distance = away
    }
  })
  return best
}
