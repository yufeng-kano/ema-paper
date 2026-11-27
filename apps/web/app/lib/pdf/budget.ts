/**
 * How many device pixels the Original view's canvases may hold (docs/pdf-viewer.md § Stage 1:
 * the Canvas 2D viewer, *Render budget*): the cap on one page's canvas, a capped page's detail
 * canvas over the part in view, and which canvases give their memory up when all of them
 * together pass the total.
 *
 * The arithmetic is pdf.js's own, `OutputScale#limitCanvas` and `PDFPageDetailView#update`
 * (web/pdf_viewer.mjs, Apache-2.0), ported to plain numbers so vitest covers it in node.
 */

import type { PageRange, Rect, Size } from './layout'

/** One page canvas's cap on iOS, iPadOS and Android, pdf.js's own value for them. */
export const MOBILE_CANVAS_CAP = 5_242_880

/** One page canvas's cap elsewhere (4096²). */
export const DESKTOP_CANVAS_CAP = 16_777_216

/** The longest side a canvas may have, as pdf.js's `maxCanvasDim`. */
export const MAX_CANVAS_DIM = 32_767

/** What of `navigator` the platform is read from. */
export interface PlatformLike {
  userAgent: string
  platform: string
  maxTouchPoints: number
}

export interface CanvasLimits {
  /** Device pixels one page canvas may hold. */
  cap: number
  /** Device pixels every live canvas together may hold: 3 caps on iOS, 4 elsewhere. */
  total: number
  maxDim: number
}

/** The limits for this device, by pdf.js's `isIOS` and `isAndroid` tests (an iPad asks for the
 *  desktop site and says MacIntel, so it is told by its touch points). */
export function canvasLimits(platform: PlatformLike): CanvasLimits {
  const ios =
    /\b(?:iPad|iPhone|iPod)(?=;)/.test(platform.userAgent) ||
    (platform.platform === 'MacIntel' && platform.maxTouchPoints > 1)
  const mobile = ios || /Android/.test(platform.userAgent)
  const cap = mobile ? MOBILE_CANVAS_CAP : DESKTOP_CANVAS_CAP
  return { cap, total: (ios ? 3 : 4) * cap, maxDim: MAX_CANVAS_DIM }
}

/** A canvas's backing store for a box of CSS px, and the scale from CSS px to it. */
export interface Backing {
  width: number
  height: number
  /** Backing px per CSS px: the render transform is `[sx, 0, 0, sy, 0, 0]`. */
  sx: number
  sy: number
  /** Drawn below the device's resolution to stay under the cap, and stretched by CSS. */
  capped: boolean
}

/**
 * The backing store for a box `width` × `height` CSS px at `dpr`, cut down so that it holds at
 * most `cap` pixels and no side passes `maxDim`, as `limitCanvas` does. Flooring keeps both
 * bounds: the scale is the largest that keeps the area and the sides inside them.
 */
export function backingSize(width: number, height: number, dpr: number, cap: number, maxDim = MAX_CANVAS_DIM): Backing {
  const limit = Math.min(Math.sqrt(cap / (width * height)), maxDim / width, maxDim / height)
  const capped = dpr > limit
  const scale = capped ? limit : dpr
  const backingWidth = Math.max(1, Math.floor(width * scale))
  const backingHeight = Math.max(1, Math.floor(height * scale))
  return { width: backingWidth, height: backingHeight, sx: backingWidth / width, sy: backingHeight / height, capped }
}

/** A capped page's detail canvas: where it lies on the page (CSS px from the page's top left),
 *  its backing store, and the `transform` to pass to `page.render`. */
export interface DetailCanvas extends Rect {
  backing: Backing
  transform: [number, number, number, number, number, number]
}

/**
 * The detail canvas for the part of a page the pane shows (`visible`, from `visibleRect`):
 * that part plus a margin of up to its own size on each side, as much as the cap leaves room
 * for, kept inside the page. When the part in view alone passes the cap the detail is drawn
 * below the device's resolution rather than over the cap.
 */
export function detailCanvas(visible: Rect, page: Size, dpr: number, cap: number, maxDim = MAX_CANVAS_DIM): DetailCanvas | null {
  if (visible.width <= 0 || visible.height <= 0) {
    return null
  }
  const ratio = Math.sqrt(cap / (visible.width * visible.height * dpr * dpr))
  const overflow = Math.min(1, Math.max(0, (ratio - 1) / 2))
  const left = Math.max(0, visible.left - visible.width * overflow)
  const top = Math.max(0, visible.top - visible.height * overflow)
  const right = Math.min(page.width, visible.left + visible.width * (1 + overflow))
  const bottom = Math.min(page.height, visible.top + visible.height * (1 + overflow))
  const width = right - left
  const height = bottom - top
  if (width <= 0 || height <= 0) {
    return null
  }
  const backing = backingSize(width, height, dpr, cap, maxDim)
  return {
    left,
    top,
    width,
    height,
    backing,
    transform: [backing.sx, 0, 0, backing.sy, -left * backing.sx, -top * backing.sy],
  }
}

/** Whether a drawn detail canvas still covers the part in view; when it does not, once the
 *  pane settles, it is drawn again. */
export function containsRect(outer: Rect, inner: Rect): boolean {
  return (
    inner.left >= outer.left &&
    inner.top >= outer.top &&
    inner.left + inner.width <= outer.left + outer.width &&
    inner.top + inner.height <= outer.top + outer.height
  )
}

/** A live canvas: a page's base or detail canvas, and the device pixels it holds. */
export interface LiveCanvas {
  index: number
  pixels: number
}

/**
 * The canvases to release, in order, so that the rest fit `total`: the furthest pages from
 * `visible` first, a page above it before one the same distance below (the reader is more
 * likely reading on). A page in view keeps its canvases even over the total, so passing the
 * canvas about to be drawn with the live ones tells whether it can be drawn at all.
 */
export function canvasesToRelease<T extends LiveCanvas>(live: readonly T[], visible: PageRange, total: number): T[] {
  let held = 0
  for (const canvas of live) {
    held += canvas.pixels
  }
  if (held <= total) {
    return []
  }
  const away = (index: number) => (index < visible.first ? visible.first - index + 0.5 : index > visible.last ? index - visible.last : 0)
  const candidates = live.filter((canvas) => away(canvas.index) > 0).sort((a, b) => away(b.index) - away(a.index))
  const released: T[] = []
  for (const canvas of candidates) {
    if (held <= total) {
      break
    }
    released.push(canvas)
    held -= canvas.pixels
  }
  return released
}
