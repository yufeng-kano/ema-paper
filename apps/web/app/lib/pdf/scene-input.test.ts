import { describe, expect, it } from 'vitest'
import { OPS } from './scene/ops.ts'
import { readPageInput } from './scene-input.ts'

/** pdf.js's `PDFObjects` as `readPageInput` sees it: resolved entries, and ids still waited on. */
function objects(resolved: Record<string, unknown>) {
  const waiting = new Map<string, (data: unknown) => void>()
  return {
    waiting,
    has: (id: string) => id in resolved,
    get(id: string, callback?: (data: unknown) => void) {
      if (callback) {
        waiting.set(id, callback)
        return null
      }
      return resolved[id]
    },
  }
}

/** A page whose operator list is `ops`, with its own objects `objs`. */
function page(ops: [number, unknown][], objs: ReturnType<typeof objects>) {
  return {
    pageNumber: 1,
    objs,
    commonObjs: objects({}),
    getViewport: () => ({ width: 100, height: 100, transform: [1, 0, 0, -1, 0, 100] }),
    getOperatorList: () => Promise.resolve({ fnArray: ops.map(([fn]) => fn), argsArray: ops.map(([, args]) => args) }),
    getTextContent: () => Promise.resolve({ items: [] }),
  }
}

describe('readPageInput', () => {
  it('leaves out an image pdf.js could not decode, which it resolves as null', async () => {
    const objs = objects({ img_p0_1: null, img_p0_2: { width: 4, height: 2, interpolate: true } })
    const input = await readPageInput(page([[OPS.paintImageXObject, ['img_p0_1', 8, 8]],
      [OPS.paintImageXObject, ['img_p0_2', 4, 2]]], objs), 's')
    expect(input.image('img_p0_1')).toBeNull()
    expect(input.image('img_p0_2')).toEqual({ width: 4, height: 2, interpolate: true })
  })

  it('rejects when aborted while it waits for an object pdf.js never delivers', async () => {
    const objs = objects({})
    const controller = new AbortController()
    const reading = readPageInput(page([[OPS.dependency, ['img_p0_1']]], objs), 's', controller.signal)
    await expect.poll(() => objs.waiting.has('img_p0_1')).toBe(true)
    controller.abort(new Error('left the render window'))
    await expect(reading).rejects.toThrow('left the render window')
  })

  it('waits for an object that is not yet resolved', async () => {
    const objs = objects({})
    const reading = readPageInput(page([[OPS.paintImageXObject, ['img_p0_1', 8, 8]]], objs), 's', new AbortController().signal)
    await expect.poll(() => objs.waiting.has('img_p0_1')).toBe(true)
    objs.waiting.get('img_p0_1')!({ width: 8, height: 8 })
    expect((await reading).image('img_p0_1')).toEqual({ width: 8, height: 8, interpolate: false })
  })
})
