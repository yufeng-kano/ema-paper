/**
 * Stored operator-list fixtures (docs/pdf-viewer.md § Testing): one page of a hand-written PDF as
 * pdf.js 6.5.438 delivered it in Node, captured by `scripts/capture-oplists.ts`, with what the
 * adapter reads beside the list and pdf.js's own Canvas 2D boxes and text content for the check.
 * Used by the tests only, which never import pdf.js.
 *
 * JSON has no typed arrays and no maps, so the capture writes a typed array as
 * `{"$t": tag, "b64": little-endian bytes}`, an `ArrayBuffer` as tag `ab`, and a `Map` as
 * `{"$map": [[key, value], …]}`. `decodeFixture` turns them back.
 */

import type { FontInput, PageInput } from './adapter.ts'
import type { TextItem } from './check.ts'
import type { Box4, Mat6 } from './matrix.ts'

export const FIXTURE_FORMAT = 1

export interface OpListFixture {
  format: typeof FIXTURE_FORMAT
  pdfjs: string
  document: string
  /** 0-based, as in the scene and in the file name, `<document>.p<page>.oplist.json`. */
  page: number
  /** pdf.js's `OPS` table at capture. */
  ops: Record<string, number>
  view: { width: number; height: number; transform: Mat6 }
  fnArray: number[]
  argsArray: unknown[]
  fonts: Record<string, FontInput | string>
  /** By `commonObjs` id, `<loadedName>_path_<fontChar>`; a glyph with an empty outline is absent. */
  glyphBounds: Record<string, Box4>
  /** The size delivered, and whether pdf.js smooths it (`/Interpolate`). */
  images: Record<string, { width: number; height: number; interpolate: boolean }>
  shadings: Record<string, unknown[]>
  text: TextItem[]
  /** One per `beginMarkedContentProps("OC", props)` op of the page's list, in op order. */
  ocVisible: boolean[]
  /** The Node canvas the page was drawn on at scale 1, and pdf.js's `recordedBBoxes.buffer`. */
  canvas: { width: number; height: number; boxes: ArrayBuffer }
}

/** Revives the tagged typed arrays, buffers and maps of a stored fixture. */
export function decodeFixture(json: string): OpListFixture {
  const fixture = JSON.parse(json, (_key, value: unknown) => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return value
    if ('$map' in value) return new Map(value.$map as [unknown, unknown][])
    if (!('$t' in value)) return value
    const { $t: tag, b64 } = value as { $t: string; b64: string }
    const binary = atob(b64)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
    // `bytes` is a fresh buffer at offset 0, so every view below is aligned.
    switch (tag) {
      case 'ab': return bytes.buffer
      case 'u8': return bytes
      case 'u8c': return new Uint8ClampedArray(bytes.buffer)
      case 'u16': return new Uint16Array(bytes.buffer)
      case 'u32': return new Uint32Array(bytes.buffer)
      case 'i32': return new Int32Array(bytes.buffer)
      case 'f32': return new Float32Array(bytes.buffer)
      case 'f64': return new Float64Array(bytes.buffer)
      default: throw new Error(`unknown typed-array tag ${JSON.stringify(tag)}`)
    }
  }) as OpListFixture
  if (fixture.format !== FIXTURE_FORMAT) throw new Error(`fixture format ${fixture.format}, expected ${FIXTURE_FORMAT}`)
  return fixture
}

/** The adapter's input for a stored page; the lookups answer null for an id the page never read. */
export function fixtureInput(f: OpListFixture, stamp = `fixture:${f.document}.p${f.page}:${f.pdfjs}`): PageInput {
  // `Object.hasOwn`, because a record parsed from JSON still inherits `constructor` and the like.
  const lookup = <T>(record: Record<string, T>, id: string): T | null => (Object.hasOwn(record, id) ? record[id]! : null)
  return {
    page: f.page,
    stamp,
    view: f.view,
    fnArray: f.fnArray,
    argsArray: f.argsArray,
    font: (id) => lookup(f.fonts, id),
    glyphBounds: (loadedName, fontChar) => lookup(f.glyphBounds, `${loadedName}_path_${fontChar}`),
    image: (objId) => lookup(f.images, objId),
    shading: (objId) => lookup(f.shadings, objId),
  }
}
