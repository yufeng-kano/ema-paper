import { describe, expect, it } from 'vitest'
import { percentOf } from '../pdf/zoom'
import {
  canStepImageZoom,
  doubleClickZoom,
  doubleTapImageZoom,
  fitScale,
  imageBox,
  imageGutter,
  imagePlacement,
  imageZoomLimits,
  isImageZoomChecked,
  naturalSize,
  openingImageZoom,
  pointAt,
  refitImageZoom,
  resolveImageZoom,
  scrollForPoint,
  stepImageZoom,
  zoomImageBy,
} from './image-zoom'
import type { ImageZoomContext, ImageZoomState } from './image-zoom'

const PHOTO = { width: 4032, height: 3024 }
const desk = (width: number, height: number, image = PHOTO): ImageZoomContext => ({
  phone: false,
  pane: { width, height },
  image,
})
const phone = (width: number, height: number, image = PHOTO): ImageZoomContext => ({
  phone: true,
  pane: { width, height },
  image,
})
/** A context whose fit is `fit`: an image one pixel high, so only its width limits the fit. */
const withFit = (fit: number, onPhone = false): ImageZoomContext =>
  onPhone ? phone(390, 700, { width: 390 / fit, height: 1 }) : desk(1000, 800, { width: 968 / fit, height: 1 })
const custom = (scale: number): ImageZoomState => ({ mode: 'custom', scale })
const FIT: ImageZoomState = { mode: 'fit', scale: 1 }

describe('fit to window', () => {
  it('fits the whole image inside the pane less 16px gutters on desktop', () => {
    expect(fitScale(desk(1000, 800))).toBeCloseTo(Math.min(968 / 4032, 768 / 3024), 12)
    expect(fitScale(desk(1000, 800))).toBeCloseTo(0.24008, 5)
  })

  it('never goes above 100%, so a small image opens at its own size', () => {
    expect(fitScale(desk(1000, 800, { width: 200, height: 100 }))).toBe(1)
  })

  it('keeps no gutter on a phone', () => {
    expect(fitScale(phone(390, 700))).toBeCloseTo(390 / 4032, 12)
  })

  it('fits a tall image by its height', () => {
    expect(fitScale(desk(1000, 800, { width: 1170, height: 20000 }))).toBeCloseTo(768 / 20000, 12)
  })

  it('stays above 0 and finite in a pane smaller than its gutters', () => {
    const fit = fitScale(desk(20, 20))
    expect(fit).toBeGreaterThan(0)
    expect(Number.isFinite(fit)).toBe(true)
  })

  it('gives an SVG with no intrinsic size the CSS default object size', () => {
    expect(naturalSize(0, 0)).toEqual({ width: 300, height: 150 })
    expect(naturalSize(0, 80)).toEqual({ width: 300, height: 150 })
    expect(naturalSize(120, 80)).toEqual({ width: 120, height: 80 })
  })

  it('keeps a 16px gutter on desktop and none on a phone', () => {
    expect(imageGutter(false)).toBe(16)
    expect(imageGutter(true)).toBe(0)
  })
})

describe('the range', () => {
  it('runs on desktop from the smaller of fit and 25% to 500%', () => {
    const low = desk(1000, 800)
    expect(imageZoomLimits(low)).toEqual({ min: fitScale(low), max: 5 })
    expect(imageZoomLimits(withFit(0.6))).toEqual({ min: 0.25, max: 5 })
  })

  it('runs on a phone from fit to the larger of 5 × fit and 100%', () => {
    const photo = phone(390, 700)
    expect(imageZoomLimits(photo)).toEqual({ min: fitScale(photo), max: 1 })
    const { min, max } = imageZoomLimits(withFit(0.5, true))
    expect(min).toBeCloseTo(0.5, 12)
    expect(max).toBeCloseTo(2.5, 12)
  })

  it('keeps every zoom asked for inside the range', () => {
    for (const context of [desk(1000, 800), withFit(0.6), phone(390, 700), withFit(0.5, true)]) {
      const { min, max } = imageZoomLimits(context)
      for (const scale of [0.01, 0.1, 0.5, 1, 3, 20]) {
        const zoom = resolveImageZoom({ mode: 'custom', scale }, context)
        expect(zoom.scale).toBeGreaterThanOrEqual(min - 1e-12)
        expect(zoom.scale).toBeLessThanOrEqual(max + 1e-12)
      }
    }
  })

  it('takes a scale at the floor as fit where the floor is the fit', () => {
    expect(zoomImageBy(custom(0.3), 0.1, withFit(0.2, true))).toEqual({ mode: 'fit', scale: fitScale(withFit(0.2, true)) })
    expect(resolveImageZoom({ mode: 'custom', scale: 0.05 }, withFit(0.1))).toEqual({
      mode: 'fit',
      scale: fitScale(withFit(0.1)),
    })
  })

  it('keeps 25% a custom zoom on desktop where the fit is above it', () => {
    expect(resolveImageZoom({ mode: 'custom', scale: 0.25 }, withFit(0.6))).toEqual(custom(0.25))
  })

  it('stops a pinch past the ceiling at the ceiling', () => {
    expect(zoomImageBy(custom(4), 3, desk(1000, 800))).toEqual(custom(5))
  })

  it('takes a phone multiple of the fit, 1× being the fit itself', () => {
    const context = withFit(0.1, true)
    expect(resolveImageZoom({ mode: 'multiple', times: 3 }, context).scale).toBeCloseTo(0.3, 12)
    expect(resolveImageZoom({ mode: 'multiple', times: 1 }, context).mode).toBe('fit')
  })
})

describe('opening and refitting', () => {
  it('opens at fit', () => {
    const context = desk(1000, 800)
    expect(openingImageZoom(context, null)).toEqual({ mode: 'fit', scale: fitScale(context) })
  })

  it('opens at the zoom carried back from Markdown, kept inside the new range', () => {
    expect(openingImageZoom(desk(1000, 800), custom(3))).toEqual(custom(3))
    expect(openingImageZoom(withFit(0.1, true), custom(3))).toEqual(custom(1))
  })

  it('refits a fit carried back to the pane it opens in', () => {
    const context = desk(1400, 900)
    expect(openingImageZoom(context, { mode: 'fit', scale: 0.1 })).toEqual({ mode: 'fit', scale: fitScale(context) })
  })

  it('refits a fit when the pane changes, and keeps a custom scale', () => {
    const wider = desk(1600, 1000)
    expect(refitImageZoom({ mode: 'fit', scale: 0.24 }, wider)).toEqual({ mode: 'fit', scale: fitScale(wider) })
    expect(refitImageZoom(custom(3), wider)).toEqual(custom(3))
  })

  it('turns a phone’s custom scale below a grown fit into fit', () => {
    expect(refitImageZoom(custom(0.2), withFit(0.3, true))).toEqual({ mode: 'fit', scale: fitScale(withFit(0.3, true)) })
  })
})

describe('the ladder', () => {
  it('steps from a fit between rungs to the rung above or below it', () => {
    const context = withFit(0.26)
    const fit = resolveImageZoom({ mode: 'fit' }, context)
    expect(stepImageZoom(fit, 'in', context)).toEqual(custom(0.5))
    expect(stepImageZoom(fit, 'out', context)).toEqual(custom(0.25))
  })

  it('cannot step below a fit that is the floor', () => {
    const context = withFit(0.1)
    const fit = resolveImageZoom({ mode: 'fit' }, context)
    expect(canStepImageZoom(fit, 'out', context)).toBe(false)
    expect(stepImageZoom(fit, 'out', context).mode).toBe('fit')
  })

  it('cannot step above 500%', () => {
    expect(canStepImageZoom(custom(5), 'in', desk(1000, 800))).toBe(false)
  })

  it('compares rungs as whole percentages, so 99.6% steps to 125%', () => {
    expect(stepImageZoom(custom(0.996), 'in', desk(1000, 800))).toEqual(custom(1.25))
  })

  it('steps a phone through 1×, 2×, 3× and 5× fit, then the ceiling', () => {
    const context = withFit(0.1, true)
    let zoom = resolveImageZoom({ mode: 'fit' }, context)
    const seen: number[] = []
    while (canStepImageZoom(zoom, 'in', context)) {
      zoom = stepImageZoom(zoom, 'in', context)
      seen.push(percentOf(zoom.scale))
    }
    expect(seen).toEqual([20, 30, 50, 100])
  })

  it('steps a phone down to fit at the floor', () => {
    const context = withFit(0.1, true)
    expect(stepImageZoom(custom(0.2), 'out', context).mode).toBe('fit')
  })

  it('climbs from fit to the ceiling and back to the floor, never turning back', () => {
    for (const context of [desk(1000, 800), withFit(0.6), desk(1000, 800, { width: 120, height: 80 }), phone(390, 700)]) {
      const { min, max } = imageZoomLimits(context)
      let zoom = resolveImageZoom({ mode: 'fit' }, context)
      while (canStepImageZoom(zoom, 'in', context)) {
        const next = stepImageZoom(zoom, 'in', context)
        expect(percentOf(next.scale)).toBeGreaterThan(percentOf(zoom.scale))
        zoom = next
      }
      expect(percentOf(zoom.scale)).toBe(percentOf(max))
      while (canStepImageZoom(zoom, 'out', context)) {
        const next = stepImageZoom(zoom, 'out', context)
        expect(percentOf(next.scale)).toBeLessThan(percentOf(zoom.scale))
        zoom = next
      }
      expect(percentOf(zoom.scale)).toBe(percentOf(min))
    }
  })
})

describe('a double click and a double tap', () => {
  it('goes from fit to 100%, and from anything else back to fit', () => {
    const context = desk(1000, 800)
    const fit = resolveImageZoom({ mode: 'fit' }, context)
    expect(doubleClickZoom(fit, context)).toEqual(custom(1))
    expect(doubleClickZoom(custom(1), context)).toEqual(fit)
  })

  it('goes to 200% where the image fits at its own size', () => {
    const context = desk(1000, 800, { width: 120, height: 80 })
    expect(doubleClickZoom(resolveImageZoom({ mode: 'fit' }, context), context)).toEqual(custom(2))
  })

  it('takes a custom scale that shows as the fit’s percentage as fit', () => {
    const context = desk(1000, 800)
    expect(doubleClickZoom(custom(fitScale(context) + 0.001), context)).toEqual(custom(1))
  })

  it('goes on a phone from fit to 2 × fit, and from anything else back to fit', () => {
    const context = withFit(0.1, true)
    const fit = resolveImageZoom({ mode: 'fit' }, context)
    expect(doubleTapImageZoom(fit, context).scale).toBeCloseTo(0.2, 12)
    expect(doubleTapImageZoom(custom(0.3), context)).toEqual(fit)
  })
})

describe('the checked menu item', () => {
  it('checks only Fit to window at a fit that comes to 100%', () => {
    const zoom: ImageZoomState = { mode: 'fit', scale: 1 }
    expect(isImageZoomChecked(zoom, { mode: 'fit' }, 1)).toBe(true)
    expect(isImageZoomChecked(zoom, { mode: 'custom', scale: 1 }, 1)).toBe(false)
  })

  it('checks a percentage the custom scale shows as, and nothing between', () => {
    expect(isImageZoomChecked(custom(1.5), { mode: 'custom', scale: 1.5 }, 0.24)).toBe(true)
    expect(isImageZoomChecked(custom(1.45), { mode: 'custom', scale: 1.5 }, 0.24)).toBe(false)
    expect(isImageZoomChecked(custom(1.45), { mode: 'fit' }, 0.24)).toBe(false)
  })

  it('checks a phone multiple of the fit, and 1× at fit', () => {
    expect(isImageZoomChecked(custom(0.3), { mode: 'multiple', times: 3 }, 0.1)).toBe(true)
    expect(isImageZoomChecked(custom(0.3), { mode: 'multiple', times: 2 }, 0.1)).toBe(false)
    expect(isImageZoomChecked(FIT, { mode: 'multiple', times: 1 }, 0.1)).toBe(true)
    expect(isImageZoomChecked(custom(0.3), { mode: 'multiple', times: 3 }, null)).toBe(false)
  })
})

describe('the image box and its placement', () => {
  it('rounds the fit box down to the pane less its gutters', () => {
    const context = desk(1000, 800)
    expect(imageBox(PHOTO, fitScale(context))).toEqual({ width: 968, height: 726 })
  })

  it('keeps a fit that is a ratio of whole numbers from flooring a pixel short', () => {
    expect(imageBox({ width: 3024, height: 10 }, 968 / 3024).width).toBe(968)
  })

  it('never gives the pane a scrollbar at fit', () => {
    for (const onPhone of [false, true]) {
      const gutter = imageGutter(onPhone)
      for (let paneWidth = 300; paneWidth <= 2000; paneWidth += 37) {
        for (let imageWidth = 100; imageWidth <= 5000; imageWidth += 97) {
          const pane = { width: paneWidth, height: 700 }
          const image = { width: imageWidth, height: Math.round(imageWidth * 0.75) + 1 }
          const box = imageBox(image, fitScale({ phone: onPhone, pane, image }))
          expect(box.width + 2 * gutter).toBeLessThanOrEqual(pane.width)
          expect(box.height + 2 * gutter).toBeLessThanOrEqual(pane.height)
          expect(imagePlacement(box, pane, gutter).stage).toEqual(pane)
        }
      }
    }
  })

  it('centres a box that fits', () => {
    expect(imagePlacement({ width: 500, height: 300 }, { width: 1000, height: 800 }, 16)).toEqual({
      stage: { width: 1000, height: 800 },
      left: 250,
      top: 250,
      box: { width: 500, height: 300 },
    })
  })

  it('scrolls a larger box with the gutter of desk beyond each edge', () => {
    const placed = imagePlacement({ width: 2000, height: 1500 }, { width: 1000, height: 800 }, 16)
    expect(placed.stage).toEqual({ width: 2032, height: 1532 })
    expect([placed.left, placed.top]).toEqual([16, 16])
  })

  it('keeps no desk beyond the edges on a phone, and centres the axis that fits', () => {
    const placed = imagePlacement({ width: 780, height: 600 }, { width: 390, height: 700 }, 0)
    expect(placed.stage).toEqual({ width: 780, height: 700 })
    expect([placed.left, placed.top]).toEqual([0, 50])
  })
})

describe('the point under the anchor', () => {
  it('stays under the anchor within a pixel when a zoom commits', () => {
    const context = desk(1000, 800)
    const pane = context.pane
    const anchor = { x: 200, y: 100 }
    const before = imagePlacement(imageBox(PHOTO, 1), pane, 16)
    const scroll = { left: 900, top: 600 }
    const point = pointAt(before, scroll, anchor)
    const after = imagePlacement(imageBox(PHOTO, 2), pane, 16)
    const next = scrollForPoint(point, anchor, after, pane)
    const landed = pointAt(after, next, anchor)
    expect(Math.abs((landed.fx - point.fx) * after.box.width)).toBeLessThanOrEqual(1)
    expect(Math.abs((landed.fy - point.fy) * after.box.height)).toBeLessThanOrEqual(1)
  })

  it('keeps a point on the desk beside the image in its relation to it', () => {
    const placed = imagePlacement({ width: 500, height: 300 }, { width: 1000, height: 800 }, 16)
    const point = pointAt(placed, { left: 0, top: 0 }, { x: 0, y: 0 })
    expect(point.fx).toBeLessThan(0)
    expect(point.fy).toBeLessThan(0)
  })

  it('stays inside the scroll range at an edge', () => {
    const pane = { width: 1000, height: 800 }
    const placed = imagePlacement({ width: 2000, height: 1500 }, pane, 16)
    const centre = { x: 500, y: 400 }
    expect(scrollForPoint({ fx: 0, fy: 0 }, centre, placed, pane)).toEqual({ left: 0, top: 0 })
    expect(scrollForPoint({ fx: 1, fy: 1 }, { x: 0, y: 0 }, placed, pane)).toEqual({ left: 1032, top: 732 })
    const fits = imagePlacement({ width: 500, height: 300 }, pane, 16)
    expect(scrollForPoint({ fx: 1, fy: 1 }, { x: 0, y: 0 }, fits, pane)).toEqual({ left: 0, top: 0 })
  })
})
