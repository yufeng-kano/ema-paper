/**
 * Clip chains for the page scene (docs/pdf-viewer.md § Room for editing, *Clips are shared chains*).
 * A clip node is its parent, its rule and its path under a matrix or its text runs; two objects under the
 * same clip stack share one node, so changing one object never disturbs the clip of the next.
 *
 * The design is hayro-svg's clip interning (hayro-svg `lib.rs` 274-301, Apache-2.0 OR MIT), rewritten:
 * nothing of its code is taken.
 */

import { EMPTY_BOX, intersect, type Box4 } from './matrix.ts'
import type { ClipColumns } from './scene.ts'

export class ClipInterner {
  #ids = new Map<string, number>()
  #count = 0
  #parent = new Int32Array(64)
  #kind = new Uint8Array(64)
  #evenOdd = new Uint8Array(64)
  #d0 = new Int32Array(64)
  #d1 = new Int32Array(64)
  #xform = new Int32Array(64)
  // Kept in doubles, so an object clipped against a node meets the very box the node was cut to.
  #box = new Float64Array(64 * 4)

  /**
   * Interns (parent, kind, evenOdd, d-range content, xform) and returns its id. The box is the node's own
   * device-independent box in page space, intersected with the parent's. An empty intersection still gets
   * an id, with an empty box (`EMPTY_BOX`).
   */
  intern(parent: number, kind: 0 | 1, evenOdd: boolean, d0: number, d1: number, xform: number,
    contentKey: string, box: Readonly<Box4>): number {
    // The range itself is not in the key: the same path copied twice into the pool is the same clip.
    const key = `${parent}|${kind}|${evenOdd ? 1 : 0}|${xform}|${contentKey}`
    const known = this.#ids.get(key)
    if (known !== undefined) return known

    const parentBox = this.box(parent)
    const clipped = (parentBox === null ? [...box] : intersect(box, parentBox)) ?? [...EMPTY_BOX]
    const id = this.#count++
    if (id === this.#parent.length) {
      this.#parent = grow(this.#parent)
      this.#kind = grow(this.#kind)
      this.#evenOdd = grow(this.#evenOdd)
      this.#d0 = grow(this.#d0)
      this.#d1 = grow(this.#d1)
      this.#xform = grow(this.#xform)
      this.#box = grow(this.#box)
    }
    this.#parent[id] = parent
    this.#kind[id] = kind
    this.#evenOdd[id] = evenOdd ? 1 : 0
    this.#d0[id] = d0
    this.#d1[id] = d1
    this.#xform[id] = xform
    this.#box.set(clipped, id * 4)
    this.#ids.set(key, id)
    return id
  }

  /** A node's box in page space; null for -1 (no clip). */
  box(id: number): Box4 | null {
    if (id < 0) return null
    const b = this.#box
    return [b[id * 4]!, b[id * 4 + 1]!, b[id * 4 + 2]!, b[id * 4 + 3]!]
  }

  /** Trimmed copies, used by `SceneBuilder.finish`. */
  columns(): ClipColumns {
    const n = this.#count
    return {
      parent: this.#parent.slice(0, n),
      kind: this.#kind.slice(0, n),
      evenOdd: this.#evenOdd.slice(0, n),
      d0: this.#d0.slice(0, n),
      d1: this.#d1.slice(0, n),
      xform: this.#xform.slice(0, n),
      box: Float32Array.from(this.#box.subarray(0, n * 4)),
    }
  }
}

export type Column = Uint8Array | Uint16Array | Uint32Array | Int32Array | Float32Array | Float64Array

/** A column of twice the length holding `a`'s values. The scene's own columns grow the same way. */
export function grow<T extends Column>(a: T): T {
  const next = new (a.constructor as new (length: number) => T)(Math.max(a.length * 2, 16))
  next.set(a)
  return next
}

const floatBits = new Float32Array(1)
const floatBytes = new Uint8Array(floatBits.buffer)

/**
 * A path's content key: FNV-1a 32 over the bytes of its values as stored (Float32), plus its length. Two
 * different paths of the same length that hash alike would share a clip; at 32 bits within one page's
 * parent and matrix, that is accepted.
 */
export function pathKey(data: ArrayLike<number>, offset: number, length: number): string {
  let hash = 0x811c9dc5
  for (let i = offset; i < offset + length; i++) {
    floatBits[0] = data[i]!
    for (const byte of floatBytes) hash = Math.imul(hash ^ byte, 0x01000193)
  }
  return `${(hash >>> 0).toString(16)}:${length}`
}
