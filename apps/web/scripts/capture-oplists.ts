/**
 * Captures the stored operator-list fixtures (docs/pdf-viewer.md § Testing): every page of the
 * hand-written documents in `pdf-fixtures/`, read with pdf.js's legacy build in Node under the
 * WebGPU renderer's open options, then drawn by pdf.js's Canvas 2D at scale 1 with
 * `recordOperations`, so each fixture carries pdf.js's own boxes and text as the check's oracle.
 *
 * Run from `apps/web` with `pnpm fixtures:oplists` under Node 24 or later (the Docker image's), again
 * after every `pdfjs-dist` upgrade. It rewrites `app/lib/pdf/scene/fixtures/` whole, and the same pdf.js
 * gives the same bytes: nothing in a fixture depends on the time, the machine or the order objects
 * arrive in. It refuses an older Node because pdf.js hands glyph outlines over as `Float16Array`
 * wherever the runtime has one, as Node 24 and the browsers do, and as `Float32Array` elsewhere, and the
 * stored glyph boxes come from those values.
 */

import { mkdirSync, readdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { FIXTURE_FORMAT, type OpListFixture } from '../app/lib/pdf/scene/fixture.ts'
import { documentStamp, readOcVisibility, readPageInput, readTextItems } from '../app/lib/pdf/scene-input.ts'
import { FIXTURE_DOCUMENTS } from './pdf-fixtures/documents.ts'
import { drawRecorded, openDocument, pdfjs, servedAssets } from './pdfjs-node.ts'

if (!('Float16Array' in globalThis)) {
  throw new Error(`capture the fixtures with Node 24 or later, not ${process.version}: there pdf.js gives glyph outlines in half floats, as in a browser`)
}

const out = fileURLToPath(new URL('../app/lib/pdf/scene/fixtures', import.meta.url))

const TAGS = new Map<unknown, string>([
  [Uint8Array, 'u8'], [Uint8ClampedArray, 'u8c'], [Uint16Array, 'u16'], [Uint32Array, 'u32'],
  [Int32Array, 'i32'], [Float32Array, 'f32'], [Float64Array, 'f64'],
])

/** JSON with typed arrays, buffers and maps tagged as `fixture.ts` decodes them. */
function encode(value: unknown): string {
  return JSON.stringify(value ?? null, (_key, v: unknown) => {
    if (typeof v === 'number' && !Number.isFinite(v)) throw new Error(`${v} has no JSON form`)
    if (v instanceof Map) return { $map: [...v] }
    if (v instanceof ArrayBuffer) return { $t: 'ab', b64: Buffer.from(v).toString('base64') }
    if (!ArrayBuffer.isView(v)) return v
    // A Float16 glyph path is widened; Float32 holds it exactly.
    const view = v.constructor.name === 'Float16Array' ? Float32Array.from(v as unknown as ArrayLike<number>) : v
    const tag = TAGS.get(view.constructor)
    if (!tag) throw new Error(`no fixture tag for ${view.constructor.name}`)
    return { $t: tag, b64: Buffer.from(view.buffer, view.byteOffset, view.byteLength).toString('base64') }
  })
}

/** A record with its keys sorted, so the file does not depend on the order things were read in. */
function sorted<T>(map: ReadonlyMap<string, T>): Record<string, T> {
  return Object.fromEntries([...map].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
}

const assets = servedAssets()
let total = 0
try {
  mkdirSync(out, { recursive: true })
  for (const name of readdirSync(out)) if (name.endsWith('.oplist.json')) unlinkSync(join(out, name))

  for (const document of FIXTURE_DOCUMENTS) {
    const task = openDocument(document.build(), assets.dir)
    try {
      const pdf = await task.promise
      const stamp = documentStamp(pdf.fingerprints[0] ?? '', pdfjs.version, 'webgpu')
      for (let number = 1; number <= pdf.numPages; number++) {
        const page = await pdf.getPage(number)
        // Read before drawing: pdf.js's Canvas 2D deletes each glyph outline it turns into a Path2D.
        const input = await readPageInput(page, stamp)
        const text = await readTextItems(page)
        const ocProps: unknown[] = []
        for (let i = 0; i < input.fnArray.length; i++) {
          const args = input.argsArray[i] as unknown[] | null
          if (input.fnArray[i] === pdfjs.OPS.beginMarkedContentProps && args![0] === 'OC') ocProps.push(args![1])
        }
        const ocVisible = await readOcVisibility(pdf, ocProps)
        const drawn = await drawRecorded(pdf, page)
        // A fixture stores one list for the adapter and for the boxes, so the two must be the same.
        if (drawn.fnArray.length !== input.fnArray.length || Array.from(drawn.fnArray).some((fn, i) => fn !== input.fnArray[i])) {
          throw new Error(`${document.name} page ${number}: pdf.js's render list is not its operator list; simplify the page`)
        }

        const fixture: OpListFixture = {
          format: FIXTURE_FORMAT,
          pdfjs: pdfjs.version,
          document: document.name,
          page: input.page,
          ops: { ...pdfjs.OPS },
          view: input.view,
          fnArray: Array.from(input.fnArray),
          argsArray: [...input.argsArray],
          fonts: sorted(input.read.fonts),
          glyphBounds: sorted(input.read.glyphBoxes),
          images: sorted(input.read.images),
          shadings: sorted(input.read.shadings) as Record<string, unknown[]>,
          text,
          ocVisible: Array.from(ocVisible, (visible) => visible === 1),
          canvas: { width: drawn.width, height: drawn.height, boxes: drawn.buffer },
        }
        // One op's arguments per line, so a re-capture after an upgrade diffs by op.
        const lines = Object.entries(fixture).map(([key, value]) => key === 'argsArray'
          ? `"argsArray": [\n${fixture.argsArray.map(encode).join(',\n')}\n]`
          : `${JSON.stringify(key)}: ${encode(value)}`)
        const file = `${document.name}.p${input.page}.oplist.json`
        const json = `{\n${lines.join(',\n')}\n}\n`
        writeFileSync(join(out, file), json)
        const bytes = Buffer.byteLength(json)
        total += bytes

        console.log(`${file} (${bytes} bytes, ${fixture.fnArray.length} ops)`)
      }
    } finally {
      await task.destroy()
    }
  }
} finally {
  assets.remove()
}
console.log(`wrote ${total} bytes of fixtures to ${out}`)
