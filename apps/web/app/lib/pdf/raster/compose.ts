/**
 * The CPU reference compositor (docs/pdf-viewer.md § Stage 4: the WebGPU core): a draw list composited as the
 * strip shader and the GPU's blend do it, for the tests and `pnpm raster:hashes` only, never for the page.
 *
 * Every target starts opaque white and each instance pixel blends premultiplied source-over with one rounding,
 * as a float blend into an 8-bit target rounds once per layer. Images sample their mip chain, built as the GPU
 * builds it, nearest or bilinear with 8-bit weights.
 */
import { NO_SLOT, PAINT_TEXEL_MASK, PAINT_TYPE_SHIFT, PaintType, type DrawList } from './protocol.ts'
import { mipLevelCount } from './sampling.ts'
import { div255 } from './encode.ts'

/** Premultiplied RGBA8 level 0 of an image or mask. */
export interface Texture { width: number; height: number; data: Uint8Array }

/**
 * Bilinear with the GPU's 8-bit weights at texel coordinate (x, y) (texel centres at whole numbers), edges
 * clamped, unrounded.
 */
function bilinear(t: Texture, x: number, y: number, channel: number): number {
  const x0 = Math.floor(x)
  const y0 = Math.floor(y)
  const fx = Math.round((x - x0) * 256)
  const fy = Math.round((y - y0) * 256)
  const left = Math.min(Math.max(x0, 0), t.width - 1)
  const right = Math.min(Math.max(x0 + 1, 0), t.width - 1)
  const top = Math.min(Math.max(y0, 0), t.height - 1)
  const bottom = Math.min(Math.max(y0 + 1, 0), t.height - 1)
  const d = t.data
  const upper = d[4 * (top * t.width + left) + channel]! * (256 - fx) + d[4 * (top * t.width + right) + channel]! * fx
  const lower = d[4 * (bottom * t.width + left) + channel]! * (256 - fx) + d[4 * (bottom * t.width + right) + channel]! * fx
  return (upper * (256 - fy) + lower * fy) / 65536
}

/** The mip chain the GPU builds at upload: level l from l - 1, bilinear at each destination texel centre, rounded to nearest. */
export function mipChain(level0: Texture): Texture[] {
  const chain = [level0]
  const levels = mipLevelCount(level0.width, level0.height)
  for (let level = 1; level < levels; level += 1) {
    const source = chain[level - 1]!
    const width = Math.max(1, level0.width >> level)
    const height = Math.max(1, level0.height >> level)
    const data = new Uint8Array(4 * width * height)
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const sx = ((x + 0.5) * source.width) / width - 0.5
        const sy = ((y + 0.5) * source.height) / height - 0.5
        for (let channel = 0; channel < 4; channel += 1) {
          data[4 * (y * width + x) + channel] = Math.round(bilinear(source, sx, sy, channel))
        }
      }
    }
    chain.push({ width, height, data })
  }
  return chain
}

/** The target as the GPU draws it, straight RGBA8 (alpha 255): one rounding per layer. `textures` by slot. */
export function composeList(list: DrawList, textures: readonly (Texture[] | null)[]): Uint8Array {
  const { width, height, instances, alphas, paints, runs } = list
  const floats = new Float32Array(paints.buffer, paints.byteOffset, paints.length)
  const target = new Uint8Array(4 * width * height).fill(255)
  const source = [0, 0, 0, 0]
  for (let run = 0; run < runs.length; run += 4) {
    const slot = runs[run + 2]!
    const chain = slot === NO_SLOT ? null : textures[slot] ?? null
    // The GPU side skips the runs of a texture whose upload failed.
    if (slot !== NO_SLOT && chain === null) {
      continue
    }
    const linear = runs[run + 3] === 1
    const first = runs[run]!
    for (let n = first; n < first + runs[run + 1]!; n += 1) {
      const x0 = instances[6 * n]! & 0xffff
      const y0 = instances[6 * n]! >>> 16
      const stripWidth = instances[6 * n + 1]! & 0xffff
      const dense = instances[6 * n + 1]! >>> 16
      const column = instances[6 * n + 2]!
      const payload = instances[6 * n + 3]!
      const paint = instances[6 * n + 4]!
      const type = paint >>> PAINT_TYPE_SHIFT
      const texel = 4 * (paint & PAINT_TEXEL_MASK)
      let level: Texture | null = null
      if (type === PaintType.Image || type === PaintType.Mask) {
        // The blank texture bound for a run without one samples as transparent: nothing drawn.
        if (chain === null) {
          continue
        }
        // The shader's textureSampleLevel clamps the level to the chain.
        level = chain[Math.min(floats[texel + 7]!, chain.length - 1)]!
      } else if (type !== PaintType.Solid) {
        continue
      }
      for (let row = 0; row < 4; row += 1) {
        const py = y0 + row
        if (py >= height) {
          break
        }
        for (let i = 0; i < stripWidth; i += 1) {
          const px = x0 + i
          if (px >= width) {
            break
          }
          const coverage = column + dense !== 0 ? alphas[4 * (column + i) + row]! / 255 : 1
          if (level === null) {
            for (let channel = 0; channel < 4; channel += 1) {
              source[channel] = ((payload >>> (8 * channel)) & 0xff) / 255
            }
          } else {
            // The UV map at the pixel centre, from the f32 values the shader reads.
            const cx = px + 0.5
            const cy = py + 0.5
            const u = floats[texel]! * cx + floats[texel + 2]! * cy + floats[texel + 4]!
            const v = floats[texel + 1]! * cx + floats[texel + 3]! * cy + floats[texel + 5]!
            const z = paints[texel + 6]!
            for (let channel = 0; channel < 4; channel += 1) {
              // A mask needs only the texel's alpha.
              const from = type === PaintType.Mask ? 3 : channel
              let value: number
              if (linear) {
                value = bilinear(level, u * level.width - 0.5, v * level.height - 0.5, from)
              } else {
                const tx = Math.min(Math.max(Math.floor(u * level.width), 0), level.width - 1)
                const ty = Math.min(Math.max(Math.floor(v * level.height), 0), level.height - 1)
                value = level.data[4 * (ty * level.width + tx) + from]!
              }
              source[channel] = type === PaintType.Mask
                ? (((z >>> (8 * channel)) & 0xff) / 255) * (value / 255)
                : ((z & 0xff) / 255) * (value / 255)
            }
          }
          const at = 4 * (py * width + px)
          const keep = 1 - coverage * source[3]!
          for (let channel = 0; channel < 4; channel += 1) {
            target[at + channel] = Math.floor(255 * (coverage * source[channel]! + (target[at + channel]! / 255) * keep) + 0.5)
          }
        }
      }
    }
  }
  return target
}

/** A deterministic premultiplied pattern standing in for an image's pixels (the fixtures store sizes only). */
export function patternTexture(width: number, height: number, seed: number): Texture {
  const data = new Uint8Array(4 * width * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      // Opaque, half and clear texels, so that premultiplying, opacity and masks all show in the pixels.
      const alpha = [255, 128, 255, 0, 255, 200][(x + 2 * y + seed) % 6]!
      const at = 4 * (y * width + x)
      data[at] = div255(((x * 37 + seed * 11) & 0xff) * alpha)
      data[at + 1] = div255(((y * 53 + seed * 7) & 0xff) * alpha)
      data[at + 2] = div255(((((x ^ y) * 29) + seed * 3) & 0xff) * alpha)
      data[at + 3] = alpha
    }
  }
  return { width, height, data }
}
