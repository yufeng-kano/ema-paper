/**
 * Where every page of the Original view stands (docs/pdf-viewer.md § Stage 1: the Canvas 2D
 * viewer, *Layout and scrolling*): one vertical column of page boxes, their sizes from the PDF
 * and the zoom, and the questions the viewer asks of it per scroll frame (which pages show,
 * which one is current), per size change (how far to correct the scroll) and per jump (where a
 * page or a destination lands).
 *
 * Framework-free and pdf.js-free: the engine hands in page sizes as plain numbers, so vitest
 * covers it in node (docs/testing.md § Frontend). Pages are counted from 0 here, as an index;
 * the viewer adds 1 where a reader sees a page number.
 *
 * The column the model describes is `layout.width` wide (the widest page plus a side gutter on
 * each side) and at least the pane's width, with each page centred in the pane's width (a page
 * wider than that starts one gutter from the left), `spacing.padding` above the first page and
 * below the last, and `spacing.gap` between pages. The viewer's CSS has to draw exactly that,
 * with each page's box set inline from `widths` and `heights`.
 */

/** CSS px per PDF point at 100%: 1 pt = 1/72 in, 1 CSS px = 1/96 in
 *  (pdf.js's `PixelsPerInch.PDF_TO_CSS_UNITS`). */
export const CSS_UNITS = 96 / 72

/** How long the pane has to be quiet before the work a moving pane would waste runs: the
 *  pinch commit, the size correction on touch and the detail canvas redraw. */
export const SETTLE_MS = 150

/** A page's size in PDF points with its `/Rotate` and `userUnit` applied: what
 *  `page.getViewport({ scale: 1 })` reports as `width` and `height`. */
export interface PageSize {
  width: number
  height: number
}

export interface LayoutSpacing {
  /** Between two pages. */
  gap: number
  /** Above the first page and below the last. */
  padding: number
  /** Left and right of the widest page. */
  gutter: number
}

/** Desktop: a centred column, 16px between pages, above page 1 and at either side. */
export const DESKTOP_SPACING: LayoutSpacing = { gap: 16, padding: 16, gutter: 16 }

/** Phone: each page the pane's full width, 10px between pages and at the ends. */
export const PHONE_SPACING: LayoutSpacing = { gap: 10, padding: 10, gutter: 0 }

export interface Size {
  width: number
  height: number
}

/** A box in CSS px. */
export interface Rect {
  left: number
  top: number
  width: number
  height: number
}

/** A scroll position, in CSS px from the column's top left. */
export interface ScrollPoint {
  left: number
  top: number
}

/** Pages `first` to `last`, both included. */
export interface PageRange {
  first: number
  last: number
}

/**
 * The column at one zoom. Typed arrays, one slot per page, so a document of a few thousand
 * pages costs a few small allocations per zoom and none per scroll frame. `setPageSizes`
 * changes a layout in place; a zoom makes a new one with `scaleLayout`, so the old layout
 * stays valid for the anchor arithmetic of the commit.
 */
export interface Layout {
  readonly scale: number
  readonly spacing: LayoutSpacing
  readonly count: number
  /** Each page's size in points; an unread page holds page 1's. */
  readonly pointWidths: Float64Array
  readonly pointHeights: Float64Array
  /** 1 once a page's real size is known, 0 while it is a placeholder. */
  readonly measured: Uint8Array
  /** Each page's box in whole CSS px. */
  readonly widths: Float64Array
  readonly heights: Float64Array
  /** Each page box's top, from the column's top. */
  readonly tops: Float64Array
  /** The column: the widest page plus both gutters. */
  width: number
  /** The column: every page, every gap and both paddings. */
  height: number
}

/**
 * A page box's side in whole CSS px. The small allowance keeps a fit-width scale, which is the
 * pane's width divided by the page's, from flooring a pixel short of the pane on a rounding
 * error.
 */
export function boxPx(points: number, scale: number): number {
  return Math.max(1, Math.floor(points * scale * CSS_UNITS + 1e-6))
}

function sizeBox(layout: Layout, index: number): void {
  layout.widths[index] = boxPx(layout.pointWidths[index]!, layout.scale)
  layout.heights[index] = boxPx(layout.pointHeights[index]!, layout.scale)
}

function stack(layout: Layout): void {
  const { gap, padding, gutter } = layout.spacing
  let top = padding
  let widest = 0
  for (let index = 0; index < layout.count; index++) {
    layout.tops[index] = top
    top += layout.heights[index]! + gap
    widest = Math.max(widest, layout.widths[index]!)
  }
  layout.width = widest + 2 * gutter
  layout.height = top - gap + padding
}

function build(
  scale: number,
  spacing: LayoutSpacing,
  pointWidths: Float64Array,
  pointHeights: Float64Array,
  measured: Uint8Array,
): Layout {
  const count = pointWidths.length
  const layout: Layout = {
    scale,
    spacing,
    count,
    pointWidths,
    pointHeights,
    measured,
    widths: new Float64Array(count),
    heights: new Float64Array(count),
    tops: new Float64Array(count),
    width: 0,
    height: 0,
  }
  for (let index = 0; index < count; index++) {
    sizeBox(layout, index)
  }
  stack(layout)
  return layout
}

/** The column once page 1's size is known: page 1 measured, every other page a placeholder
 *  at page 1's size. `count` is at least 1. */
export function createLayout(count: number, first: PageSize, scale: number, spacing: LayoutSpacing): Layout {
  const measured = new Uint8Array(count)
  measured[0] = 1
  return build(
    scale,
    spacing,
    new Float64Array(count).fill(first.width),
    new Float64Array(count).fill(first.height),
    measured,
  )
}

/** The same pages at another zoom (or spacing, when the pane turns from desktop to phone). */
export function scaleLayout(layout: Layout, scale: number, spacing: LayoutSpacing = layout.spacing): Layout {
  return build(scale, spacing, layout.pointWidths.slice(), layout.pointHeights.slice(), layout.measured.slice())
}

/** One page's size in points, measured or placeholder. */
export function pageSize(layout: Layout, index: number): PageSize {
  return { width: layout.pointWidths[index]!, height: layout.pointHeights[index]! }
}

export interface PageSizeChange {
  index: number
  size: PageSize
}

/**
 * Records real page sizes, in place, and returns how far to move `scrollTop` (add it) in the
 * same frame so that what the reader sees does not move. A page whose top was above the
 * pane's top shifts everything under it, so its change in height is compensated; a page at or
 * below the pane's top changes in place. On touch the caller holds the changes until the pane
 * has settled and applies them together, which this takes as one batch.
 */
export function setPageSizes(layout: Layout, changes: readonly PageSizeChange[], scrollTop: number): number {
  let correction = 0
  for (const { index, size } of changes) {
    const before = layout.heights[index]!
    layout.pointWidths[index] = size.width
    layout.pointHeights[index] = size.height
    layout.measured[index] = 1
    sizeBox(layout, index)
    if (layout.tops[index]! < scrollTop) {
      correction += layout.heights[index]! - before
    }
  }
  if (changes.length > 0) {
    stack(layout)
  }
  return correction
}

/** The last page whose top is at or above `y`: the page under `y`, or the one above the gap
 *  `y` falls in. Page 0 above the first page. A binary search, for every scroll frame. */
export function pageAt(layout: Layout, y: number): number {
  let low = 0
  let high = layout.count - 1
  while (low < high) {
    const middle = (low + high + 1) >> 1
    if (layout.tops[middle]! <= y) {
      low = middle
    } else {
      high = middle - 1
    }
  }
  return low
}

/** The pages that cross the band from `top` to `bottom`, or null when the band lies in a gap
 *  or outside the column. */
export function visibleRange(layout: Layout, top: number, bottom: number): PageRange | null {
  let first = pageAt(layout, top)
  if (layout.tops[first]! + layout.heights[first]! <= top) {
    first++
  }
  let last = pageAt(layout, bottom)
  if (layout.tops[last]! >= bottom) {
    last--
  }
  return first <= last && first < layout.count ? { first, last } : null
}

/** The page filling the most of the pane's height; a tie goes to the lower number. */
export function currentPage(layout: Layout, scrollTop: number, paneHeight: number): number {
  const bottom = scrollTop + paneHeight
  const range = visibleRange(layout, scrollTop, bottom)
  if (!range) {
    return pageAt(layout, scrollTop)
  }
  let best = range.first
  let most = -1
  for (let index = range.first; index <= range.last; index++) {
    const top = layout.tops[index]!
    const shown = Math.min(bottom, top + layout.heights[index]!) - Math.max(scrollTop, top)
    if (shown > most) {
      best = index
      most = shown
    }
  }
  return best
}

/** The pages that hold layers: those crossing the pane, and one more on each side. */
export function renderWindow(layout: Layout, scrollTop: number, paneHeight: number): PageRange {
  const range = visibleRange(layout, scrollTop, scrollTop + paneHeight)
  const first = range ? range.first : pageAt(layout, scrollTop)
  const last = range ? range.last : first
  return { first: Math.max(0, first - 1), last: Math.min(layout.count - 1, last + 1) }
}

/** How far a page is from the pane's middle, with every page in view ahead of every page out
 *  of it: the order to draw or read pages in. */
function distance(layout: Layout, index: number, scrollTop: number, paneHeight: number): number {
  const top = layout.tops[index]!
  const bottom = top + layout.heights[index]!
  const away = Math.abs((top + bottom) / 2 - (scrollTop + paneHeight / 2))
  const shown = bottom > scrollTop && top < scrollTop + paneHeight
  return shown ? away : layout.height + away
}

/** The render window's pages in the order to draw them, one `page.render` at a time: pages in
 *  view first, nearest the pane's middle first. */
export function renderQueue(layout: Layout, scrollTop: number, paneHeight: number): number[] {
  const { first, last } = renderWindow(layout, scrollTop, paneHeight)
  const queue: number[] = []
  for (let index = first; index <= last; index++) {
    queue.push(index)
  }
  return queue.sort((a, b) => distance(layout, a, scrollTop, paneHeight) - distance(layout, b, scrollTop, paneHeight))
}

/** The placeholder pages to read a size for (`getPage`): those within one pane height of the
 *  pane and those in the render window, nearest first. */
export function pagesToMeasure(layout: Layout, scrollTop: number, paneHeight: number): number[] {
  const near = visibleRange(layout, scrollTop - paneHeight, scrollTop + 2 * paneHeight)
  const drawn = renderWindow(layout, scrollTop, paneHeight)
  const first = Math.min(drawn.first, near ? near.first : drawn.first)
  const last = Math.max(drawn.last, near ? near.last : drawn.last)
  const pages: number[] = []
  for (let index = first; index <= last; index++) {
    if (!layout.measured[index]) {
      pages.push(index)
    }
  }
  return pages.sort((a, b) => distance(layout, a, scrollTop, paneHeight) - distance(layout, b, scrollTop, paneHeight))
}

/** The `scrollTop` that puts a page's top one gap below the pane's top. */
export function jumpOffset(layout: Layout, index: number): number {
  return Math.max(0, layout.tops[index]! - layout.spacing.gap)
}

/** The `scrollTop` for a link's destination: its top (a fraction of the page's height from the
 *  page's top, from `destinationFraction`) lands where a jump puts a page's top; without one,
 *  the page's own top does. */
export function destinationOffset(layout: Layout, index: number, fraction: number | null): number {
  if (fraction === null) {
    return jumpOffset(layout, index)
  }
  return Math.max(0, layout.tops[index]! + fraction * layout.heights[index]! - layout.spacing.gap)
}

/**
 * A page's box in the column, which is at least `paneWidth` wide. Every page is centred in the
 * pane's width, as pdf.js's viewer centres it, not in the column: one landscape page wider than
 * the pane must not push the portrait pages off its centre (on a phone, half off the screen).
 */
export function pageRect(layout: Layout, index: number, paneWidth: number): Rect {
  const width = layout.widths[index]!
  return {
    left: Math.max(layout.spacing.gutter, (paneWidth - width) / 2),
    top: layout.tops[index]!,
    width,
    height: layout.heights[index]!,
  }
}

/** The part of a page the pane shows, in CSS px from the page's top left, or null when none of
 *  it shows: where a capped page's detail canvas goes. */
export function visibleRect(layout: Layout, index: number, scroll: ScrollPoint, pane: Size): Rect | null {
  const page = pageRect(layout, index, pane.width)
  const left = Math.max(scroll.left, page.left)
  const top = Math.max(scroll.top, page.top)
  const right = Math.min(scroll.left + pane.width, page.left + page.width)
  const bottom = Math.min(scroll.top + pane.height, page.top + page.height)
  if (right <= left || bottom <= top) {
    return null
  }
  return { left: left - page.left, top: top - page.top, width: right - left, height: bottom - top }
}
