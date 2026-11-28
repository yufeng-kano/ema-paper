/**
 * Running the adapter in time slices (docs/pdf-viewer.md § The WebGPU renderer, *Threads*): the walk
 * runs on the main thread, so it gives the thread back every `SLICE_MS`, under pdf.js's own 15 ms
 * render slice. The clock is injected, so the tests drive it by hand.
 */

import type { Stepper } from './adapter.ts'
import type { PageScene, SceneSide } from './scene.ts'

export const SLICE_MS = 8

export interface SliceClock {
  now(): number
  /** Resolves once the thread has run whatever was waiting. */
  yield(): Promise<void>
}

/** Main-thread time only: the time spent yielded is not in `totalMs`. */
export interface SliceStats { slices: number; totalMs: number; longestMs: number }

/**
 * `performance.now()`, and a yield that posts a message to itself: unlike a timer it is not clamped to
 * 4 ms, and unlike a microtask it lets input and rendering run. Each yield has its own channel, closed
 * once it has delivered, so an idle channel never keeps a Node process alive.
 */
export function messageChannelClock(): SliceClock {
  return {
    now: () => performance.now(),
    yield: () => new Promise((resolve) => {
      const { port1, port2 } = new MessageChannel()
      port1.onmessage = () => {
        port1.close()
        resolve()
      }
      port2.postMessage(null)
    }),
  }
}

/**
 * Steps until the stepper finishes, yielding between slices of `SLICE_MS`. A slice ends after the op
 * that reaches it, so one slow op makes a slice longer. Null when `signal` is aborted before the scene
 * is finished.
 */
export async function runSliced(stepper: Stepper, clock: SliceClock,
  signal: AbortSignal): Promise<{ scene: PageScene; side: SceneSide; stats: SliceStats } | null> {
  const stats: SliceStats = { slices: 0, totalMs: 0, longestMs: 0 }
  let done = false
  while (!signal.aborted) {
    const start = clock.now()
    if (!done) done = stepper.step(() => clock.now() - start >= SLICE_MS)
    // Trimming the scene's arrays is main-thread work too: it has a slice of its own unless the last
    // step left time for it.
    const result = done && clock.now() - start < SLICE_MS ? stepper.finish() : null
    const ms = clock.now() - start
    stats.slices++
    stats.totalMs += ms
    stats.longestMs = Math.max(stats.longestMs, ms)
    if (result !== null) return { ...result, stats }
    await clock.yield()
  }
  return null
}
