/**
 * One page's layers in the Original view (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer,
 * *Render budget*, *Text*, *Links*): the base canvas, a capped page's detail canvas, the text
 * layer and the link layer, inside the page's content layer that the viewer provides.
 *
 * The viewer owns the page box, its size and the custom properties the layers are laid out
 * with (`--scale-factor` on the pane, `--user-unit` and `--total-scale-factor` on the page),
 * and decides when a page draws and when it lets go of its canvases. This class does the
 * drawing: a zoom is CSS until `draw` is called at the new scale, and the text and link layers
 * are built once and never again.
 */

import type { PageViewport, PDFPageProxy, RenderTask, TextLayer } from 'pdfjs-dist'
import type { TextContent } from 'pdfjs-dist/types/src/display/api'
import { backingSize, containsRect, detailCanvas, type CanvasLimits } from './budget'
import { CSS_UNITS, type Rect } from './layout'
import { externalUrl, linkBoxes, namedActionPage } from './links'
import { pdfFailure, type PdfDestination, type PdfDocumentHandle } from './pdfjs'
import { bindTextLayer } from './text-selection'

/** What a link is called when the PDF gives it no text, in the reader's language. */
export interface PageViewLabels {
  /** An internal link to page `page` (from 1): *第 {page} 頁*. */
  page(page: number): string
  /** An external link, by its URL's host. */
  external(host: string): string
}

export interface PageViewOptions {
  /** The page's light-scheme content layer (`.pdf-page-content`), sized by the viewer. */
  host: HTMLElement
  document: PdfDocumentHandle
  page: PDFPageProxy
  /** `canvasLimits(navigator)`. */
  limits: CanvasLimits
  labels: PageViewLabels
  /** An internal link or a named action was followed. */
  onNavigate(target: PdfDestination): void
  /** The device pixels this page's canvases hold changed (a swap, a release). */
  onPixels?(pixels: number): void
}

/** `drawn`: a canvas was swapped in. `unchanged`: nothing needed drawing. `cancelled`: a later
 *  call, a `cancel`, a `release` or a `destroy` stopped it, which is not an error. */
export type DrawOutcome = 'drawn' | 'unchanged' | 'cancelled'

/** What the base canvas was last drawn for. */
interface BaseState {
  scale: number
  dpr: number
  width: number
  height: number
  capped: boolean
}

function isCancel(error: unknown): boolean {
  const name = typeof error === 'object' && error !== null ? (error as { name?: unknown }).name : undefined
  return name === 'RenderingCancelledException' || name === 'AbortException'
}

function newCanvas(): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.className = 'pdf-canvas'
  // The page's accessible content is its text layer and links; the picture says nothing more.
  canvas.setAttribute('aria-hidden', 'true')
  return canvas
}

/** Lets a canvas's memory go now rather than when the collector gets to it, as pdf.js does. */
function zero(canvas: HTMLCanvasElement | null): void {
  if (canvas) {
    canvas.width = 0
    canvas.height = 0
    canvas.remove()
  }
}

export class PageView {
  readonly pageNumber: number

  readonly #options: PageViewOptions
  readonly #host: HTMLElement
  readonly #page: PDFPageProxy
  #base: HTMLCanvasElement | null = null
  #baseState: BaseState | null = null
  #detail: HTMLCanvasElement | null = null
  /** The part of the page the detail canvas covers, for the base it was drawn over. */
  #detailRect: Rect | null = null
  #detailFor: BaseState | null = null
  #tasks = new Set<RenderTask>()
  /** Bumped by every `draw`, `cancel` and `release`, so a draw that resumes late knows. */
  #generation = 0
  #painted = false
  #destroyed = false
  #layers: Layers | null = null
  #textReady: Promise<void> | null = null

  constructor(options: PageViewOptions) {
    this.#options = options
    this.#host = options.host
    this.#page = options.page
    this.pageNumber = options.page.pageNumber
  }

  /** Whether a base canvas shows (the page is no longer a blank placeholder). */
  get painted(): boolean {
    return this.#painted
  }

  /** Device pixels the page's canvases hold now, for the total budget. */
  get pixels(): number {
    return (this.#base ? this.#base.width * this.#base.height : 0) + (this.#detail ? this.#detail.width * this.#detail.height : 0)
  }

  /**
   * The text and link layers, settled once both are built. A failure (as a `PdfFailure`) leaves
   * the picture in place, so the viewer need not treat it as a page error; a cancel resolves.
   * Null until the first `draw`.
   */
  get textReady(): Promise<void> | null {
    return this.#textReady
  }

  /**
   * Draws what the page needs at `scale` (1 = 100%) and `dpr`, sized to the host's client box:
   * the base canvas when the scale, the resolution or the box changed (into a fresh canvas
   * swapped in when done, so the old, stretched picture stays meanwhile), then, on a capped
   * page, the detail canvas over `visible` (the part in view, in CSS px from the content
   * layer's top left; `layout.visibleRect`'s rect is close enough) when the part it last drew
   * no longer covers it. Calling it again with nothing new is cheap and draws nothing, so the
   * viewer can call it after every settle. A newer call cancels an older one still running.
   * Rejects with a `PdfFailure` for a failure other than a cancel.
   */
  async draw(scale: number, dpr: number, visible: Rect | null): Promise<DrawOutcome> {
    if (this.#destroyed) {
      return 'cancelled'
    }
    const width = this.#host.clientWidth
    const height = this.#host.clientHeight
    if (width === 0 || height === 0) {
      return 'unchanged'
    }
    this.cancel()
    const generation = this.#generation
    const viewport = this.#page.getViewport({ scale: scale * CSS_UNITS })
    this.#buildLayers(viewport)
    const { cap, maxDim } = this.#options.limits
    let outcome: DrawOutcome = 'unchanged'

    const drawn = this.#baseState
    const current =
      drawn && drawn.scale === scale && drawn.dpr === dpr && drawn.width === width && drawn.height === height ? drawn : null
    let state = current
    if (!state) {
      const backing = backingSize(width, height, dpr, cap, maxDim)
      const canvas = newCanvas()
      canvas.width = backing.width
      canvas.height = backing.height
      // The viewport is in unrounded px and the box in whole ones: stretch the one over the
      // other, so the picture fills the canvas exactly whatever the rounding.
      const transform = [backing.width / viewport.width, 0, 0, backing.height / viewport.height, 0, 0]
      if (!(await this.#render(canvas, viewport, transform, generation))) {
        return 'cancelled'
      }
      if (this.#base) {
        this.#base.replaceWith(canvas)
        zero(this.#base)
      } else {
        this.#host.prepend(canvas)
      }
      this.#base = canvas
      state = { scale, dpr, width, height, capped: backing.capped }
      this.#baseState = state
      if (!state.capped) {
        zero(this.#detail)
        this.#detail = null
        this.#detailRect = null
        this.#detailFor = null
      }
      this.#painted = true
      outcome = 'drawn'
      this.#options.onPixels?.(this.pixels)
    }

    const part = visible && clip(visible, width, height)
    if (state.capped && part && (this.#detailFor !== state || !this.#detailRect || !containsRect(this.#detailRect, part))) {
      const detail = detailCanvas(part, { width, height }, dpr, cap, maxDim)
      if (detail) {
        const canvas = newCanvas()
        canvas.classList.add('pdf-canvas-detail')
        canvas.width = detail.backing.width
        canvas.height = detail.backing.height
        // `detail.transform` maps CSS px to the detail's backing px; the viewport's px are not
        // quite CSS px (see above), so its scale takes their ratio too.
        const [sx, , , sy, dx, dy] = detail.transform
        const transform = [(sx * width) / viewport.width, 0, 0, (sy * height) / viewport.height, dx, dy]
        if (!(await this.#render(canvas, viewport, transform, generation))) {
          return 'cancelled'
        }
        Object.assign(canvas.style, {
          left: `${(100 * detail.left) / width}%`,
          top: `${(100 * detail.top) / height}%`,
          width: `${(100 * detail.width) / width}%`,
          height: `${(100 * detail.height) / height}%`,
        })
        if (this.#detail) {
          this.#detail.replaceWith(canvas)
          zero(this.#detail)
        } else if (this.#base) {
          this.#base.after(canvas)
        }
        this.#detail = canvas
        this.#detailRect = { left: detail.left, top: detail.top, width: detail.width, height: detail.height }
        this.#detailFor = state
        outcome = 'drawn'
        this.#options.onPixels?.(this.pixels)
      }
    }
    return outcome
  }

  /** Stops any draw in progress; what shows stays. */
  cancel(): void {
    this.#generation++
    for (const task of this.#tasks) {
      task.cancel()
    }
    this.#tasks.clear()
  }

  /**
   * Gives the canvases' memory back, for the total budget or as the page leaves the render
   * window: cancels any draw, sets each canvas's size to 0, removes it and lets pdf.js free the
   * page's drawing data. The text and link layers stay. The page is a white placeholder again
   * until its next `draw`.
   */
  release(): void {
    this.cancel()
    const had = this.pixels > 0
    zero(this.#base)
    zero(this.#detail)
    this.#base = null
    this.#detail = null
    this.#baseState = null
    this.#detailRect = null
    this.#detailFor = null
    this.#painted = false
    this.#page.cleanup()
    if (had) {
      this.#options.onPixels?.(0)
    }
  }

  /**
   * Takes every layer out of the host: the page has left the render window. If focus is on one
   * of its links, the viewer moves it to the page's `<section>` first. After this, `draw` does
   * nothing.
   */
  destroy(): void {
    if (this.#destroyed) {
      return
    }
    this.release()
    this.#destroyed = true
    if (this.#layers) {
      this.#layers.textLayer?.cancel()
      this.#layers.unbind?.()
      this.#layers.text.remove()
      this.#layers.links.remove()
      this.#layers = null
    }
  }

  /** Renders into `canvas`; false when the render was cancelled or overtaken. */
  async #render(canvas: HTMLCanvasElement, viewport: PageViewport, transform: number[], generation: number): Promise<boolean> {
    const task = this.#page.render({ canvas, viewport, transform })
    this.#tasks.add(task)
    try {
      await task.promise
    } catch (error) {
      zero(canvas)
      if (isCancel(error) || generation !== this.#generation) {
        return false
      }
      throw pdfFailure(error)
    } finally {
      this.#tasks.delete(task)
    }
    if (generation !== this.#generation || this.#destroyed) {
      zero(canvas)
      return false
    }
    return true
  }

  /** The text and link layers, started on the first draw and kept from then on. */
  #buildLayers(viewport: PageViewport): void {
    if (this.#layers) {
      return
    }
    const text = document.createElement('div')
    text.className = 'textLayer'
    // An empty `lang` until the file names its own, so a screen reader does not read an English
    // paper in the page's zh-Hant voice.
    text.lang = ''
    const links = document.createElement('div')
    links.className = 'pdf-link-layer'
    links.dataset.mainRotation = String(((this.#page.rotate % 360) + 360) % 360)
    this.#host.append(text, links)
    const layers: Layers = { text, links, textLayer: null, unbind: null }
    this.#layers = layers
    this.#textReady = Promise.all([this.#buildText(text, viewport, layers), this.#buildLinks(links)]).then(() => undefined)
    this.#textReady.catch(() => {})
  }

  async #buildText(container: HTMLDivElement, viewport: PageViewport, layers: Layers): Promise<void> {
    const { TextLayer, normalizeUnicode } = this.#options.document.pdfjs
    // The stream keeps the text arriving in chunks, and is read through on its way to pdf.js
    // only to learn the file's `/Lang`, which `TextLayer` keeps to itself.
    let langSeen = false
    const source = this.#page.streamTextContent({ includeMarkedContent: true, disableNormalization: true }).pipeThrough(
      new TransformStream<TextContent, TextContent>({
        transform(chunk, controller) {
          if (!langSeen && chunk.lang) {
            langSeen = true
            container.lang = chunk.lang
          }
          controller.enqueue(chunk)
        },
      }),
    )
    const textLayer = new TextLayer({ textContentSource: source, container, viewport })
    layers.textLayer = textLayer
    try {
      await textLayer.render()
    } catch (error) {
      if (isCancel(error) || this.#destroyed) {
        return
      }
      throw pdfFailure(error)
    }
    if (this.#destroyed) {
      return
    }
    const end = document.createElement('div')
    end.className = 'endOfContent'
    container.append(end)
    layers.unbind = bindTextLayer(container, end, normalizeUnicode as (text: string) => string)
  }

  async #buildLinks(layer: HTMLDivElement): Promise<void> {
    const { document: handle, labels, onNavigate } = this.#options
    let annotations: unknown[]
    try {
      annotations = await this.#page.getAnnotations({ intent: 'display' })
    } catch (error) {
      if (this.#destroyed || handle.destroyed) {
        return
      }
      throw pdfFailure(error)
    }
    const view = this.#page.view
    const index = this.pageNumber - 1
    const built = await Promise.all(
      annotations.map(async (annotation) => {
        const data = annotation as LinkData
        if (data.annotationType !== handle.pdfjs.AnnotationType.LINK || !data.rect) {
          return null
        }
        // pdf.js binds the first of these a link has, in this order; only a web address, a
        // named action and a destination are followed, and anything else is no link at all.
        if (data.url !== undefined) {
          const url = externalUrl(data.url)
          return url ? { data, href: url.href, name: data.overlaidText || labels.external(url.host), go: null } : null
        }
        if (data.action !== undefined) {
          const target = namedActionPage(data.action, index, handle.numPages)
          if (target === null) {
            return null
          }
          const go = () => onNavigate({ page: target + 1, top: null, fraction: null })
          return { data, href: '#', name: data.overlaidText || labels.page(target + 1), go }
        }
        if (data.attachment !== undefined || data.setOCGState !== undefined || data.dest === undefined || data.dest === null) {
          return null
        }
        const dest = data.dest
        const page = data.overlaidText ? null : await handle.destinationPage(dest)
        if (!data.overlaidText && page === null) {
          return null
        }
        const go = async () => {
          const target = await handle.resolveDestination(dest)
          if (target && !this.#destroyed) {
            onNavigate(target)
          }
        }
        return { data, href: '#', name: data.overlaidText || labels.page(page ?? 0), go }
      }),
    )
    if (this.#destroyed) {
      return
    }
    for (const link of built) {
      if (!link) {
        continue
      }
      const boxes = linkBoxes(link.data.rect!, link.data.quadPoints, view)
      boxes.forEach((box, at) => {
        const anchor = document.createElement('a')
        anchor.href = link.href
        Object.assign(anchor.style, {
          left: `${box.left}%`,
          top: `${box.top}%`,
          width: `${box.width}%`,
          height: `${box.height}%`,
        })
        if (at === 0) {
          anchor.title = link.name
        } else {
          // A link over several lines is one stop for the keyboard and one name for a screen
          // reader; its other boxes only catch the pointer.
          anchor.tabIndex = -1
          anchor.setAttribute('aria-hidden', 'true')
        }
        if (link.go) {
          const go = link.go
          anchor.addEventListener('click', (event) => {
            event.preventDefault()
            void go()
          })
        } else {
          anchor.target = '_blank'
          anchor.rel = 'noopener noreferrer'
        }
        layer.append(anchor)
      })
    }
  }
}

/** A page's text and link layers, and what takes them down. */
interface Layers {
  text: HTMLDivElement
  links: HTMLDivElement
  textLayer: TextLayer | null
  unbind: (() => void) | null
}

/** The fields of a link annotation this reads (build/pdf.worker.mjs, `LinkAnnotation`). */
interface LinkData {
  annotationType?: number
  rect?: number[]
  quadPoints?: ArrayLike<number> | null
  url?: unknown
  action?: unknown
  attachment?: unknown
  setOCGState?: unknown
  dest?: unknown
  overlaidText?: string
}

function clip(rect: Rect, width: number, height: number): Rect | null {
  const left = Math.max(0, rect.left)
  const top = Math.max(0, rect.top)
  const right = Math.min(width, rect.left + rect.width)
  const bottom = Math.min(height, rect.top + rect.height)
  return right > left && bottom > top ? { left, top, width: right - left, height: bottom - top } : null
}
