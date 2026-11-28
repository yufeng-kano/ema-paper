/**
 * Clips for the sparse-strip rasteriser (docs/pdf-viewer.md § Stage 4: the WebGPU core): the intersection of
 * two strip runs, and each scene clip node's chain as strips, built once per job.
 *
 * Ported from Vello 0.4.0 (vello_common, git 570f0c4): clip.rs 393-831, Apache-2.0 OR MIT,
 * Copyright 2025 the Vello Authors, THIRD_PARTY_NOTICES § 8.
 * Changed: runs in typed arrays; a per-job cache of clip chains by scene clip id.
 */
import { ClipKind, type PageScene } from '../scene/scene.ts'
import { FILL_GAP, SENTINEL_X, Strips } from './strips.ts'

/** Strips `[start, end)` of a store, the last one a sentinel; alpha indices are the store's. */
export interface StripRun { store: Strips; start: number; end: number }

const Region = { None: 0, Fill: 1, Strip: 2 } as const

/**
 * Vello's `RowIterator`: the strip and filled regions of one strip row, alternately. The run's arrays are read
 * through `run.store` at every step, because the output of an intersection may be the same store and grow.
 */
class RowIterator {
  run: StripRun
  index: number
  row: number
  onStrip = true
  kind: number = Region.None
  start = 0
  width = 0
  alpha = 0

  constructor(run: StripRun, index: number, row: number) {
    while ((run.store.data[2 * index]! >>> 16) >> 2 < row) {
      index += 1
    }
    this.run = run
    this.index = index
    this.row = row
  }

  /** The current strip's width in px: the next strip's alpha index less its own, over 4. */
  #width(): number {
    const data = this.run.store.data
    return ((data[2 * this.index + 3]! & ~FILL_GAP) - (data[2 * this.index + 1]! & ~FILL_GAP)) / 4
  }

  /** Moves to the next region; false at the row's end. */
  next(): boolean {
    for (;;) {
      if (!this.onStrip) {
        this.onStrip = true
        const data = this.run.store.data
        const nextFills = (data[2 * this.index + 3]! & FILL_GAP) !== 0
        const x = (data[2 * this.index]! & 0xffff) + this.#width()
        const width = (data[2 * this.index + 2]! & 0xffff) - x
        this.index += 1
        // A next strip on the next row never fills its gap, so it needs no row check.
        if (nextFills && width > 0) {
          this.kind = Region.Fill
          this.start = x
          this.width = width
          return true
        }
      }
      this.onStrip = false
      const xy = this.run.store.data[2 * this.index]!
      if ((xy & 0xffff) === SENTINEL_X || (xy >>> 16) >> 2 !== this.row) {
        this.kind = Region.None
        return false
      }
      const width = this.#width()
      // A zero-width strip only closes a row's filled gap; it covers nothing.
      if (width === 0) {
        continue
      }
      this.kind = Region.Strip
      this.start = xy & 0xffff
      this.width = width
      this.alpha = this.run.store.data[2 * this.index + 1]! & ~FILL_GAP
      return true
    }
  }
}

/**
 * Vello's `intersect`: a ∩ b appended to `out`, which may be either run's store. Fill ∩ fill is a fill gap,
 * strip ∩ fill copies the strip's bytes, strip ∩ strip is `(a * b + 255) >> 8`. A sentinel ends what was
 * appended, when anything was.
 */
export function intersectStrips(a: StripRun, b: StripRun, out: Strips): void {
  if (a.end === a.start || b.end === b.start) {
    return
  }
  const rowOf = (run: StripRun, index: number): number => (run.store.data[2 * index]! >>> 16) >> 2
  const aStartRow = rowOf(a, a.start)
  const bStartRow = rowOf(b, b.start)
  let row = Math.max(aStartRow, bStartRow)
  const endRow = Math.min(rowOf(a, a.end - 1), rowOf(b, b.end - 1))
  const outStart = out.count

  // Skips the run that starts higher to the first strip at or after the common row, by bisection.
  const firstAtOrAfter = (run: StripRun): number => {
    let low = run.start
    let high = run.end
    while (low < high) {
      const middle = (low + high) >>> 1
      if (rowOf(run, middle) < row) {
        low = middle + 1
      } else {
        high = middle
      }
    }
    return low
  }
  let aIndex = a.start
  let bIndex = b.start
  if (aStartRow < row) {
    aIndex = firstAtOrAfter(a)
  } else if (bStartRow < row) {
    bIndex = firstAtOrAfter(b)
  }

  // The strip being built: its x, alpha index and fill gap, or none.
  let open = false
  let stripX = 0
  let stripAlpha = 0
  let stripGap = false
  const flush = (): void => {
    if (open) {
      out.push(stripX, 4 * row, stripAlpha, stripGap)
      open = false
    }
  }
  const start = (x: number, fillGap: boolean): void => {
    open = true
    stripX = x
    stripAlpha = out.alphaCount
    stripGap = fillGap
  }
  // False when the overlap can extend the strip being built.
  const shouldCreateNewStrip = (overlapStart: number): boolean =>
    !open || stripX + (out.alphaCount - stripAlpha) / 4 < overlapStart - 1

  while (row <= endRow) {
    const one = new RowIterator(a, aIndex, row)
    const two = new RowIterator(b, bIndex, row)
    let hasOne = one.next()
    let hasTwo = two.next()
    // Once either run has nothing left in the row, the rest of the other is clipped away.
    while (hasOne && hasTwo) {
      const oneEnd = one.start + one.width
      const twoEnd = two.start + two.width
      if (oneEnd <= two.start) {
        hasOne = one.next()
        continue
      }
      if (one.start >= twoEnd) {
        hasTwo = two.next()
        continue
      }
      const overlapStart = Math.max(one.start, two.start)
      const overlapEnd = Math.min(oneEnd, twoEnd)
      if (one.kind === Region.Fill && two.kind === Region.Fill) {
        // The strip closed here and one opened at the overlap's end fill everything between.
        flush()
        start(overlapEnd, true)
      } else if (one.kind === Region.Fill || two.kind === Region.Fill) {
        if (shouldCreateNewStrip(overlapStart)) {
          flush()
          start(overlapStart, false)
        }
        const strip = one.kind === Region.Strip ? one : two
        const n = 4 * (overlapEnd - overlapStart)
        const from = strip.alpha + 4 * (overlapStart - strip.start)
        const at = out.reserveAlphas(n)
        // Read after reserving: when the strip is in `out`, its bytes have moved with it.
        out.alphas.set(strip.run.store.alphas.subarray(from, from + n), at)
      } else {
        if (shouldCreateNewStrip(overlapStart)) {
          flush()
          start(overlapStart, false)
        }
        const n = 4 * (overlapEnd - overlapStart)
        const fromOne = one.alpha + 4 * (overlapStart - one.start)
        const fromTwo = two.alpha + 4 * (overlapStart - two.start)
        const at = out.reserveAlphas(n)
        const target = out.alphas
        const oneAlphas = one.run.store.alphas
        const twoAlphas = two.run.store.alphas
        for (let k = 0; k < n; k += 1) {
          target[at + k] = (oneAlphas[fromOne + k]! * twoAlphas[fromTwo + k]! + 255) >> 8
        }
      }
      if (oneEnd <= twoEnd) {
        hasOne = one.next()
      } else {
        hasTwo = two.next()
      }
    }
    flush()
    aIndex = one.index
    bIndex = two.index
    row += 1
  }
  if (out.count > outStart) {
    out.push(SENTINEL_X, 4 * endRow, out.alphaCount, false)
  }
}

/**
 * Per job: a clip node's chain as strips (its parent's chain ∩ its own path), built when a drawn row first needs
 * it. `get` answers null when the chain clips everything. A node is a scene clip id, never -1 (no clip). Text
 * clips are not rasterised in stage 4: the walk draws nothing under them, so `get` refuses them.
 */
export class ClipCache {
  #scene: PageScene
  #raster: (pathStart: number, pathEnd: number, xform: number, evenOdd: boolean, out: Strips) => StripRun | null
  #runs = new Map<number, StripRun | null>()
  #store = new Strips()
  #own = new Strips()

  /** `raster` appends the strips of a path range under a scene matrix to `out`, null when there are none. */
  constructor(scene: PageScene, raster: (pathStart: number, pathEnd: number, xform: number, evenOdd: boolean, out: Strips) => StripRun | null) {
    this.#scene = scene
    this.#raster = raster
  }

  get(node: number): StripRun | null {
    const known = this.#runs.get(node)
    if (known !== undefined) {
      return known
    }
    const clips = this.#scene.clips
    // The chain is built from the nearest known ancestor down, without recursion: chains can be long.
    const pending: number[] = []
    let ancestor = node
    while (ancestor >= 0 && !this.#runs.has(ancestor)) {
      pending.push(ancestor)
      ancestor = clips.parent[ancestor]!
    }
    // Undefined: the topmost pending node has no parent clip.
    let chain: StripRun | null | undefined = ancestor >= 0 ? this.#runs.get(ancestor) as StripRun | null : undefined
    for (let i = pending.length - 1; i >= 0; i -= 1) {
      const id = pending[i]!
      if (clips.kind[id] !== ClipKind.Path) {
        throw new Error(`clip ${id} is a text clip, which stage 4 does not rasterise`)
      }
      let run: StripRun | null = null
      if (chain === undefined) {
        run = this.#raster(clips.d0[id]!, clips.d1[id]!, clips.xform[id]!, clips.evenOdd[id] === 1, this.#store)
      } else if (chain !== null) {
        this.#own.clear()
        const own = this.#raster(clips.d0[id]!, clips.d1[id]!, clips.xform[id]!, clips.evenOdd[id] === 1, this.#own)
        if (own !== null) {
          const start = this.#store.count
          intersectStrips(chain, own, this.#store)
          run = this.#store.count > start ? { store: this.#store, start, end: this.#store.count } : null
        }
      }
      this.#runs.set(id, run)
      chain = run
    }
    return chain as StripRun | null
  }

  clear(): void {
    this.#runs.clear()
    this.#store.clear()
    this.#own.clear()
  }
}
