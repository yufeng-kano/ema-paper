import { describe, expect, it } from 'vitest'
import { IDENTITY } from './matrix.ts'
import { OPS } from './ops.ts'
import { Flag, isContainer, Kind, kindName, NoteReason, PaintKind, SCENE_VERSION, SceneBuilder, sceneBytes, type PageScene } from './scene.ts'

const VIEW = { width: 612, height: 792 }
const AT = { flags: 0, clip: -1, xform: 0 }

function builder() {
  return new SceneBuilder(4, 'stamp', VIEW)
}

/** Every row's `end` and `parent` agree with a pre-order walk of the tree they describe. */
function expectWellFormed(scene: PageScene) {
  const { kind, parent, end } = scene.obj
  const stack: number[] = []
  for (let row = 0; row < scene.count; row++) {
    while (stack.length > 0 && end[stack.at(-1)!]! <= row) stack.pop()
    expect(parent[row]).toBe(stack.at(-1) ?? -1)
    if (isContainer(kind[row]!)) {
      expect(end[row]).toBeGreaterThan(row)
      expect(end[row]).toBeLessThanOrEqual(stack.length > 0 ? end[stack.at(-1)!]! : scene.count)
      stack.push(row)
    } else {
      expect(end[row]).toBe(row + 1)
    }
  }
}

describe('the scene’s rows', () => {
  it('form a pre-order tree whose ends close each subtree', () => {
    const b = builder()
    const form = b.open(Kind.Form, 3, AT)
    b.leaf(Kind.Path, 4, AT)
    const group = b.open(Kind.Group, 5, AT)
    b.leaf(Kind.Image, 6, { ...AT, d0: 0, d1: 0 })
    b.close(group)
    b.leaf(Kind.Text, 7, AT)
    b.close(form)
    b.leaf(Kind.Path, 8, AT)
    const scene = b.finish()
    expectWellFormed(scene)
    expect(Array.from(scene.obj.end)).toEqual([5, 2, 4, 4, 5, 6])
    expect(Array.from(scene.obj.parent)).toEqual([-1, 0, 0, 2, 0, -1])
    expect(Array.from(scene.obj.op)).toEqual([3, 4, 5, 6, 7, 8])
  })

  it('refuses to close a container that is not the innermost one open', () => {
    const b = builder()
    const outer = b.open(Kind.Form, 0, AT)
    b.open(Kind.Group, 1, AT)
    expect(() => b.close(outer)).toThrow()
    expect(() => b.markTreeEnd()).toThrow()
    expect(() => b.leaf(Kind.Form, 2, AT)).toThrow()
    expect(() => b.open(Kind.Path, 2, AT)).toThrow()
  })

  it('are active from the start, and default to no paint, no style and the default composite', () => {
    const b = builder()
    const row = b.leaf(Kind.Path, 0, { flags: Flag.Fill, clip: 2, xform: 1, fill: 5 })
    const { obj } = b.finish()
    expect(obj.flags[row]).toBe(Flag.Active | Flag.Fill)
    expect([obj.clip[row], obj.xform[row], obj.fill[row], obj.stroke[row], obj.style[row], obj.comp[row], obj.d0[row], obj.d1[row]])
      .toEqual([2, 1, 5, -1, -1, 0, -1, -1])
  })

  it('give a container the union of its children’s boxes', () => {
    const b = builder()
    const form = b.open(Kind.Form, 0, AT)
    b.setBox(b.leaf(Kind.Path, 1, AT), [10, 10, 20, 20])
    const group = b.open(Kind.Group, 2, AT)
    b.setBox(b.leaf(Kind.Path, 3, AT), [50, 5, 60, 15])
    b.close(group)
    b.leaf(Kind.Path, 4, AT)
    b.close(form)
    const empty = b.open(Kind.OptionalContent, 5, AT)
    b.close(empty)
    const { bbox } = b.finish().obj
    expect(Array.from(bbox.subarray(form * 4, form * 4 + 4))).toEqual([10, 5, 60, 20])
    expect(Array.from(bbox.subarray(group * 4, group * 4 + 4))).toEqual([50, 5, 60, 15])
    expect(Array.from(bbox.subarray(empty * 4, empty * 4 + 4))).toEqual([Infinity, Infinity, -Infinity, -Infinity])
  })

  it('after the tree’s end are flagged out of tree, and the tree end is kept', () => {
    const b = builder()
    b.leaf(Kind.Path, 0, AT)
    b.markTreeEnd()
    const glyph = b.open(Kind.Type3Glyph, 0, AT)
    const inside = b.leaf(Kind.Path, 1, AT)
    b.close(glyph)
    const scene = b.finish()
    expect(scene.treeEnd).toBe(1)
    expect(scene.obj.flags[0]! & Flag.OutOfTree).toBe(0)
    expect(scene.obj.flags[glyph]! & Flag.OutOfTree).toBe(Flag.OutOfTree)
    expect(scene.obj.flags[inside]! & Flag.OutOfTree).toBe(Flag.OutOfTree)
    expect(scene.obj.parent[glyph]).toBe(-1)
    expectWellFormed(scene)
  })

  it('end the tree at the last row when no end was marked', () => {
    const b = builder()
    b.leaf(Kind.Path, 0, AT)
    b.leaf(Kind.Path, 1, AT)
    expect(b.finish().treeEnd).toBe(2)
  })

  it('carry a note, and the NotDrawn flag, for content not drawn', () => {
    const b = builder()
    b.leaf(Kind.Path, 0, AT)
    const text = b.leaf(Kind.Text, 1, AT)
    b.note(text, NoteReason.FontFailed)
    const scene = b.finish()
    expect(Array.from(scene.notes.row)).toEqual([text])
    expect(Array.from(scene.notes.reason)).toEqual([NoteReason.FontFailed])
    expect(scene.obj.flags[text]! & Flag.NotDrawn).toBe(Flag.NotDrawn)
    expect(scene.obj.flags[0]! & Flag.NotDrawn).toBe(0)
  })
})

describe('growing and trimming', () => {
  it('keeps every row and pool entry past the first capacity, and trims each array to its length', () => {
    const b = builder()
    const form = b.open(Kind.Form, 0, AT)
    for (let i = 0; i < 1000; i++) {
      const row = b.leaf(Kind.Path, i + 1, { ...AT, xform: b.xform([1, 0, 0, 1, i, 0]), fill: b.solid(i) })
      b.setBox(row, [i, 0, i + 1, 1])
      b.path([0, i, 0, 1, i + 1, 0])
      b.glyph({ font: 0, code: i, x: i, y: 0, width: 500, unicode: -1, flags: 0 })
    }
    b.close(form)
    for (let i = 0; i < 3000; i++) b.glyph({ font: 0, code: i, x: 0, y: 0, width: 0, unicode: -1, flags: 0 })
    const scene = b.finish()
    expect(scene.count).toBe(1001)
    expect(scene.obj.kind.length).toBe(1001)
    expect(scene.obj.bbox.length).toBe(1001 * 4)
    expect(scene.obj.op[1000]).toBe(1000)
    expect(Array.from(scene.obj.bbox.subarray(1000 * 4))).toEqual([999, 0, 1000, 1])
    expect(Array.from(scene.obj.bbox.subarray(0, 4))).toEqual([0, 0, 1000, 1])
    expect(scene.xforms.length).toBe(1000 * 6)
    expect(Array.from(scene.xforms.subarray(999 * 6))).toEqual([1, 0, 0, 1, 999, 0])
    expect(scene.paints.kind.length).toBe(1000)
    expect(scene.paints.rgb[999]).toBe(999)
    expect(scene.pathData.length).toBe(6000)
    expect(Array.from(scene.pathData.subarray(5994))).toEqual([0, 999, 0, 1, 1000, 0])
    expect(scene.glyphs.code.length).toBe(4000)
    expect(scene.glyphs.code[3999]).toBe(2999)
    expect(scene.comps.blend.length).toBe(1)
    expect(scene.opCounts.length).toBe(OPS.rawFillPath + 1)
  })

  it('copies a path buffer, so a later change to pdf.js’s buffer does not reach the scene', () => {
    const b = builder()
    const buffer = Float32Array.of(0, 1, 2, 1, 3, 4)
    expect(b.path(buffer)).toEqual([0, 6])
    expect(b.path([4])).toEqual([6, 1])
    buffer[1] = 99
    expect(Array.from(b.finish().pathData)).toEqual([0, 1, 2, 1, 3, 4, 4])
  })

  it('keeps the opCounts the adapter wrote, as a copy', () => {
    const b = builder()
    b.opCounts[OPS.constructPath] = 7
    const scene = b.finish()
    b.opCounts[OPS.constructPath] = 8
    expect(scene.opCounts[OPS.constructPath]).toBe(7)
  })
})

describe('the pools', () => {
  it('share one matrix entry between consecutive equal matrices, and only those', () => {
    const b = builder()
    const a = b.xform(IDENTITY)
    expect(b.xform([1, 0, 0, 1, 0, 0])).toBe(a)
    const moved = b.xform([1, 0, 0, 1, 5, 0])
    expect(moved).not.toBe(a)
    expect(b.xform(IDENTITY)).toBe(moved + 1)
  })

  it('intern solid colours, and give every transparent paint one id', () => {
    const b = builder()
    const red = b.solid(0xff0000)
    expect(b.solid(0x00ff00)).not.toBe(red)
    expect(b.solid(0xff0000)).toBe(red)
    const none = b.transparent()
    expect(b.transparent()).toBe(none)
    const pattern = b.pattern(PaintKind.Tiling, 2, 3)
    expect(b.pattern(PaintKind.Tiling, 2, 3)).not.toBe(pattern)
    const { paints } = b.finish()
    expect([paints.kind[red], paints.rgb[red], paints.ref[red]]).toEqual([PaintKind.Solid, 0xff0000, -1])
    expect(paints.kind[none]).toBe(PaintKind.Transparent)
    expect([paints.kind[pattern], paints.ref[pattern], paints.xform[pattern]]).toEqual([PaintKind.Tiling, 2, 3])
  })

  it('intern stroke styles by value, dashes included', () => {
    const b = builder()
    const plain = { width: 1, cap: 0, join: 0, miter: 10, dash: [], phase: 0 }
    const dashed = { ...plain, dash: [3, 1], phase: 0.5 }
    const a = b.style(plain)
    const d = b.style(dashed)
    expect(b.style({ ...plain })).toBe(a)
    expect(b.style({ ...dashed, dash: [3, 1] })).toBe(d)
    expect(b.style({ ...dashed, dash: [3, 2] })).not.toBe(d)
    expect(b.style({ ...plain, width: 2 })).not.toBe(a)
    const { styles, dashes } = b.finish()
    expect([styles.dashStart[d], styles.dashCount[d], styles.dashPhase[d]]).toEqual([0, 2, 0.5])
    expect(Array.from(dashes)).toEqual([3, 1, 3, 2])
    expect([styles.width[a], styles.miter[a], styles.dashCount[a]]).toEqual([1, 10, 0])
  })

  it('intern composites, with 0 the default', () => {
    const b = builder()
    expect(b.comp({ blend: 0, fillAlpha: 1, strokeAlpha: 1, mask: -1, transfer: -1 })).toBe(0)
    const half = b.comp({ blend: 1, fillAlpha: 0.5, strokeAlpha: 1, mask: -1, transfer: -1 })
    expect(half).toBe(1)
    expect(b.comp({ blend: 1, fillAlpha: 0.5, strokeAlpha: 1, mask: -1, transfer: -1 })).toBe(half)
    expect(b.comp({ blend: 1, fillAlpha: 0.5, strokeAlpha: 1, mask: 3, transfer: -1 })).toBe(2)
    const { comps } = b.finish()
    expect([comps.blend[half], comps.fillAlpha[half], comps.mask[2]]).toEqual([1, 0.5, 3])
  })

  it('intern strings', () => {
    const b = builder()
    expect(b.string('A')).toBe(0)
    expect(b.string('fi')).toBe(1)
    expect(b.string('A')).toBe(0)
    expect(b.finish().strings).toEqual(['A', 'fi'])
  })

  it('expand transfer maps to R, G, B and A as pdf.js’s Canvas 2D applies them', () => {
    const b = builder()
    const invert = Uint8Array.from({ length: 256 }, (_, i) => 255 - i)
    expect(b.transfer(null)).toBe(-1)
    const one = b.transfer([invert])
    const four = b.transfer([null, invert, null, invert])
    const { transfers } = b.finish()
    expect(transfers.length).toBe(2 * 1024)
    const at = (id: number, channel: number, value: number) => transfers[id * 1024 + channel * 256 + value]
    // One map serves R, G and B; alpha is never mapped.
    expect([at(one, 0, 10), at(one, 1, 10), at(one, 2, 10), at(one, 3, 10)]).toEqual([245, 245, 245, 10])
    // Of four, a null map is the identity and the fourth is not used.
    expect([at(four, 0, 10), at(four, 1, 10), at(four, 2, 10), at(four, 3, 10)]).toEqual([10, 245, 10, 10])
  })

  it('copy shading IR, so the scene never shares a buffer with pdf.js', () => {
    const b = builder()
    const coords = Float32Array.of(1, 2, 3)
    const id = b.shading(['Mesh', 4, coords])
    coords[0] = 99
    const [kind, type, copied] = b.finish().shadings[id] as [string, number, Float32Array]
    expect([kind, type, Array.from(copied)]).toEqual(['Mesh', 4, [1, 2, 3]])
  })
})

describe('a finished scene', () => {
  function sample(): PageScene {
    const b = builder()
    const form = b.open(Kind.Form, 0, { ...AT, xform: b.xform([1, 0, 0, -1, 0, 792]) })
    const row = b.leaf(Kind.Path, 1, { flags: Flag.Fill, clip: b.clips.intern(-1, 0, false, 0, 3, 0, 'k', [0, 0, 9, 9]), xform: 0, fill: b.solid(0x336699) })
    b.setBox(row, [1, 2, 3, 4])
    b.path([0, 1, 2])
    b.style({ width: 2, cap: 1, join: 1, miter: 4, dash: [1, 1], phase: 0 })
    b.glyph({ font: b.font({ loadedName: 'g_d0_f1', kind: 'outline', css: null, vertical: false, remeasure: false, fontMatrix: [0.001, 0, 0, 0.001, 0, 0], bbox: null, procs: {} }), code: 65, x: 1, y: 2, width: 600, unicode: b.string('A'), flags: 0 })
    b.image({ id: 'img_p4_1', width: 4, height: 4, pdfWidth: 4, pdfHeight: 4 })
    b.transfer([Uint8Array.from({ length: 256 }, (_, i) => i)])
    b.ocProps({ type: 'OCG', id: '5R' })
    b.close(form)
    return b.finish()
  }

  it('says its version, page, stamp and view', () => {
    const scene = sample()
    expect([scene.version, scene.page, scene.stamp, scene.view]).toEqual([SCENE_VERSION, 4, 'stamp', VIEW])
    expect(scene.view).not.toBe(VIEW)
  })

  it('survives a structured clone unchanged', () => {
    const scene = sample()
    expect(structuredClone(scene)).toEqual(scene)
  })

  it('counts its bytes from its typed arrays, strings and numbers', () => {
    const scene = sample()
    const bytes = sceneBytes(scene)
    expect(bytes).toBeGreaterThan(scene.transfers.byteLength + scene.opCounts.byteLength + scene.obj.bbox.byteLength)
  })

  it('names its kinds', () => {
    expect(kindName(Kind.Type3Glyph)).toBe('Type3Glyph')
    expect(kindName(99)).toBe('kind 99')
  })
})
