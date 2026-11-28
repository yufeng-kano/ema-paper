import { describe, expect, it } from 'vitest'
import { SCENE_VERSION, type PageScene } from '../scene/scene.ts'
import { isRasterReply, isRasterRequest, MAX_TEXTURE_SIDE, replyTransfer, type DrawList, type RasterReply } from './protocol.ts'

// The guards check shape only, so a scene that is nothing but its version stands for one.
const scene = { version: SCENE_VERSION } as unknown as PageScene

function sceneRequest() {
  return { type: 'scene', reading: 3, scene, ocVisible: new Uint8Array(2), inlineSizes: new Uint32Array([10, 20]) }
}

function regionRequest() {
  return { type: 'region', job: 7, reading: 3, width: 800, height: 600, transform: [2, 0, 0, 2, 0, 0], dpr: 2 }
}

function list(): DrawList {
  return { width: 4, height: 4, instances: new Uint32Array(6), alphas: new Uint8Array(65_536), paints: new Uint32Array(16_384), runs: new Uint32Array(4) }
}

function drawnReply(): Extract<RasterReply, { type: 'drawn' }> {
  return { type: 'drawn', job: 7, list: list(), notDrawn: new Int32Array([5, 1]),
    stats: { ms: 1, rows: 2, tiles: 3, strips: 4, instances: 1, alphaBytes: 16 } }
}

describe('the request guard', () => {
  it('accepts each request', () => {
    expect(isRasterRequest(sceneRequest())).toBe(true)
    expect(isRasterRequest(regionRequest())).toBe(true)
    expect(isRasterRequest({ type: 'drop', reading: 0 })).toBe(true)
    expect(isRasterRequest({ ...regionRequest(), width: 1, height: MAX_TEXTURE_SIDE })).toBe(true)
  })

  it('rejects what is not a request', () => {
    expect(isRasterRequest(null)).toBe(false)
    expect(isRasterRequest('scene')).toBe(false)
    expect(isRasterRequest({ type: 'paint', reading: 0 })).toBe(false)
    expect(isRasterRequest({ type: 'drop' })).toBe(false)
  })

  it('rejects ids that are not non-negative safe integers', () => {
    for (const id of [-1, 1.5, Number.NaN, 2 ** 53, '3', undefined]) {
      expect(isRasterRequest({ type: 'drop', reading: id }), String(id)).toBe(false)
      expect(isRasterRequest({ ...regionRequest(), job: id }), String(id)).toBe(false)
    }
  })

  it('rejects a region with a wrong size, transform or pixel ratio', () => {
    const bad = [
      { width: 0 },
      { width: MAX_TEXTURE_SIDE + 1 },
      { height: 0 },
      { height: 10.5 },
      { transform: [2, 0, 0, 2, 0, 0, 0] },
      { transform: [2, 0, 0, 2, 0] },
      { transform: [2, 0, 0, 2, 0, Number.NaN] },
      { transform: [2, 0, 0, 2, 0, Infinity] },
      { transform: new Float64Array([2, 0, 0, 2, 0, 0]) },
      { dpr: Number.NaN },
      { dpr: 0 },
      { dpr: -1 },
      { dpr: Infinity },
      { reading: undefined },
    ]
    for (const change of bad) {
      expect(isRasterRequest({ ...regionRequest(), ...change }), JSON.stringify(change)).toBe(false)
    }
  })

  it('rejects a scene with a wrong version, odd inline sizes or plain arrays', () => {
    expect(isRasterRequest({ ...sceneRequest(), scene: { version: SCENE_VERSION + 1 } })).toBe(false)
    expect(isRasterRequest({ ...sceneRequest(), scene: null })).toBe(false)
    expect(isRasterRequest({ ...sceneRequest(), inlineSizes: new Uint32Array([10, 20, 30]) })).toBe(false)
    expect(isRasterRequest({ ...sceneRequest(), inlineSizes: [10, 20] })).toBe(false)
    expect(isRasterRequest({ ...sceneRequest(), ocVisible: [1, 0] })).toBe(false)
    expect(isRasterRequest({ ...sceneRequest(), ocVisible: new Uint8ClampedArray(2) })).toBe(false)
  })
})

describe('the reply guard', () => {
  it('accepts each reply', () => {
    expect(isRasterReply(drawnReply())).toBe(true)
    expect(isRasterReply({ type: 'failed', job: 7, message: 'Error: too many tiles' })).toBe(true)
    expect(isRasterReply({ type: 'gone', job: 0 })).toBe(true)
  })

  it('rejects a missing or wrong field', () => {
    expect(isRasterReply({ type: 'gone' })).toBe(false)
    expect(isRasterReply({ type: 'gone', job: -1 })).toBe(false)
    expect(isRasterReply({ type: 'failed', job: 7 })).toBe(false)
    expect(isRasterReply({ type: 'scene', job: 7 })).toBe(false)
    expect(isRasterReply({ ...drawnReply(), list: null })).toBe(false)
    expect(isRasterReply({ ...drawnReply(), notDrawn: [5, 1] })).toBe(false)
    expect(isRasterReply({ ...drawnReply(), stats: undefined })).toBe(false)
    for (const key of ['instances', 'alphas', 'paints', 'runs'] as const) {
      expect(isRasterReply({ ...drawnReply(), list: { ...list(), [key]: Array.from(list()[key]) } }), key).toBe(false)
    }
    expect(isRasterReply({ ...drawnReply(), list: { ...list(), alphas: new Uint32Array(16_384) } })).toBe(false)
  })
})

describe('what a reply transfers', () => {
  it('is the five buffers of a drawn reply, each once', () => {
    const reply = drawnReply()
    const buffers = replyTransfer(reply)
    expect(buffers).toHaveLength(5)
    expect(new Set(buffers).size).toBe(5)
    const { instances, alphas, paints, runs } = reply.list
    for (const array of [instances, alphas, paints, runs, reply.notDrawn]) {
      expect(buffers).toContain(array.buffer)
    }
  })

  it('is nothing for a failed or gone reply', () => {
    expect(replyTransfer({ type: 'failed', job: 1, message: 'x' })).toEqual([])
    expect(replyTransfer({ type: 'gone', job: 1 })).toEqual([])
  })
})
