/**
 * pdf.js in Node for the developer scripts (docs/pdf-viewer.md § Stage 3: the scene check): its
 * legacy build, opened as the WebGPU renderer opens a document, and drawn by its Canvas 2D at scale 1
 * into its own Node canvas with `recordOperations`, so a page comes with pdf.js's boxes for each op.
 */

import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist/legacy/build/pdf.mjs'
import type { RecordedBoxes } from '../app/lib/pdf/scene/check.ts'
import { PDFJS_ASSET_DIRS, selectPdfjsAssets } from '../shared/pdfjs-assets.ts'

// pdf.js polyfills Path2D from its optional dependency `@napi-rs/canvas` when it loads; without it
// nothing can be drawn and there are no boxes to record.
if (!('Path2D' in globalThis)) {
  throw new Error('pdf.js could not load @napi-rs/canvas, its Node canvas: run `pnpm install` in apps/web')
}

export { pdfjs }

/**
 * A temporary folder holding only the pdf.js files the app serves, so a font the browser cannot have
 * (the Liberation fonts) is missing here too.
 */
export function servedAssets(): { dir: string; remove(): void } {
  const pdfjsRoot = fileURLToPath(new URL('../node_modules/pdfjs-dist', import.meta.url))
  const dir = mkdtempSync(join(tmpdir(), 'pdfjs-assets-'))
  for (const folder of PDFJS_ASSET_DIRS) {
    const { files, missing } = selectPdfjsAssets(folder, readdirSync(join(pdfjsRoot, folder)))
    if (missing.length) throw new Error(`pdfjs-dist/${folder} lacks ${missing.join(', ')}`)
    mkdirSync(join(dir, folder))
    for (const name of files) copyFileSync(join(pdfjsRoot, folder, name), join(dir, folder, name))
  }
  return { dir, remove: () => rmSync(dir, { recursive: true, force: true }) }
}

/**
 * The app's font, asset and parsing options (app/lib/pdf/pdfjs.ts) with the WebGPU renderer's two, so
 * glyphs reach the display side as outlines. The app's `useWorkerFetch` and `disableWorkerRendering`
 * are left to pdf.js's Node defaults: the assets are read from disk, and Node has no worker rendering.
 * Warnings are shown, for the developer running the script.
 */
export function openDocument(data: Uint8Array, assets: string) {
  return pdfjs.getDocument({
    data,
    disableFontFace: true,
    useSystemFonts: false,
    useWasm: false,
    cMapUrl: join(assets, 'cmaps/'),
    cMapPacked: true,
    standardFontDataUrl: join(assets, 'standard_fonts/'),
    wasmUrl: join(assets, 'wasm/'),
    enableXfa: false,
    verbosity: pdfjs.VerbosityLevel.WARNINGS,
  })
}

/**
 * Draws the page at scale 1 with `recordOperations: true`. The boxes index pdf.js's render list, which
 * its optimizer merges from the operator list (W 5804-5884), so the list comes with them. pdf.js has
 * no public accessor for it: it is the operator list of the intent state a render started
 * (`displayReadyCapability`, build/pdf.mjs 16683).
 */
export async function drawRecorded(pdf: PDFDocumentProxy, page: PDFPageProxy): Promise<RecordedBoxes & {
  width: number
  height: number
  ms: number
}> {
  const viewport = page.getViewport({ scale: 1 })
  const { canvas } = (pdf.canvasFactory as { create(width: number, height: number): { canvas: HTMLCanvasElement } })
    .create(viewport.width, viewport.height)
  const start = performance.now()
  await page.render({ canvas, viewport, recordOperations: true }).promise
  const ms = performance.now() - start
  const buffer = (page.recordedBBoxes as { buffer: ArrayBuffer } | null)?.buffer
  if (!buffer) throw new Error(`page ${page.pageNumber}: pdf.js recorded no boxes`)
  const states = [...(page._intentStates as Map<string, { displayReadyCapability?: unknown;
    operatorList: { fnArray: number[]; argsArray: unknown[] } }>).values()]
  const rendered = states.find((state) => state.displayReadyCapability)
  if (!rendered) throw new Error(`page ${page.pageNumber}: pdf.js kept no render list`)
  const { fnArray, argsArray } = rendered.operatorList
  return { buffer: buffer.slice(0), fnArray, argsArray, width: canvas.width, height: canvas.height, ms }
}
