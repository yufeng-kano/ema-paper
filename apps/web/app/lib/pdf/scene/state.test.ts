import { describe, expect, it } from 'vitest'
import { SceneBuilder, StyleInherit } from './scene.ts'
import { applyGState, initialState, setFont, StateStack, type GState, type GStateContext } from './state.ts'

const VIEW: [number, number, number, number, number, number] = [1, 0, 0, -1, 0, 792]

function builder() {
  return new SceneBuilder(0, 'stamp', { width: 612, height: 792 })
}

/** A context whose fonts are numbered in the order asked for, holding one closed soft mask (row 7). */
function context(): GStateContext & { fonts: string[] } {
  const fonts: string[] = []
  let softMask = 7
  return {
    fonts,
    font: (name) => {
      if (!fonts.includes(name)) fonts.push(name)
      return fonts.indexOf(name)
    },
    takeSoftMask: () => {
      const row = softMask
      softMask = -1
      return row
    },
  }
}

/** Every field of the state set to a value no default has. */
function changeEverything(s: GState) {
  s.ctm = [2, 0, 0, 2, 5, 6]
  s.ctmId = 3
  s.clip = 4
  s.fillPaint = { type: 'solid', rgb: 0xff0000 }
  s.strokePaint = { type: 'transparent' }
  s.lineWidth = 3
  s.cap = 1
  s.join = 2
  s.miter = 4
  s.dash = [1, 2]
  s.phase = 0.5
  s.styleInherit = StyleInherit.Cap
  s.fillAlpha = 0.5
  s.strokeAlpha = 0.25
  s.blend = 1
  s.mask = 9
  s.maskBlend = 3
  s.transfer = 2
  s.pendingClip = 2
  Object.assign(s.text, { matrix: [1, 0, 0, 1, 7, 8], matrixScale: 1, x: 1, y: 2, lineX: 3, lineY: 4, charSpacing: 5,
    wordSpacing: 6, hScale: 0.5, leading: -12, rise: 3, mode: 7, font: 2, fontSize: 11, fontDirection: -1 })
}

describe('the graphics state', () => {
  it('starts with pdf.js’s defaults under the view transform', () => {
    const s = initialState(VIEW)
    expect(s.ctm).toEqual(VIEW)
    expect(s.ctm).not.toBe(VIEW)
    expect(s).toMatchObject({ clip: -1, fillPaint: { type: 'solid', rgb: 0 }, strokePaint: { type: 'solid', rgb: 0 },
      lineWidth: 1, cap: 0, join: 0, miter: 10, dash: [], phase: 0, styleInherit: 0, fillAlpha: 1, strokeAlpha: 1, blend: 0,
      mask: -1, maskBlend: 0, transfer: -1, pendingClip: 0 })
    expect(s.text).toMatchObject({ matrix: [1, 0, 0, 1, 0, 0], x: 0, y: 0, hScale: 1, leading: 0, rise: 0, mode: 0,
      font: -1, fontSize: 0, fontDirection: 1 })
  })

  it('restores every field, the text state included, as pdf.js’s save and restore do', () => {
    const stack = new StateStack(initialState(VIEW))
    changeEverything(stack.current)
    const before = structuredClone(stack.current)
    stack.save()
    expect(stack.current).toEqual(before)

    const inner = stack.current
    inner.ctm = [1, 0, 0, 1, 0, 0]
    inner.clip = 10
    inner.fillPaint = { type: 'transparent' }
    inner.lineWidth = 9
    inner.dash = []
    inner.mask = -1
    inner.text.x = 100
    inner.text.font = 0
    inner.text.matrix = [3, 0, 0, 3, 0, 0]
    inner.pendingClip = 0

    expect(stack.restore()).toBe(true)
    // A restore drops a pending clip (pdf.js nulls `pendingClip`), so only that field differs.
    expect(stack.current).toEqual({ ...before, pendingClip: 0 })
    expect(stack.restore()).toBe(false)
  })

  it('keeps a clip pending across a save, as pdf.js holds it outside the state', () => {
    const stack = new StateStack(initialState(VIEW))
    stack.current.pendingClip = 1
    stack.save()
    expect(stack.current.pendingClip).toBe(1)
  })

  it('ends a soft mask on a restore with nothing saved, and says it popped nothing', () => {
    const stack = new StateStack(initialState(VIEW))
    stack.current.mask = 5
    stack.current.lineWidth = 4
    expect(stack.restore()).toBe(false)
    expect(stack.current.mask).toBe(-1)
    expect(stack.current.lineWidth).toBe(4)
  })
})

describe('setFont', () => {
  it('takes a negative size as the opposite direction', () => {
    const { text } = initialState(VIEW)
    setFont(text, 3, -12)
    expect(text).toMatchObject({ font: 3, fontSize: 12, fontDirection: -1 })
    setFont(text, 1, 9)
    expect(text).toMatchObject({ font: 1, fontSize: 9, fontDirection: 1 })
  })
})

describe('setGState', () => {
  it('applies the line, alpha, blend and font keys and ignores RI and FL', () => {
    const s = initialState(VIEW)
    const ctx = context()
    applyGState(s, [['LW', 2.5], ['LC', 1], ['LJ', 2], ['ML', 4], ['RI', 'Perceptual'], ['FL', 1], ['CA', 0.3],
      ['ca', 0.6], ['BM', 'multiply'], ['Font', ['g_d0_f1', -10]]], builder(), ctx)
    expect(s).toMatchObject({ lineWidth: 2.5, cap: 1, join: 2, miter: 4, strokeAlpha: 0.3, fillAlpha: 0.6, blend: 1 })
    expect(s.text).toMatchObject({ font: 0, fontSize: 10, fontDirection: -1 })
    expect(ctx.fonts).toEqual(['g_d0_f1'])
  })

  it('reads an unknown blend mode as source-over', () => {
    const s = initialState(VIEW)
    applyGState(s, [['BM', 'luminosity']], builder(), context())
    expect(s.blend).toBe(15)
    applyGState(s, [['BM', 'unknown']], builder(), context())
    expect(s.blend).toBe(0)
  })

  it('clears the inherited bit of each stroke field it sets', () => {
    const s = initialState(VIEW)
    s.styleInherit = StyleInherit.Width | StyleInherit.Cap | StyleInherit.Join | StyleInherit.Miter | StyleInherit.Dash
    applyGState(s, [['LW', 2], ['D', [[1], 0]]], builder(), context())
    expect(s.styleInherit).toBe(StyleInherit.Cap | StyleInherit.Join | StyleInherit.Miter)
    applyGState(s, [['LC', 1], ['LJ', 1], ['ML', 2]], builder(), context())
    expect(s.styleInherit).toBe(0)
  })

  it('copies the dash array, so the op list’s array is never held', () => {
    const s = initialState(VIEW)
    const dash = [3, 1]
    applyGState(s, [['D', [dash, 2]]], builder(), context())
    dash[0] = 99
    expect(s.dash).toEqual([3, 1])
    expect(s.phase).toBe(2)
  })

  it('makes the last closed soft mask current at SMask true, and forgets it at any SMask key', () => {
    const s = initialState(VIEW)
    const ctx = context()
    applyGState(s, [['SMask', true]], builder(), ctx)
    expect(s.mask).toBe(7)
    // Taken: a second SMask true finds none, as pdf.js's `tempSMask` is null by then.
    applyGState(s, [['SMask', true]], builder(), ctx)
    expect(s.mask).toBe(-1)

    const other = context()
    applyGState(s, [['SMask', false]], builder(), other)
    expect(s.mask).toBe(-1)
    expect(other.takeSoftMask()).toBe(-1)
  })

  // pdf.js keeps the blend of the SMask key on the mask (build/pdf.mjs 10335) and draws the masked content
  // into a scratch canvas set to source-over (10507); a later BM reaches only that canvas.
  it('keeps the blend in force at the SMask key for the mask, in either key order', () => {
    const smaskThenBm = initialState(VIEW)
    applyGState(smaskThenBm, [['SMask', true], ['BM', 'multiply']], builder(), context())
    expect(smaskThenBm).toMatchObject({ mask: 7, maskBlend: 0, blend: 1 })

    const bmThenSmask = initialState(VIEW)
    applyGState(bmThenSmask, [['BM', 'multiply'], ['SMask', true]], builder(), context())
    expect(bmThenSmask).toMatchObject({ mask: 7, maskBlend: 1, blend: 0 })
    // Ending the mask copies the scratch canvas's blend back, which a BM under the mask set.
    applyGState(bmThenSmask, [['BM', 'screen'], ['SMask', false]], builder(), context())
    expect(bmThenSmask).toMatchObject({ mask: -1, blend: 2 })
  })

  it('expands a transfer function to 1024 bytes of R, G, B and an identity A', () => {
    const s = initialState(VIEW)
    const b = builder()
    const invert = Uint8Array.from({ length: 256 }, (_, i) => 255 - i)
    applyGState(s, [['TR', [invert]]], b, context())
    expect(s.transfer).toBe(0)
    applyGState(s, [['TR', null]], b, context())
    expect(s.transfer).toBe(-1)

    const { transfers } = b.finish()
    expect(transfers.length).toBe(1024)
    expect([transfers[0], transfers[255], transfers[256 + 10], transfers[512 + 200]]).toEqual([255, 0, 245, 55])
    expect([transfers[768], transfers[768 + 200], transfers[1023]]).toEqual([0, 200, 255])
  })
})
