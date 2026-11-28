/**
 * Reads a page from pdf.js into the adapter's input (docs/pdf-viewer.md § The WebGPU renderer,
 * *The page scene*): its operator list, the fonts, glyph-outline boxes, image sizes and shading IR
 * the list refers to, its text content and its optional-content visibility.
 *
 * It names the few members of pdf.js it reads with its own interfaces and imports nothing from
 * `pdfjs-dist`, so the Node scripts in `scripts/` run it as it is on pdf.js's legacy build, and the
 * viewer can call it with the browser build's page. It waits for every object the list depends on
 * before returning, as pdf.js's `CanvasGraphics.executeOperatorList` does at each `dependency` op,
 * so the adapter's walk never waits.
 */

import type { FontInput, PageInput } from './scene/adapter.ts'
import type { TextItem } from './scene/check.ts'
import type { Box4, Mat6 } from './scene/matrix.ts'
import { DRAW_OPS, OPS } from './scene/ops.ts'

/** pdf.js's `PDFObjects`: `get` with a callback waits, without one it throws for an id not yet resolved. */
interface PdfObjects {
  has(id: string): boolean
  get(id: string, callback?: (data: unknown) => void, errorCallback?: (reason: unknown) => void): unknown
}

export interface PdfOperatorList {
  fnArray: ArrayLike<number>
  argsArray: readonly unknown[]
}

/** The members of pdf.js's `PDFPageProxy` read here. */
interface PdfPage {
  readonly pageNumber: number
  readonly objs: PdfObjects
  readonly commonObjs: PdfObjects
  getViewport(params: { scale: number }): { width: number; height: number; transform: number[] }
  getOperatorList(params: { intent: 'display' }): Promise<PdfOperatorList>
  getTextContent(params: { disableNormalization: boolean }): Promise<{
    items: readonly ({ str: string; transform: number[] } | { type: string })[]
  }>
}

/** The members of pdf.js's `PDFDocumentProxy` read here. */
interface PdfDocument {
  getOptionalContentConfig(params: { intent: 'display' }): Promise<{ isVisible(props: unknown): boolean }>
}

/** A `FontFaceObject` in `commonObjs`: getters over pdf.js's font data, and a Type 3 font's procedures. */
interface PdfFont {
  loadedName: string
  missingFile?: boolean
  isType3Font?: boolean
  isInvalidPDFjsFont?: boolean
  vertical?: boolean
  remeasure?: boolean
  bold?: boolean
  black?: boolean
  italic?: boolean
  fallbackName?: string
  fontMatrix?: ArrayLike<number>
  bbox?: ArrayLike<number>
  charProcOperatorList?: ReadonlyMap<string, PdfOperatorList>
}

/** The page input, with what was read kept by id for a caller that stores it (the fixture capture). */
interface PageReading extends PageInput {
  read: {
    /**
     * By `commonObjs` id, the name `setFont` gives. A font with no file has another `loadedName`, its
     * family (`Helvetica` for `g_d0_f1`), which is what its CSS names.
     */
    fonts: ReadonlyMap<string, FontInput | string>
    /** By `commonObjs` id, `<loadedName>_path_<fontChar>`; a glyph with an empty outline is absent. */
    glyphBoxes: ReadonlyMap<string, Box4>
    /** The size delivered, which may be smaller than the PDF size in the op, and whether to smooth it. */
    images: ReadonlyMap<string, { width: number; height: number; interpolate: boolean }>
    shadings: ReadonlyMap<string, readonly unknown[]>
  }
}

/**
 * Outline boxes by `commonObjs` id, per document. pdf.js sends a glyph's outline once per document,
 * and its Canvas 2D renderer deletes the entry once it has built a `Path2D` from it, so a box read
 * for one page must outlive the entry for the next.
 */
const glyphBoxCache = new WeakMap<PdfObjects, Map<string, Box4 | null>>()

/** The stamp an object id is valid under: the document, pdf.js, the annotation mode and the renderer. */
export function documentStamp(fingerprint: string, pdfjsVersion: string, renderer: 'canvas2d' | 'webgpu'): string {
  return `${fingerprint}:${pdfjsVersion}:annot-enable:${renderer}`
}

export async function readPageInput(page: PdfPage, stamp: string, signal?: AbortSignal): Promise<PageReading> {
  const list = await page.getOperatorList({ intent: 'display' })
  signal?.throwIfAborted()

  // `g_` ids are document-wide, in `commonObjs`; the rest are the page's own (build/pdf.mjs 10019-10026).
  const resolved = async (id: string): Promise<unknown> => {
    const objs = id.startsWith('g_') ? page.commonObjs : page.objs
    const data = objs.has(id) ? objs.get(id) : await new Promise((resolve, reject) => objs.get(id, resolve, reject))
    signal?.throwIfAborted()
    return data
  }

  const fonts = new Map<string, FontInput | string>()
  const images = new Map<string, { width: number; height: number; interpolate: boolean }>()
  const shadings = new Map<string, readonly unknown[]>()
  const fontChars = new Set<string>()
  // Type 3 procedures and tiling cells are operator lists of their own, read like the page's.
  const lists: PdfOperatorList[] = [list]
  const readFont = async (id: string) => {
    if (fonts.has(id)) return
    const font = (await resolved(id)) as PdfFont | string
    if (typeof font === 'string') {
      fonts.set(id, font)
      return
    }
    const input: FontInput = {
      loadedName: font.loadedName,
      missingFile: font.missingFile === true,
      isType3Font: font.isType3Font === true,
      isInvalidPDFjsFont: font.isInvalidPDFjsFont === true,
      vertical: font.vertical === true,
      remeasure: font.remeasure === true,
      bold: font.bold === true,
      black: font.black === true,
      italic: font.italic === true,
      fallbackName: font.fallbackName ?? '',
    }
    if (font.fontMatrix) input.fontMatrix = Array.from(font.fontMatrix)
    if (font.bbox) input.bbox = Array.from(font.bbox)
    if (font.charProcOperatorList) {
      const procs = new Map<string, PdfOperatorList>()
      for (const [name, proc] of font.charProcOperatorList) {
        procs.set(name, { fnArray: proc.fnArray, argsArray: proc.argsArray })
        lists.push(proc)
      }
      input.type3Procs = procs
    }
    fonts.set(id, input)
  }
  const readShading = async (id: string) => {
    if (!shadings.has(id)) shadings.set(id, (await resolved(id)) as readonly unknown[])
  }
  const readImage = async (id: string) => {
    if (images.has(id)) return
    const { width, height, interpolate } = (await resolved(id)) as { width: number; height: number; interpolate?: boolean }
    images.set(id, { width, height, interpolate: interpolate === true })
  }

  for (let l = 0; l < lists.length; l++) {
    const { fnArray, argsArray } = lists[l]!
    for (let i = 0; i < fnArray.length; i++) {
      const args = argsArray[i] as unknown[] | null
      switch (fnArray[i]) {
        case OPS.dependency:
          for (const id of args as string[]) await resolved(id)
          break
        case OPS.setFont:
          await readFont(args![0] as string)
          break
        case OPS.setGState:
          for (const [key, value] of args![0] as [string, unknown][]) {
            if (key === 'Font') await readFont((value as [string, number])[0])
          }
          break
        case OPS.showText:
          for (const glyph of args![0] as unknown[]) {
            // Numbers are TJ adjustments; a glyph may carry a seac accent drawn as a second glyph.
            if (typeof glyph !== 'object' || glyph === null) continue
            const { fontChar, accent } = glyph as { fontChar: string; accent: { fontChar: string } | null }
            fontChars.add(fontChar)
            if (accent) fontChars.add(accent.fontChar)
          }
          break
        case OPS.paintImageXObject:
          await readImage(args![0] as string)
          break
        case OPS.paintImageMaskXObject: {
          // An image mask pdf.js could not trace inside a Type 3 glyph carries its data inline.
          const { data } = args![0] as { data: unknown }
          if (typeof data === 'string') await readImage(data)
          break
        }
        case OPS.shadingFill:
          await readShading(args![0] as string)
          break
        case OPS.setFillColorN:
        case OPS.setStrokeColorN:
          if (args![0] === 'Shading') await readShading(args![1] as string)
          else if (args![0] === 'TilingPattern') lists.push(args![2] as PdfOperatorList)
          break
      }
    }
  }

  // Which font each glyph was shown in depends on the text state at that op, so rather than replay
  // it, every outline font is paired with every character: pdf.js holds an outline only for a pair
  // it compiled, so a pair that was never shown finds nothing.
  let cache = glyphBoxCache.get(page.commonObjs)
  if (!cache) glyphBoxCache.set(page.commonObjs, (cache = new Map()))
  const glyphBoxes = new Map<string, Box4>()
  for (const font of fonts.values()) {
    if (typeof font === 'string' || font.missingFile || font.isType3Font) continue
    for (const fontChar of fontChars) {
      const id = `${font.loadedName}_path_${fontChar}`
      if (!cache.has(id) && page.commonObjs.has(id)) {
        // DrawOPS in em units with the font matrix applied; control points are in the hull.
        const { path } = page.commonObjs.get(id) as { path: ArrayLike<number> }
        const box: Box4 = [Infinity, Infinity, -Infinity, -Infinity]
        for (let i = 0; i < path.length;) {
          const op = path[i++]
          const coords = op === DRAW_OPS.curveTo ? 6 : op === DRAW_OPS.quadraticCurveTo ? 4 : op === DRAW_OPS.closePath ? 0 : 2
          for (let k = 0; k < coords; k += 2) {
            const x = path[i + k]!
            const y = path[i + k + 1]!
            box[0] = Math.min(box[0], x)
            box[1] = Math.min(box[1], y)
            box[2] = Math.max(box[2], x)
            box[3] = Math.max(box[3], y)
          }
          i += coords
        }
        cache.set(id, box[0] <= box[2] ? box : null)
      }
      const box = cache.get(id)
      if (box) glyphBoxes.set(id, box)
    }
  }

  const viewport = page.getViewport({ scale: 1 })
  return {
    page: page.pageNumber - 1,
    stamp,
    view: { width: viewport.width, height: viewport.height, transform: viewport.transform.slice(0, 6) as Mat6 },
    fnArray: list.fnArray,
    argsArray: list.argsArray,
    font: (id) => fonts.get(id) ?? null,
    glyphBounds: (loadedName, fontChar) => glyphBoxes.get(`${loadedName}_path_${fontChar}`) ?? null,
    image: (objId) => images.get(objId) ?? null,
    shading: (objId) => shadings.get(objId) ?? null,
    read: { fonts, glyphBoxes, images, shadings },
  }
}

/** `getTextContent` without Unicode normalisation, since the scene keeps each glyph's text as pdf.js gave it. */
export async function readTextItems(page: PdfPage): Promise<TextItem[]> {
  const { items } = await page.getTextContent({ disableNormalization: true })
  const text: TextItem[] = []
  for (const item of items) {
    // Without `includeMarkedContent` there are no marked-content items, but the type allows them.
    if ('str' in item) text.push({ str: item.str, transform: item.transform.slice() })
  }
  return text
}

/** Whether each optional-content props value is visible under the document's display configuration. */
export async function readOcVisibility(doc: PdfDocument, props: readonly unknown[]): Promise<Uint8Array> {
  const config = await doc.getOptionalContentConfig({ intent: 'display' })
  return Uint8Array.from(props, (p) => (config.isVisible(p) ? 1 : 0))
}
