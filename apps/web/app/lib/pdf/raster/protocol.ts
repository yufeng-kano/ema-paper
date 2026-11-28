/**
 * The messages between the viewer and the raster worker, and the draw list the worker hands the GPU
 * side (docs/pdf-viewer.md § Stage 4: the WebGPU core). Shared by the worker, the renderer and the tests.
 */
import type { Mat6 } from '../scene/matrix.ts'
import { SCENE_VERSION, type PageScene } from '../scene/scene.ts'

/** A strip instance is Vello's 24-byte `GpuStrip`: 6 u32 words. */
export const INSTANCE_WORDS = 6
/** Both data textures are rgba32uint, this many texels wide (the shader's DATA_WIDTH_BITS = 12). */
export const DATA_TEXTURE_WIDTH = 4096
/** Bytes in one row of a data texture: 4096 texels of 16 bytes. */
export const DATA_ROW_BYTES = 65_536
/** At most this many rows per data texture (256 MiB); a job that needs more fails. */
export const MAX_DATA_ROWS = 4096
/** Words per run: first instance, instance count, texture slot, filter. */
export const RUN_WORDS = 4
/** A run's slot when it samples no texture. */
export const NO_SLOT = 0xffff_ffff
/** The WebGPU default `maxTextureDimension2D`; the device asks for no limits, so this is the limit. */
export const MAX_TEXTURE_SIDE = 8192

/** Instance word 4: `type << PAINT_TYPE_SHIFT | texel`. 6 and 7 are Vello's image types; 7 is ours (a mask). */
export const PaintType = { Solid: 0, Image: 6, Mask: 7 } as const
export type PaintType = (typeof PaintType)[keyof typeof PaintType]
export const PAINT_TYPE_SHIFT = 26
export const PAINT_TEXEL_MASK = 0x00ff_ffff
export const Filter = { Nearest: 0, Linear: 1 } as const

/** Why a row of the page tree is drawn as nothing in stage 4 (docs/pdf-viewer.md § Stage 4). */
export const NotDrawn = { Text: 1, Shading: 2, Tiling: 3, Blend: 4, SoftMask: 5, TextClip: 6, GroupLayer: 7, Transfer: 8 } as const
export type NotDrawn = (typeof NotDrawn)[keyof typeof NotDrawn]

/** What the GPU side draws into one target, all in that target's pixels. Buffers are transferred. */
export interface DrawList {
  /** The target's size in device px, 1..MAX_TEXTURE_SIDE each. */
  width: number
  height: number
  /** INSTANCE_WORDS per instance, in paint order. */
  instances: Uint32Array
  /** The alpha texture's bytes: whole DATA_ROW_BYTES rows, at least one. */
  alphas: Uint8Array
  /** The paint texture's words: 4 per texel, whole rows of DATA_TEXTURE_WIDTH texels, at least one. */
  paints: Uint32Array
  /** RUN_WORDS per run: first instance, count, slot (NO_SLOT or a texture slot), filter. */
  runs: Uint32Array
}

/** The worker's own figures for one job, for the console report. */
export interface RegionStats {
  ms: number
  rows: number
  tiles: number
  strips: number
  instances: number
  alphaBytes: number
}

export type RasterRequest =
  /** Posted once per reading; a structured clone, not a transfer. */
  | { type: 'scene'; reading: number; scene: PageScene; ocVisible: Uint8Array; inlineSizes: Uint32Array }
  /** `transform` maps page space (scene.view) to the target's px; `dpr` is devicePixelRatio, for smoothing. */
  | { type: 'region'; job: number; reading: number; width: number; height: number; transform: Mat6; dpr: number }
  | { type: 'drop'; reading: number }

export type RasterReply =
  /** `notDrawn` is pairs of (row, NotDrawn). Every typed array here is transferred. */
  | { type: 'drawn'; job: number; list: DrawList; notDrawn: Int32Array; stats: RegionStats }
  | { type: 'failed'; job: number; message: string }
  /** The reading was dropped before the job ran. */
  | { type: 'gone'; job: number }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isId(value: unknown): boolean {
  return Number.isSafeInteger(value) && (value as number) >= 0
}

function isSide(value: unknown): boolean {
  return Number.isInteger(value) && (value as number) >= 1 && (value as number) <= MAX_TEXTURE_SIDE
}

/** Shape only: the scene's contents are never read here. */
export function isRasterRequest(value: unknown): value is RasterRequest {
  if (!isRecord(value) || !isId(value.reading)) {
    return false
  }
  switch (value.type) {
    case 'scene':
      return isRecord(value.scene) && value.scene.version === SCENE_VERSION
        && value.ocVisible instanceof Uint8Array
        && value.inlineSizes instanceof Uint32Array && value.inlineSizes.length % 2 === 0
    case 'region':
      return isId(value.job) && isSide(value.width) && isSide(value.height)
        && Array.isArray(value.transform) && value.transform.length === 6
        && value.transform.every((entry) => Number.isFinite(entry))
        && Number.isFinite(value.dpr) && (value.dpr as number) > 0
    case 'drop':
      return true
    default:
      return false
  }
}

export function isRasterReply(value: unknown): value is RasterReply {
  if (!isRecord(value) || !isId(value.job)) {
    return false
  }
  switch (value.type) {
    case 'drawn': {
      const list = value.list
      return isRecord(list)
        && list.instances instanceof Uint32Array && list.alphas instanceof Uint8Array
        && list.paints instanceof Uint32Array && list.runs instanceof Uint32Array
        && value.notDrawn instanceof Int32Array && isRecord(value.stats)
    }
    case 'failed':
      return typeof value.message === 'string'
    case 'gone':
      return true
    default:
      return false
  }
}

/** The buffers a reply moves: the five of 'drawn', none otherwise. */
export function replyTransfer(reply: RasterReply): ArrayBuffer[] {
  if (reply.type !== 'drawn') {
    return []
  }
  const { instances, alphas, paints, runs } = reply.list
  // The worker allocates each array on its own, never on a SharedArrayBuffer (the site is not cross-origin isolated).
  return [instances, alphas, paints, runs, reply.notDrawn].map((array) => array.buffer as ArrayBuffer)
}
