/**
 * The graphics and text state the adapter carries while it walks an operator list
 * (docs/pdf-viewer.md § The WebGPU renderer, *The page scene*).
 *
 * Ported from pdf.js 6.5.438, src/display/canvas.js (`CanvasExtraState`; `CanvasGraphics` `save`,
 * `restore`, `setGState`, `setFont`; build/pdf.mjs 9639-9710, 10292-10364, 10807-10834, 11077-11093),
 * Apache-2.0, Copyright 2024 Mozilla Foundation (THIRD_PARTY_NOTICES § 1). Changed: one plain record
 * holds what pdf.js splits between `CanvasExtraState` and the canvas context (the CTM, the clip, cap,
 * join, dash), colours are paint references the scene interns, and the clip is a clip-chain id.
 * docs/pdf-viewer.md § What is taken from where.
 */

import { IDENTITY, type Mat6 } from './matrix.ts'
import { BLEND_MODES, StyleInherit, type SceneBuilder } from './scene.ts'

/**
 * A fill or stroke colour as the state holds it. The adapter turns it into a scene paint id when a row
 * is painted, because a shading pattern's matrix is composed with the base transform current at the
 * paint, not at the `scn` (pdf.js's `RadialAxialShadingPattern.getPattern` reads `owner.baseTransform`).
 */
export type PaintRef =
  | { type: 'solid'; rgb: number }
  | { type: 'transparent' }
  /** `shading` is the scene's shading index, or -1 when pdf.js delivered no IR for it. */
  | { type: 'shading'; shading: number; matrix: Mat6 | null }
  /** Already a scene paint: a tiling pattern, whose base transform is the one at the `scn`. */
  | { type: 'paint'; id: number }
  /** Inside an out-of-tree procedure: the paint of the row that uses it (a Type 3 glyph's text, an uncoloured tiling). */
  | { type: 'inherit' }

interface TextState {
  /** The text matrix; pdf.js's `null` reads as the identity. */
  matrix: Mat6
  /** `hypot(matrix[0], matrix[1])`, pdf.js's `textMatrixScale`. */
  matrixScale: number
  x: number
  y: number
  lineX: number
  lineY: number
  charSpacing: number
  wordSpacing: number
  /** `Tz / 100`. */
  hScale: number
  /** `-TL`, as pdf.js stores it. */
  leading: number
  rise: number
  mode: number
  /** Index into the scene's fonts, or -1 before any `Tf`. */
  font: number
  /** Always positive; a negative `Tf` size sets `fontDirection` to -1. */
  fontSize: number
  fontDirection: 1 | -1
}

export interface GState {
  /** Page space from the current user space. */
  ctm: Mat6
  /** The `xforms` id of `ctm`, or -1 until a row needs one. */
  ctmId: number
  /** The clip chain in force, or -1. */
  clip: number
  fillPaint: PaintRef
  strokePaint: PaintRef
  lineWidth: number
  cap: number
  join: number
  miter: number
  dash: readonly number[]
  phase: number
  /**
   * `StyleInherit` bits: the stroke fields a Type 3 glyph's procedure has not set, which it takes from
   * the text that shows it, as pdf.js runs the procedure in that text's state. 0 everywhere else.
   */
  styleInherit: number
  fillAlpha: number
  strokeAlpha: number
  /** Index into `BLEND_MODES`. */
  blend: number
  /** The `SoftMask` row in force, or -1. */
  mask: number
  /**
   * The blend in force at the `SMask` key that set `mask`. pdf.js keeps it on the mask and composites
   * the masked content with it; a `BM` while the mask is in force only reaches `blend`, which applies
   * again once the mask ends.
   */
  maskBlend: number
  /** A `transfers` id, or -1. */
  transfer: number
  text: TextState
  /** 0 none, 1 nonzero, 2 even-odd: a `W` or `W*` waiting for the next path, as pdf.js's `pendingClip`. */
  pendingClip: 0 | 1 | 2
}

/** What `setGState` reads beyond the state itself. */
export interface GStateContext {
  /** The scene's font index for a `loadedName`. */
  font(loadedName: string): number
  /** The `SoftMask` row the last `endGroup` closed, or -1; pdf.js forgets it at any `SMask` key. */
  takeSoftMask(): number
}

/** pdf.js's defaults: black fill and stroke, width 1, butt caps, miter joins, miter limit 10. */
export function initialState(view: Readonly<Mat6>): GState {
  return {
    ctm: [...view],
    ctmId: -1,
    clip: -1,
    fillPaint: { type: 'solid', rgb: 0 },
    strokePaint: { type: 'solid', rgb: 0 },
    lineWidth: 1,
    cap: 0,
    join: 0,
    miter: 10,
    dash: [],
    phase: 0,
    styleInherit: 0,
    fillAlpha: 1,
    strokeAlpha: 1,
    blend: 0,
    mask: -1,
    maskBlend: 0,
    transfer: -1,
    text: {
      matrix: [...IDENTITY],
      matrixScale: 1,
      x: 0,
      y: 0,
      lineX: 0,
      lineY: 0,
      charSpacing: 0,
      wordSpacing: 0,
      hScale: 1,
      leading: 0,
      rise: 0,
      mode: 0,
      font: -1,
      fontSize: 0,
      fontDirection: 1,
    },
    pendingClip: 0,
  }
}

/**
 * pdf.js's `stateStack` with its `current`. Arrays in the state are replaced, never written into, so a
 * saved state shares them safely; only the text state is changed in place and is copied.
 */
export class StateStack {
  #current: GState
  #saved: GState[] = []

  constructor(initial: GState) {
    this.#current = initial
  }

  get current(): GState {
    return this.#current
  }

  save(): void {
    this.#saved.push(this.#current)
    this.#current = { ...this.#current, text: { ...this.#current.text } }
  }

  /**
   * False on an empty stack. pdf.js still leaves its soft-mask mode there (build/pdf.mjs 10819-10823), so
   * the mask ends. A pending clip does not survive a restore that pops.
   */
  restore(): boolean {
    const previous = this.#saved.pop()
    if (previous === undefined) {
      this.#current.mask = -1
      return false
    }
    previous.pendingClip = 0
    this.#current = previous
    return true
  }
}

/** `Tf`, and `setGState`'s `Font`. */
export function setFont(text: TextState, font: number, size: number): void {
  text.font = font
  text.fontDirection = size < 0 ? -1 : 1
  text.fontSize = Math.abs(size)
}

/** `LW LC LJ ML D CA ca BM SMask TR Font`, in the order given; `RI` and `FL` are no-ops in pdf.js too. */
export function applyGState(state: GState, entries: readonly (readonly [string, unknown])[], b: SceneBuilder,
  context: GStateContext): void {
  for (const [key, value] of entries) {
    switch (key) {
      case 'LW':
        state.lineWidth = value as number
        state.styleInherit &= ~StyleInherit.Width
        break
      case 'LC':
        state.cap = value as number
        state.styleInherit &= ~StyleInherit.Cap
        break
      case 'LJ':
        state.join = value as number
        state.styleInherit &= ~StyleInherit.Join
        break
      case 'ML':
        state.miter = value as number
        state.styleInherit &= ~StyleInherit.Miter
        break
      case 'D': {
        const [dash, phase] = value as [ArrayLike<number>, number]
        state.dash = Array.from(dash)
        state.phase = phase
        state.styleInherit &= ~StyleInherit.Dash
        break
      }
      case 'Font': {
        const [loadedName, size] = value as [string, number]
        setFont(state.text, context.font(loadedName), size)
        break
      }
      case 'CA':
        state.strokeAlpha = value as number
        break
      case 'ca':
        state.fillAlpha = value as number
        break
      case 'BM':
        state.blend = Math.max(0, BLEND_MODES.indexOf(value as (typeof BLEND_MODES)[number]))
        break
      case 'SMask': {
        const last = context.takeSoftMask()
        const wasMasked = state.mask >= 0
        state.mask = value ? last : -1
        if (state.mask >= 0) {
          state.maskBlend = state.blend
          // pdf.js draws masked content into a scratch canvas whose blend it sets to source-over
          // (beginSMaskMode, build/pdf.mjs 10507); an SMask: false copies that blend back.
          if (!wasMasked) state.blend = 0
        }
        break
      }
      case 'TR':
        state.transfer = b.transfer(value as readonly (Uint8Array | null)[] | null)
        break
    }
  }
}
