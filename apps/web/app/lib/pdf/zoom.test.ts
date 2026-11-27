import { describe, expect, it } from 'vitest'
import { createLayout, DESKTOP_SPACING, pageAt, pageRect, scaleLayout, setPageSizes } from './layout'
import type { Layout } from './layout'
import {
  anchoredScroll,
  canStepZoom,
  doubleTapZoom,
  fitPageScale,
  fitWidthScale,
  isZoomChecked,
  openingZoom,
  percentOf,
  refitZoom,
  resolveZoom,
  stepZoom,
  ZOOM_PRESETS,
  zoomBy,
  zoomLimits,
} from './zoom'
import type { ZoomContext, ZoomState } from './zoom'

const LETTER = { width: 612, height: 792 }
const desktop = (width: number, height = 900): ZoomContext => ({ phone: false, pane: { width, height }, page: LETTER })
const phone: ZoomContext = { phone: true, pane: { width: 390, height: 784 }, page: LETTER }
const custom = (scale: number): ZoomState => ({ mode: 'custom', scale })

describe('fit scales', () => {
  it('fits the page’s width to the pane less 16px gutters on desktop, and none on a phone', () => {
    expect(fitWidthScale(desktop(848))).toBeCloseTo(1, 12)
    expect(fitWidthScale(phone)).toBeCloseTo(390 / 816, 12)
  })

  it('fits the whole page, by whichever side is tighter', () => {
    expect(fitPageScale(desktop(2000, 1088))).toBeCloseTo(1, 12)
    expect(fitPageScale(desktop(848, 5000))).toBeCloseTo(1, 12)
  })

  it('keeps fit modes inside the desktop range', () => {
    expect(resolveZoom({ mode: 'fit-width' }, desktop(100))).toEqual({ mode: 'fit-width', scale: 0.25 })
    expect(resolveZoom({ mode: 'fit-width' }, { ...desktop(5000), page: { width: 100, height: 100 } }).scale).toBe(5)
  })
})

describe('opening zoom', () => {
  it('opens at 100% on desktop when page 1 fits with its gutters, at fit width when not', () => {
    expect(openingZoom(desktop(848), null)).toEqual(custom(1))
    const narrow = openingZoom(desktop(847), null)
    expect(narrow.mode).toBe('fit-width')
    expect(narrow.scale).toBeLessThan(1)
  })

  it('opens a phone at fit width, whatever page 1 is', () => {
    expect(openingZoom(phone, null)).toEqual({ mode: 'fit-width', scale: fitWidthScale(phone) })
    expect(openingZoom({ ...phone, page: { width: 100, height: 100 } }, null).mode).toBe('fit-width')
  })

  it('reopens at the zoom it left, refitted to the pane it reopens in', () => {
    expect(openingZoom(desktop(1200), custom(1.5))).toEqual(custom(1.5))
    expect(openingZoom(desktop(1200), { mode: 'fit-width', scale: 0.4 }).scale).toBeCloseTo(fitWidthScale(desktop(1200)), 12)
  })
})

describe('limits', () => {
  it('runs from 25% to 500% on desktop', () => {
    expect(zoomLimits(desktop(1000))).toEqual({ min: 0.25, max: 5 })
    expect(resolveZoom({ mode: 'custom', scale: 9 }, desktop(1000))).toEqual(custom(5))
    expect(resolveZoom({ mode: 'custom', scale: 0.01 }, desktop(1000))).toEqual(custom(0.25))
  })

  it('runs from fit width to 5 × fit width on a phone, the floor being fit width itself', () => {
    const fit = fitWidthScale(phone)
    expect(zoomLimits(phone)).toEqual({ min: fit, max: 5 * fit })
    expect(resolveZoom({ mode: 'custom', scale: 0.1 }, phone)).toEqual({ mode: 'fit-width', scale: fit })
    expect(zoomBy({ mode: 'fit-width', scale: fit }, 20, phone)).toEqual(custom(5 * fit))
    expect(zoomBy(custom(2 * fit), 0.1, phone)).toEqual({ mode: 'fit-width', scale: fit })
  })

  it('keeps a pinch inside the limits', () => {
    expect(zoomBy(custom(4), 2, desktop(1000))).toEqual(custom(5))
    expect(zoomBy(custom(1), 1.1, desktop(1000)).scale).toBeCloseTo(1.1, 12)
  })
})

describe('the ladder', () => {
  const context = desktop(1000)

  it('steps to the next rung above or below', () => {
    expect(stepZoom(custom(1), 'in', context)).toEqual(custom(1.25))
    expect(stepZoom(custom(1), 'out', context)).toEqual(custom(0.75))
    expect(stepZoom(custom(2), 'in', context)).toEqual(custom(3))
    expect(stepZoom(custom(0.5), 'out', context)).toEqual(custom(0.25))
  })

  it('steps from between rungs to the nearest rung on that side', () => {
    expect(stepZoom(custom(1.1), 'in', context)).toEqual(custom(1.25))
    expect(stepZoom(custom(1.1), 'out', context)).toEqual(custom(1))
  })

  it('never steps to a rung that shows as the same percentage', () => {
    const fit: ZoomState = { mode: 'fit-width', scale: 0.996 }
    expect(stepZoom(fit, 'in', context)).toEqual(custom(1.25))
    expect(stepZoom(fit, 'out', context)).toEqual(custom(0.75))
    expect(stepZoom(custom(0.994), 'in', context)).toEqual(custom(1))
  })

  it('stops at the limits, where the button is disabled', () => {
    expect(stepZoom(custom(5), 'in', context)).toEqual(custom(5))
    expect(canStepZoom(custom(5), 'in', context)).toBe(false)
    expect(canStepZoom(custom(5), 'out', context)).toBe(true)
    expect(canStepZoom(custom(0.25), 'out', context)).toBe(false)
    expect(stepZoom(custom(4.6), 'in', context)).toEqual(custom(5))
  })

  it('leaves a fit mode for a custom scale', () => {
    expect(stepZoom({ mode: 'fit-page', scale: 0.8 }, 'in', context).mode).toBe('custom')
  })

  it('steps through 1×, 2×, 3× and 5× fit width on a phone', () => {
    const fit = fitWidthScale(phone)
    const at = (state: ZoomState) => state.scale / fit
    let zoom: ZoomState = { mode: 'fit-width', scale: fit }
    const seen: number[] = []
    for (let step = 0; step < 4; step++) {
      zoom = stepZoom(zoom, 'in', phone)
      seen.push(Math.round(at(zoom) * 100) / 100)
    }
    expect(seen).toEqual([2, 3, 5, 5])
    expect(stepZoom(custom(2 * fit), 'out', phone)).toEqual({ mode: 'fit-width', scale: fit })
    expect(canStepZoom({ mode: 'fit-width', scale: fit }, 'out', phone)).toBe(false)
  })
})

describe('menus and taps', () => {
  it('resolves the phone’s multiples of fit width, 1× being fit width', () => {
    const fit = fitWidthScale(phone)
    expect(resolveZoom({ mode: 'multiple', times: 3 }, phone).scale).toBeCloseTo(3 * fit, 12)
    expect(resolveZoom({ mode: 'multiple', times: 1 }, phone)).toEqual({ mode: 'fit-width', scale: fit })
  })

  it('checks the current preset only, a fit mode never checking a percentage', () => {
    expect(isZoomChecked(custom(1), { mode: 'custom', scale: 1 }, 1)).toBe(true)
    expect(isZoomChecked({ mode: 'fit-width', scale: 1 }, { mode: 'custom', scale: 1 }, 1)).toBe(false)
    expect(isZoomChecked({ mode: 'fit-width', scale: 1 }, { mode: 'fit-width' }, 1)).toBe(true)
    expect(isZoomChecked({ mode: 'fit-width', scale: 1 }, { mode: 'fit-page' }, 1)).toBe(false)
    expect(isZoomChecked(custom(1.1), { mode: 'custom', scale: 1 }, 1)).toBe(false)
    const checked = ZOOM_PRESETS.filter((scale) => isZoomChecked(custom(1.1), { mode: 'custom', scale }, null))
    expect(checked).toEqual([])
  })

  it('checks a phone multiple by its ratio to fit width', () => {
    const fit = 390 / 816
    expect(isZoomChecked(custom(2 * fit), { mode: 'multiple', times: 2 }, fit)).toBe(true)
    expect(isZoomChecked(custom(2.1 * fit), { mode: 'multiple', times: 2 }, fit)).toBe(false)
    expect(isZoomChecked({ mode: 'fit-width', scale: fit }, { mode: 'multiple', times: 1 }, fit)).toBe(true)
    expect(isZoomChecked(custom(2 * fit), { mode: 'multiple', times: 2 }, null)).toBe(false)
  })

  it('toggles between fit width and 2× on a double tap, from any other zoom back to fit width', () => {
    const fit = fitWidthScale(phone)
    const twice = doubleTapZoom({ mode: 'fit-width', scale: fit }, phone)
    expect(twice).toEqual(custom(2 * fit))
    expect(doubleTapZoom(twice, phone)).toEqual({ mode: 'fit-width', scale: fit })
    expect(doubleTapZoom(custom(4 * fit), phone)).toEqual({ mode: 'fit-width', scale: fit })
  })

  it('rounds a scale to a whole percentage', () => {
    expect(percentOf(0.996)).toBe(100)
    expect(percentOf(1.254)).toBe(125)
  })
})

describe('refitting', () => {
  it('keeps fit width at the pane’s width as the pane changes, and a custom scale as it is', () => {
    expect(refitZoom({ mode: 'fit-width', scale: 1 }, desktop(1256)).scale).toBeCloseTo(1.5, 12)
    expect(refitZoom(custom(1.5), desktop(500))).toEqual(custom(1.5))
  })

  it('raises a phone’s custom scale to a floor that moved past it', () => {
    const turned: ZoomContext = { ...phone, pane: { width: 844, height: 340 } }
    expect(refitZoom(custom(2 * fitWidthScale(phone)), turned)).toEqual({ mode: 'fit-width', scale: fitWidthScale(turned) })
  })
})

/** The page and the fraction of it under a pane point, the zoom-independent place a reader
 *  looks at. */
function placeUnder(layout: Layout, scroll: { left: number; top: number }, anchor: { x: number; y: number }, paneWidth: number) {
  const y = scroll.top + anchor.y
  const index = pageAt(layout, y)
  const rect = pageRect(layout, index, paneWidth)
  return { index, fx: (scroll.left + anchor.x - rect.left) / rect.width, fy: (y - rect.top) / rect.height }
}

describe('anchored scroll on a zoom commit', () => {
  const pane = { width: 1000, height: 800 }
  const before = createLayout(20, LETTER, 1, DESKTOP_SPACING)
  setPageSizes(before, [{ index: 6, size: { width: 792, height: 612 } }], 0)

  it('keeps the point under the pointer where it was', () => {
    const scroll = { left: 0, top: before.tops[5]! + 300 }
    for (const anchor of [
      { x: 500, y: 400 },
      { x: 120, y: 10 },
      { x: 880, y: 790 },
    ]) {
      const place = placeUnder(before, scroll, anchor, pane.width)
      for (const scale of [0.5, 1.37, 2, 4.5]) {
        const after = scaleLayout(before, scale)
        const moved = anchoredScroll(before, after, scroll, anchor, pane)
        const now = placeUnder(after, moved, anchor, pane.width)
        expect(now.index).toBe(place.index)
        expect(now.fy).toBeCloseTo(place.fy, 9)
        // A page centred in the pane that grows past it starts one gutter from the left, so
        // near the left edge the scroll gives way at 0 rather than holding the point.
        if (after.width > pane.width && moved.left > 0) {
          expect(now.fx).toBeCloseTo(place.fx, 9)
        }
      }
    }
  })

  it('keeps the point under the pointer when the pointer is over a gap', () => {
    const scroll = { left: 0, top: before.tops[3]! - 8 - 400 }
    const after = scaleLayout(before, 2)
    const moved = anchoredScroll(before, after, scroll, { x: 500, y: 400 }, pane)
    expect(after.tops[3]! - (moved.top + 400)).toBe(8)
  })

  it('round-trips: zooming in and back out about the same point returns to the same scroll', () => {
    const scroll = { left: 0, top: before.tops[8]! + 123 }
    const anchor = { x: 500, y: 400 }
    const after = scaleLayout(before, 3)
    const there = anchoredScroll(before, after, scroll, anchor, pane)
    const back = anchoredScroll(after, before, there, anchor, pane)
    expect(back.top).toBeCloseTo(scroll.top, 6)
    expect(back.left).toBeCloseTo(scroll.left, 6)
  })

  it('gives way at an edge rather than scrolling past it', () => {
    const top = anchoredScroll(before, scaleLayout(before, 0.25), { left: 0, top: 0 }, { x: 500, y: 0 }, pane)
    expect(top).toEqual({ left: 0, top: 0 })
    const after = scaleLayout(before, 0.25)
    const end = anchoredScroll(before, after, { left: 0, top: before.height - pane.height }, { x: 500, y: 790 }, pane)
    expect(end.top).toBeLessThanOrEqual(Math.max(0, after.height - pane.height))
    expect(end.top).toBeGreaterThanOrEqual(0)
  })
})
