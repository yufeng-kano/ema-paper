import { describe, expect, it } from 'vitest'
import type { Stepper } from './adapter.ts'
import { SceneBuilder } from './scene.ts'
import { messageChannelClock, runSliced, SLICE_MS, type SliceClock } from './slice.ts'

/** A clock that moves only when an op runs, and a log of the ops, slices and yields in their order. */
function fakeRun(ops: number, opMs = 1) {
  const log: string[] = []
  let now = 0
  let ran = 0
  let finished = 0
  const clock: SliceClock = {
    now: () => now,
    yield: async () => {
      log.push('yield')
    },
  }
  const stepper: Stepper = {
    step(expired) {
      const start = now
      while (ran < ops) {
        ran++
        now += opMs
        if (expired()) break
      }
      log.push(`slice ${now - start}`)
      return ran === ops
    },
    finish() {
      finished++
      log.push('finish')
      return { scene: new SceneBuilder(0, 's', { width: 1, height: 1 }).finish(), side: { inline: [] } }
    },
  }
  return { clock, stepper, log, finishedCount: () => finished }
}

describe('a sliced run', () => {
  it('runs slices of at most the slice time, with a yield between two slices and none after the last', async () => {
    const run = fakeRun(20)
    const result = await runSliced(run.stepper, run.clock, new AbortController().signal)
    expect(run.log).toEqual(['slice 8', 'yield', 'slice 8', 'yield', 'slice 4', 'finish'])
    expect(result?.stats).toEqual({ slices: 3, totalMs: 20, longestMs: 8 })
    expect(result?.scene.page).toBe(0)
    expect(run.finishedCount()).toBe(1)
  })

  it('ends a slice after the op that reaches the slice time', async () => {
    const run = fakeRun(5, 3)
    const result = await runSliced(run.stepper, run.clock, new AbortController().signal)
    expect(SLICE_MS).toBe(8)
    expect(run.log).toEqual(['slice 9', 'yield', 'slice 6', 'finish'])
    expect(result?.stats).toEqual({ slices: 2, totalMs: 15, longestMs: 9 })
  })

  it('takes one slice for a list that fits in it', async () => {
    const run = fakeRun(3)
    const result = await runSliced(run.stepper, run.clock, new AbortController().signal)
    expect(run.log).toEqual(['slice 3', 'finish'])
    expect(result?.stats).toEqual({ slices: 1, totalMs: 3, longestMs: 3 })
  })

  it('finishes the scene in a slice of its own when the last step used the whole slice', async () => {
    const run = fakeRun(16)
    const result = await runSliced(run.stepper, run.clock, new AbortController().signal)
    expect(run.log).toEqual(['slice 8', 'yield', 'slice 8', 'yield', 'finish'])
    expect(result?.stats).toEqual({ slices: 3, totalMs: 16, longestMs: 8 })
  })

  it('returns null without stepping when aborted before it starts', async () => {
    const run = fakeRun(20)
    const controller = new AbortController()
    controller.abort()
    expect(await runSliced(run.stepper, run.clock, controller.signal)).toBeNull()
    expect(run.log).toEqual([])
  })

  it('returns null, and finishes no scene, when aborted during a yield', async () => {
    const run = fakeRun(20)
    const controller = new AbortController()
    const yieldOnce = run.clock.yield
    run.clock.yield = async () => {
      await yieldOnce()
      controller.abort()
    }
    expect(await runSliced(run.stepper, run.clock, controller.signal)).toBeNull()
    expect(run.log).toEqual(['slice 8', 'yield'])
    expect(run.finishedCount()).toBe(0)
  })
})

describe('the message-channel clock', () => {
  it('resumes after each yield, on a clock that does not go back', async () => {
    const clock = messageChannelClock()
    const before = clock.now()
    await clock.yield()
    await clock.yield()
    expect(clock.now()).toBeGreaterThanOrEqual(before)
  })
})
