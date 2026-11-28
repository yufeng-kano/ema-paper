import { describe, expect, it } from 'vitest'
import { DRAW_OPS } from '../scene/ops.ts'
import { Lines, PathBuffer } from './buffers.ts'

describe('lines', () => {
  it('keep every line as they grow past their capacity', () => {
    const lines = new Lines()
    const first = lines.data
    const count = first.length // four times past the starting capacity
    for (let i = 0; i < count; i += 1) {
      lines.push(i, i + 0.5, -i, 2 * i)
    }
    expect(lines.count).toBe(count)
    expect(lines.data).not.toBe(first)
    expect(lines.data.length).toBeGreaterThanOrEqual(4 * count)
    for (let i = 0; i < count; i += 1) {
      expect(Array.from(lines.data.subarray(4 * i, 4 * i + 4))).toEqual([i, i + 0.5, -i, 2 * i])
    }
  })

  it('keep their memory on clear', () => {
    const lines = new Lines()
    for (let i = 0; i < 1000; i += 1) {
      lines.push(1, 2, 3, 4)
    }
    const data = lines.data
    lines.clear()
    expect(lines.count).toBe(0)
    expect(lines.data).toBe(data)
    lines.push(5, 6, 7, 8)
    expect(lines.data).toBe(data)
    expect(Array.from(lines.data.subarray(0, 4))).toEqual([5, 6, 7, 8])
  })

  it('store coordinates as f32', () => {
    const lines = new Lines()
    lines.push(0.1, 1 / 3, 1e-8 + 4096, -2.7)
    expect(Array.from(lines.data.subarray(0, 4))).toEqual([Math.fround(0.1), Math.fround(1 / 3), Math.fround(1e-8 + 4096), Math.fround(-2.7)])
    expect(lines.data[0]).not.toBe(0.1)
  })
})

describe('a path buffer', () => {
  it('writes pdf.js’s DrawOPS encoding in f64', () => {
    const path = new PathBuffer()
    path.moveTo(0.1, 2)
    path.lineTo(3, 4)
    path.curveTo(5, 6, 7, 8, 9, 10)
    path.close()
    expect(Array.from(path.data.subarray(0, path.length))).toEqual([
      DRAW_OPS.moveTo, 0.1, 2,
      DRAW_OPS.lineTo, 3, 4,
      DRAW_OPS.curveTo, 5, 6, 7, 8, 9, 10,
      DRAW_OPS.closePath,
    ])
  })

  it('keeps its content as it grows past its capacity', () => {
    const path = new PathBuffer()
    const first = path.data
    const curves = first.length // seven entries each: well past the starting capacity
    path.moveTo(-1, -1)
    for (let i = 0; i < curves; i += 1) {
      path.curveTo(i, i + 1, i + 2, i + 3, i + 4, i + 5)
    }
    path.close()
    expect(path.data).not.toBe(first)
    expect(path.length).toBe(3 + 7 * curves + 1)
    expect(Array.from(path.data.subarray(0, 3))).toEqual([DRAW_OPS.moveTo, -1, -1])
    for (let i = 0; i < curves; i += 1) {
      const at = 3 + 7 * i
      expect(Array.from(path.data.subarray(at, at + 7))).toEqual([DRAW_OPS.curveTo, i, i + 1, i + 2, i + 3, i + 4, i + 5])
    }
    expect(path.data[path.length - 1]).toBe(DRAW_OPS.closePath)
  })

  it('keeps its memory on clear', () => {
    const path = new PathBuffer()
    for (let i = 0; i < 1000; i += 1) {
      path.lineTo(i, i)
    }
    const data = path.data
    path.clear()
    expect(path.length).toBe(0)
    path.moveTo(1, 2)
    expect(path.data).toBe(data)
    expect(Array.from(path.data.subarray(0, path.length))).toEqual([DRAW_OPS.moveTo, 1, 2])
  })
})
