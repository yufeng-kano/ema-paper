/**
 * The Original view's zoom (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Zoom*): the
 * modes and limits, the ladder the buttons and keys step through, the fit scales, what a menu choice, a
 * pinch or a double tap resolves to, and where the pane scrolls so the point under the pointer
 * (or the pane's centre) stays put when a zoom commits.
 *
 * Scale 1 is 100%: one PDF point is 96/72 CSS px. Framework-free and pdf.js-free, for the
 * viewer component and ResultViewer's header alike.
 */

import { boxPx, CSS_UNITS, DESKTOP_SPACING, PHONE_SPACING, pageAt, pageRect } from './layout'
import type { Layout, PageSize, ScrollPoint, Size } from './layout'

export type ZoomMode = 'fit-width' | 'fit-page' | 'custom'

/** The zoom as it stands: how it was chosen, and the scale that came out (1 = 100%). A fit mode
 *  refits when the pane or the current page changes; `custom` keeps its scale. */
export interface ZoomState {
  mode: ZoomMode
  scale: number
}

/** A zoom asked for: by a menu, a key, a button or the phone's More. */
export type ZoomTarget =
  | { mode: 'fit-width' }
  | { mode: 'fit-page' }
  /** Absolute, 1 = 100%. */
  | { mode: 'custom'; scale: number }
  /** `times` × fit width, the phone's zoom group. */
  | { mode: 'multiple'; times: number }

/** What the viewer tells ResultViewer, which draws the zoom controls, on every change. */
export interface PdfViewerState {
  /** Page 1's size is known and no password or error state shows: the zoom controls work. */
  ready: boolean
  zoom: ZoomState
  /** The current page's fit-width scale, which the phone's multiples are measured against. */
  fitWidth: number | null
  canZoomIn: boolean
  canZoomOut: boolean
  /** The current page, from 1; 0 before the document is open. */
  page: number
  /** 0 before the document is open. */
  pages: number
}

/** The desktop range, 25% to 500%. */
export const MIN_SCALE = 0.25
export const MAX_SCALE = 5

/** The rungs − and + (and the keys) step through on desktop. */
export const ZOOM_STEPS: readonly number[] = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4, 5]

/** The zoom menu's percentages, after *Fit width* and *Fit page* and a separator. */
export const ZOOM_PRESETS: readonly number[] = [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4]

/** The phone's More zoom group after *Fit width*, and its ladder with fit width (1×) below
 *  them: a phone never zooms below fit width or past 5×. */
export const PHONE_MULTIPLES: readonly number[] = [2, 3, 5]

/** A double tap toggles between fit width and this. */
export const DOUBLE_TAP_MULTIPLE = 2

/** What every zoom is measured from: the pane's client size and the current page's size in
 *  points (page 1's when the document opens). */
export interface ZoomContext {
  phone: boolean
  pane: Size
  page: PageSize
}

/** The scale at which the current page fills the pane's width less the side gutters. */
export function fitWidthScale(context: ZoomContext): number {
  const { gutter } = context.phone ? PHONE_SPACING : DESKTOP_SPACING
  return Math.max(1, context.pane.width - 2 * gutter) / (context.page.width * CSS_UNITS)
}

/** The scale at which the whole current page fits the pane, a gap clear of each edge. */
export function fitPageScale(context: ZoomContext): number {
  const { gap } = context.phone ? PHONE_SPACING : DESKTOP_SPACING
  const height = Math.max(1, context.pane.height - 2 * gap) / (context.page.height * CSS_UNITS)
  return Math.min(fitWidthScale(context), height)
}

/** 25% to 500% on desktop; fit width to 5× fit width on a phone. */
export function zoomLimits(context: ZoomContext): { min: number; max: number } {
  if (!context.phone) {
    return { min: MIN_SCALE, max: MAX_SCALE }
  }
  const fit = fitWidthScale(context)
  return { min: fit, max: PHONE_MULTIPLES[PHONE_MULTIPLES.length - 1]! * fit }
}

/** A scale as a whole percentage, which is what a label shows and what two zooms are compared
 *  by: two scales a reader cannot tell apart are the same zoom. Format `percentOf(s) / 100`
 *  with `Intl.NumberFormat(locale, { style: 'percent' })`. */
export function percentOf(scale: number): number {
  return Math.round(scale * 100)
}

/** A scale the reader chose, inside the limits. On a phone a scale at the floor is fit width
 *  itself, so it keeps fitting when the phone turns. */
function custom(scale: number, context: ZoomContext): ZoomState {
  const { min, max } = zoomLimits(context)
  const clamped = Math.min(max, Math.max(min, scale))
  if (context.phone && percentOf(clamped) <= percentOf(min)) {
    return { mode: 'fit-width', scale: min }
  }
  return { mode: 'custom', scale: clamped }
}

/** What a menu choice, a key or the phone's More comes to. */
export function resolveZoom(target: ZoomTarget, context: ZoomContext): ZoomState {
  const { min, max } = zoomLimits(context)
  switch (target.mode) {
    case 'fit-width':
      return { mode: 'fit-width', scale: Math.min(max, Math.max(min, fitWidthScale(context))) }
    case 'fit-page':
      return { mode: 'fit-page', scale: Math.min(max, Math.max(min, fitPageScale(context))) }
    case 'custom':
      return custom(target.scale, context)
    case 'multiple':
      return custom(target.times * fitWidthScale(context), context)
  }
}

/** The zoom after the pane is resized or the current page changes: a fit mode fits again, a
 *  custom scale stays within the (phone's moving) limits. */
export function refitZoom(zoom: ZoomState, context: ZoomContext): ZoomState {
  return zoom.mode === 'custom' ? custom(zoom.scale, context) : resolveZoom({ mode: zoom.mode }, context)
}

/**
 * The zoom a document opens at, measured from page 1. A zoom carried over from the last visit
 * to the Original view is kept (refitted). Otherwise a phone opens at fit width, and a desktop
 * at 100% if page 1 fits the pane's width at 100% with its gutters, at fit width if not.
 */
export function openingZoom(context: ZoomContext, initial: ZoomState | null): ZoomState {
  if (initial) {
    return refitZoom(initial, context)
  }
  if (!context.phone && boxPx(context.page.width, 1) + 2 * DESKTOP_SPACING.gutter <= context.pane.width) {
    return { mode: 'custom', scale: 1 }
  }
  return resolveZoom({ mode: 'fit-width' }, context)
}

/**
 * The next rung above or below the current scale: the desktop ladder, or 1×, 2×, 3× and 5× fit
 * width on a phone. Rungs are compared as whole percentages, so a fit-width scale of 99.6% goes
 * up to 125%, not to a 100% that looks the same; past the last rung the limit is the step.
 */
export function stepZoom(zoom: ZoomState, direction: 'in' | 'out', context: ZoomContext): ZoomState {
  const { min, max } = zoomLimits(context)
  const fit = fitWidthScale(context)
  const rungs = context.phone ? [1, ...PHONE_MULTIPLES].map((times) => times * fit) : ZOOM_STEPS
  const now = percentOf(zoom.scale)
  const scale =
    direction === 'in'
      ? (rungs.find((rung) => percentOf(rung) > now) ?? max)
      : (rungs.findLast((rung) => percentOf(rung) < now) ?? min)
  return custom(scale, context)
}

/** Whether − or + would change what the reader sees: `canZoomIn` / `canZoomOut`. */
export function canStepZoom(zoom: ZoomState, direction: 'in' | 'out', context: ZoomContext): boolean {
  return percentOf(stepZoom(zoom, direction, context).scale) !== percentOf(zoom.scale)
}

/** A pinch that ends at `ratio` times the zoom it started from, inside the limits. During the
 *  pinch, `zoomBy(zoom, ratio, context).scale / zoom.scale` is the transform to show. */
export function zoomBy(zoom: ZoomState, ratio: number, context: ZoomContext): ZoomState {
  return custom(zoom.scale * ratio, context)
}

/** A double tap on a phone: fit width goes to 2×, anything else back to fit width. */
export function doubleTapZoom(zoom: ZoomState, context: ZoomContext): ZoomState {
  return zoom.mode === 'fit-width'
    ? custom(DOUBLE_TAP_MULTIPLE * fitWidthScale(context), context)
    : resolveZoom({ mode: 'fit-width' }, context)
}

/**
 * Whether a menu item is the current zoom (`menuitemradio`'s checked). A fit mode checks only
 * its own item, so 100% is not checked while fit width happens to come to 100%; a percentage
 * checks when the custom scale shows as that percentage; a scale between presets checks
 * nothing. A phone multiple compares with `fitWidth` × times, and 1× is fit width.
 */
export function isZoomChecked(zoom: ZoomState, target: ZoomTarget, fitWidth: number | null): boolean {
  switch (target.mode) {
    case 'fit-width':
    case 'fit-page':
      return zoom.mode === target.mode
    case 'custom':
      return zoom.mode === 'custom' && percentOf(zoom.scale) === percentOf(target.scale)
    case 'multiple':
      if (target.times === 1) {
        return zoom.mode === 'fit-width'
      }
      return zoom.mode === 'custom' && fitWidth !== null && Math.abs(zoom.scale / fitWidth - target.times) < 0.005
  }
}

/**
 * Where to scroll when a zoom commits so the point of the document under `anchor` (in pane
 * client px: the pointer, the pinch's midpoint, or the pane's centre for a button or key)
 * stays under it. The point is found in `before` as a fraction of its page and put back at the
 * same fraction of that page in `after`; above the first page and in a gap, where nothing
 * scales, it keeps its distance in px. The result is inside the column's scroll range, so at
 * an edge the anchor gives way rather than the pane over-scrolling.
 */
export function anchoredScroll(
  before: Layout,
  after: Layout,
  scroll: ScrollPoint,
  anchor: { x: number; y: number },
  pane: Size,
): ScrollPoint {
  const x = scroll.left + anchor.x
  const y = scroll.top + anchor.y
  const index = pageAt(before, y)
  const from = pageRect(before, index, pane.width)
  const to = pageRect(after, index, pane.width)
  let top: number
  if (y < from.top) {
    top = y
  } else if (y <= from.top + from.height) {
    top = to.top + ((y - from.top) / from.height) * to.height
  } else {
    top = to.top + to.height + (y - from.top - from.height)
  }
  const left = to.left + ((x - from.left) / from.width) * to.width
  return {
    left: Math.min(Math.max(0, Math.max(after.width, pane.width) - pane.width), Math.max(0, left - anchor.x)),
    top: Math.min(Math.max(0, after.height - pane.height), Math.max(0, top - anchor.y)),
  }
}
