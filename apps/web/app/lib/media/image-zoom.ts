/**
 * The image view's zoom (docs/media-viewer.md § The image view, *Zoom*): fit to window, the
 * range, what a menu choice, the ladder, a pinch, a double click or a double tap resolves to,
 * a resize, which choice is checked; and its geometry: the image box, where it sits on the
 * scrolling stage, and the scroll that keeps a point under the anchor when a zoom commits.
 *
 * Scale 1 is 100%: one image pixel per CSS pixel. It mirrors `lib/pdf/zoom.ts`'s shapes rather
 * than sharing them, since an image has one fit mode, its phone multiples measure against that
 * fit, and its phone ceiling is never below 100%. Framework-free, for the view and the result
 * viewer's header alike.
 */

import { DESKTOP_SPACING, PHONE_SPACING } from '../pdf/layout'
import type { ScrollPoint, Size } from '../pdf/layout'
import { DOUBLE_TAP_MULTIPLE, MAX_SCALE, MIN_SCALE, PHONE_MULTIPLES, ZOOM_STEPS, percentOf } from '../pdf/zoom'

export type ImageZoomMode = 'fit' | 'custom'

/** The zoom as it stands (1 = 100%). `fit` refits when the pane changes; `custom` keeps its
 *  scale, inside the range. */
export interface ImageZoomState {
  mode: ImageZoomMode
  scale: number
}

/** A zoom asked for: by a menu, a key, a button or the phone's More. */
export type ImageZoomTarget =
  | { mode: 'fit' }
  /** Absolute, 1 = 100%. */
  | { mode: 'custom'; scale: number }
  /** `times` × fit, the phone's zoom group. */
  | { mode: 'multiple'; times: number }

/** What every zoom is measured from, in CSS px: the pane's client size and the image's natural
 *  size. */
export interface ImageZoomContext {
  phone: boolean
  pane: Size
  image: Size
}

/** A point as fractions of the image box; outside 0…1 on the desk around it. */
export interface ImagePoint {
  fx: number
  fy: number
}

/** Where the view stood when it unmounted, for the way back from Markdown. */
export interface ImageReturn {
  zoom: ImageZoomState
  centre: ImagePoint
}

/** What the view tells the result viewer, which draws the zoom controls, on every change. */
export interface ImageViewerState {
  /** Loaded and shown; false while loading and after a failure. */
  ready: boolean
  /** The failure sheet shows: More leaves out *Open in new tab*. */
  failed: boolean
  zoom: ImageZoomState
  /** The fit scale now, which the phone's multiples are measured against; null until ready. */
  fit: number | null
  canZoomIn: boolean
  canZoomOut: boolean
  /** The natural size, for More's *Dimensions*; null until ready. */
  size: Size | null
  /** The image point at the pane's centre; null until ready. */
  centre: ImagePoint | null
}

/** The scrolling stage, and the image box on it. */
export interface Placement {
  stage: Size
  left: number
  top: number
  box: Size
}

/** The desk kept clear on each side of the image: 16px on desktop and a tablet, none on a
 *  phone. */
export function imageGutter(phone: boolean): number {
  return phone ? PHONE_SPACING.gutter : DESKTOP_SPACING.gutter
}

/** An `<img>`'s natural size. An SVG with no intrinsic size reports 0 in some engines; it then
 *  takes the CSS default object size, 300×150. */
export function naturalSize(width: number, height: number): Size {
  return width === 0 || height === 0 ? { width: 300, height: 150 } : { width, height }
}

/** The scale at which the whole image fits the pane less the gutters, never above 100%. */
export function fitScale(context: ImageZoomContext): number {
  const gutter = imageGutter(context.phone)
  const width = Math.max(1, context.pane.width - 2 * gutter)
  const height = Math.max(1, context.pane.height - 2 * gutter)
  return Math.min(1, width / context.image.width, height / context.image.height)
}

/** The smaller of fit and 25% to 500% on desktop; fit to the larger of 5 × fit and 100% on a
 *  phone. */
export function imageZoomLimits(context: ImageZoomContext): { min: number; max: number } {
  const fit = fitScale(context)
  if (!context.phone) {
    return { min: Math.min(fit, MIN_SCALE), max: MAX_SCALE }
  }
  return { min: fit, max: Math.max(PHONE_MULTIPLES[PHONE_MULTIPLES.length - 1]! * fit, 1) }
}

/** A scale the reader chose, inside the limits. A scale at the floor where the floor is the fit
 *  is fit to window itself, so it keeps following the window. */
function custom(scale: number, context: ImageZoomContext): ImageZoomState {
  const fit = fitScale(context)
  const { min, max } = imageZoomLimits(context)
  const clamped = Math.min(max, Math.max(min, scale))
  if (percentOf(clamped) <= percentOf(min) && percentOf(min) === percentOf(fit)) {
    return { mode: 'fit', scale: fit }
  }
  return { mode: 'custom', scale: clamped }
}

/** What a menu choice, a key or the phone's More comes to. */
export function resolveImageZoom(target: ImageZoomTarget, context: ImageZoomContext): ImageZoomState {
  switch (target.mode) {
    case 'fit':
      return { mode: 'fit', scale: fitScale(context) }
    case 'custom':
      return custom(target.scale, context)
    case 'multiple':
      return custom(target.times * fitScale(context), context)
  }
}

/** The zoom after the pane is resized: a fit fits again, a custom scale stays inside the new
 *  range. */
export function refitImageZoom(zoom: ImageZoomState, context: ImageZoomContext): ImageZoomState {
  return zoom.mode === 'fit' ? resolveImageZoom({ mode: 'fit' }, context) : custom(zoom.scale, context)
}

/** The zoom an image opens at: fit, or the zoom carried back from Markdown, refitted. */
export function openingImageZoom(context: ImageZoomContext, initial: ImageZoomState | null): ImageZoomState {
  return initial ? refitImageZoom(initial, context) : resolveImageZoom({ mode: 'fit' }, context)
}

/**
 * The next rung above or below the current scale: the PDF viewer's ladder on desktop, 1×, 2×,
 * 3× and 5× fit on a phone. Rungs are compared as whole percentages, so a fit of 99.6% goes up
 * to 125%, not to a 100% that looks the same; past the last rung the limit is the step.
 */
export function stepImageZoom(zoom: ImageZoomState, direction: 'in' | 'out', context: ImageZoomContext): ImageZoomState {
  const { min, max } = imageZoomLimits(context)
  const fit = fitScale(context)
  const rungs = context.phone ? [1, ...PHONE_MULTIPLES].map((times) => times * fit) : ZOOM_STEPS
  const now = percentOf(zoom.scale)
  const scale =
    direction === 'in'
      ? (rungs.find((rung) => percentOf(rung) > now) ?? max)
      : (rungs.findLast((rung) => percentOf(rung) < now) ?? min)
  return custom(scale, context)
}

/** Whether − or + would change what the reader sees: `canZoomIn` / `canZoomOut`. */
export function canStepImageZoom(zoom: ImageZoomState, direction: 'in' | 'out', context: ImageZoomContext): boolean {
  return percentOf(stepImageZoom(zoom, direction, context).scale) !== percentOf(zoom.scale)
}

/** A pinch that ends at `ratio` times the zoom it started from, inside the limits. During the
 *  pinch, `zoomImageBy(zoom, ratio, context).scale / zoom.scale` is the transform to show. */
export function zoomImageBy(zoom: ImageZoomState, ratio: number, context: ImageZoomContext): ImageZoomState {
  return custom(zoom.scale * ratio, context)
}

/** A custom scale that shows as the fit's percentage is at fit too, for a double click and a
 *  double tap. */
function atFit(zoom: ImageZoomState, context: ImageZoomContext): boolean {
  return zoom.mode === 'fit' || percentOf(zoom.scale) === percentOf(fitScale(context))
}

/** A double click: fit goes to 100%, or to 200% where fit is 100% already; anything else back
 *  to fit. */
export function doubleClickZoom(zoom: ImageZoomState, context: ImageZoomContext): ImageZoomState {
  if (!atFit(zoom, context)) {
    return resolveImageZoom({ mode: 'fit' }, context)
  }
  return custom(percentOf(fitScale(context)) >= 100 ? 2 : 1, context)
}

/** A double tap on a phone: fit goes to 2 × fit, anything else back to fit. */
export function doubleTapImageZoom(zoom: ImageZoomState, context: ImageZoomContext): ImageZoomState {
  return atFit(zoom, context)
    ? custom(DOUBLE_TAP_MULTIPLE * fitScale(context), context)
    : resolveImageZoom({ mode: 'fit' }, context)
}

/**
 * Whether a menu item is the current zoom (`menuitemradio`'s checked). A fit checks only *Fit to
 * window*, never the percentage it happens to come to; a percentage checks when the custom
 * scale shows as it; a scale between presets checks nothing. A phone multiple compares with
 * `fit` × times, and 1× is fit.
 */
export function isImageZoomChecked(zoom: ImageZoomState, target: ImageZoomTarget, fit: number | null): boolean {
  switch (target.mode) {
    case 'fit':
      return zoom.mode === 'fit'
    case 'custom':
      return zoom.mode === 'custom' && percentOf(zoom.scale) === percentOf(target.scale)
    case 'multiple':
      if (target.times === 1) {
        return zoom.mode === 'fit'
      }
      return zoom.mode === 'custom' && fit !== null && Math.abs(zoom.scale / fit - target.times) < 0.005
  }
}

/** The image box in whole CSS px, rounded down as `boxPx` rounds a page, so a fit never
 *  overflows the pane; the allowance keeps a rounding error from costing a pixel. */
export function imageBox(image: Size, scale: number): Size {
  return {
    width: Math.max(1, Math.floor(image.width * scale + 1e-6)),
    height: Math.max(1, Math.floor(image.height * scale + 1e-6)),
  }
}

/** The stage the pane scrolls and the box on it, per axis: a box that fits is centred in the
 *  pane; a larger one scrolls with `gutter` of desk beyond each edge. */
export function imagePlacement(box: Size, pane: Size, gutter: number): Placement {
  const stage = {
    width: box.width + 2 * gutter > pane.width ? box.width + 2 * gutter : pane.width,
    height: box.height + 2 * gutter > pane.height ? box.height + 2 * gutter : pane.height,
  }
  return {
    stage,
    left: Math.floor((stage.width - box.width) / 2),
    top: Math.floor((stage.height - box.height) / 2),
    box,
  }
}

/** The image point under `anchor` (pane client px) at this scroll. Not clamped: a point on the
 *  desk keeps its relation to the image. */
export function pointAt(placement: Placement, scroll: ScrollPoint, anchor: { x: number; y: number }): ImagePoint {
  return {
    fx: (scroll.left + anchor.x - placement.left) / placement.box.width,
    fy: (scroll.top + anchor.y - placement.top) / placement.box.height,
  }
}

/** The scroll that puts `point` under `anchor` on the `next` placement, inside the stage's
 *  scroll range, so at an edge the anchor gives way rather than the pane over-scrolling. */
export function scrollForPoint(
  point: ImagePoint,
  anchor: { x: number; y: number },
  next: Placement,
  pane: Size,
): ScrollPoint {
  const left = next.left + point.fx * next.box.width - anchor.x
  const top = next.top + point.fy * next.box.height - anchor.y
  return {
    left: Math.min(Math.max(0, next.stage.width - pane.width), Math.max(0, left)),
    top: Math.min(Math.max(0, next.stage.height - pane.height), Math.max(0, top)),
  }
}
