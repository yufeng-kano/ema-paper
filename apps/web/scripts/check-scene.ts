/**
 * The stage 3 scene check on a PDF on disk (docs/pdf-viewer.md § Stage 3: the scene check): each page
 * is read through `scene-input.ts`, adapted into its scene, drawn by pdf.js's Canvas 2D with its boxes
 * recorded, and compared with `scene/check.ts`. It prints each page's report and a last line
 * `checked N pages, M mismatches`, and exits with 1 when M is not 0, and with 2 on a bad argument or a
 * range with no page in it.
 *
 * Run from `apps/web`: `pnpm scene:check <file.pdf> [first-last]`, pages numbered from 1.
 */

import { readFileSync } from 'node:fs'
import { adaptPage } from '../app/lib/pdf/scene/adapter.ts'
import { buildReport, diffGrids, diffText, gridFromRecorded, gridFromScene, reportLines } from '../app/lib/pdf/scene/check.ts'
import { RENDERER_STAGE } from '../app/lib/pdf/scene/coverage.ts'
import { messageChannelClock, runSliced } from '../app/lib/pdf/scene/slice.ts'
import { documentStamp, readOcVisibility, readPageInput, readTextItems } from '../app/lib/pdf/scene-input.ts'
import { drawRecorded, openDocument, pdfjs, servedAssets } from './pdfjs-node.ts'

const [file, range] = process.argv.slice(2)
const bounds = range === undefined ? null : /^(\d+)(?:-(\d+))?$/.exec(range)
if (file === undefined || (range !== undefined && bounds === null)) {
  console.error('usage: pnpm scene:check <file.pdf> [first-last]')
  process.exit(2)
}

// Read first, so a file that cannot be read leaves no asset folder behind.
const data = new Uint8Array(readFileSync(file))
const assets = servedAssets()
const task = openDocument(data, assets.dir)
try {
  const pdf = await task.promise
  const first = bounds === null ? 1 : Math.max(1, Number(bounds[1]))
  const last = bounds === null ? pdf.numPages : Math.min(pdf.numPages, Number(bounds[2] ?? bounds[1]))
  const stamp = documentStamp(pdf.fingerprints[0] ?? '', pdfjs.version, 'webgpu')
  let checked = 0
  let mismatches = 0
  for (let number = first; number <= last; number++) {
    const page = await pdf.getPage(number)
    // Read before drawing: pdf.js's Canvas 2D deletes each glyph outline it turns into a Path2D.
    const input = await readPageInput(page, stamp)
    const items = await readTextItems(page)
    const drawn = await drawRecorded(pdf, page)
    const adapted = await runSliced(adaptPage(input), messageChannelClock(), new AbortController().signal)
    const { scene, stats } = adapted!
    const ocVisible = await readOcVisibility(pdf, scene.ocProps)
    const grid = diffGrids(gridFromRecorded(drawn, scene), gridFromScene(scene, ocVisible), scene.view)
    const text = diffText(scene, items, input.view.transform)
    const report = buildReport({ scene, stats, renderMs: drawn.ms, grid, text, rendererStage: RENDERER_STAGE })
    for (const line of reportLines(report)) console.log(line)
    console.log('')
    checked++
    if (report.verdict === 'mismatch') mismatches++
    page.cleanup()
  }
  if (checked === 0) {
    // A range with no page in it would otherwise read as a pass.
    console.error(`no pages in ${range} (the document has ${pdf.numPages})`)
    process.exitCode = 2
  } else {
    console.log(`checked ${checked} pages, ${mismatches} mismatches`)
    process.exitCode = mismatches > 0 ? 1 : 0
  }
} finally {
  await task.destroy()
  assets.remove()
}
