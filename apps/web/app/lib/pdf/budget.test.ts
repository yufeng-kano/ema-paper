import { describe, expect, it } from 'vitest'
import {
  backingSize,
  canvasesToRelease,
  canvasLimits,
  containsRect,
  DESKTOP_CANVAS_CAP,
  detailCanvas,
  MAX_CANVAS_DIM,
  MOBILE_CANVAS_CAP,
} from './budget'

const MAC = { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', platform: 'MacIntel', maxTouchPoints: 0 }
const IPHONE = { userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15', platform: 'iPhone', maxTouchPoints: 5 }
const IPAD = { ...MAC, maxTouchPoints: 5 }
const ANDROID = { userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36', platform: 'Linux armv8l', maxTouchPoints: 5 }

describe('canvas limits by platform', () => {
  it('caps iOS, iPadOS and Android at pdf.js’s mobile value, and the rest at 4096²', () => {
    expect(canvasLimits(MAC)).toEqual({ cap: DESKTOP_CANVAS_CAP, total: 4 * DESKTOP_CANVAS_CAP, maxDim: MAX_CANVAS_DIM })
    expect(canvasLimits(IPHONE)).toEqual({ cap: MOBILE_CANVAS_CAP, total: 3 * MOBILE_CANVAS_CAP, maxDim: MAX_CANVAS_DIM })
    expect(canvasLimits(IPAD).total).toBe(3 * MOBILE_CANVAS_CAP)
    expect(canvasLimits(ANDROID)).toEqual({ cap: MOBILE_CANVAS_CAP, total: 4 * MOBILE_CANVAS_CAP, maxDim: MAX_CANVAS_DIM })
  })
})

describe('a page’s backing store', () => {
  it('is the CSS size × the device pixel ratio while that fits', () => {
    expect(backingSize(816, 1056, 2, DESKTOP_CANVAS_CAP)).toEqual({ width: 1632, height: 2112, sx: 2, sy: 2, capped: false })
    expect(backingSize(816, 1056, 1, MOBILE_CANVAS_CAP).capped).toBe(false)
  })

  it('never passes the cap or the longest side, however far the page is zoomed', () => {
    for (const [width, height, dpr] of [
      [4080, 5280, 2],
      [4080, 5280, 3],
      [2040, 2640, 3],
      [40000, 300, 2],
      [300, 40000, 1],
      [1357, 1999, 2.625],
    ] as const) {
      for (const cap of [MOBILE_CANVAS_CAP, DESKTOP_CANVAS_CAP]) {
        const backing = backingSize(width, height, dpr, cap)
        expect(backing.width * backing.height).toBeLessThanOrEqual(cap)
        expect(Math.max(backing.width, backing.height)).toBeLessThanOrEqual(MAX_CANVAS_DIM)
        expect(backing.capped).toBe(true)
        expect(backing.sx).toBeLessThan(dpr)
      }
    }
  })

  it('keeps the page’s shape when capped, so the stretched canvas is not distorted', () => {
    const backing = backingSize(4080, 5280, 2, MOBILE_CANVAS_CAP)
    expect(backing.sx).toBeCloseTo(backing.sy, 3)
    expect(backing.width * backing.height).toBeGreaterThan(0.99 * MOBILE_CANVAS_CAP)
  })
})

describe('the detail canvas', () => {
  const page = { width: 4080, height: 5280 }

  it('covers the part in view and a margin, inside the page and under the cap', () => {
    const visible = { left: 1000, top: 2000, width: 390, height: 784 }
    const detail = detailCanvas(visible, page, 3, MOBILE_CANVAS_CAP)!
    expect(containsRect(detail, visible)).toBe(true)
    expect(detail.width).toBeGreaterThan(visible.width)
    expect(detail.left).toBeGreaterThanOrEqual(0)
    expect(detail.top).toBeGreaterThanOrEqual(0)
    expect(detail.left + detail.width).toBeLessThanOrEqual(page.width)
    expect(detail.top + detail.height).toBeLessThanOrEqual(page.height)
    expect(detail.backing.width * detail.backing.height).toBeLessThanOrEqual(MOBILE_CANVAS_CAP)
    expect(detail.backing.sx).toBeCloseTo(3, 2)
  })

  it('stops its margin at the page’s edges', () => {
    const detail = detailCanvas({ left: 0, top: 0, width: 300, height: 300 }, page, 2, DESKTOP_CANVAS_CAP)!
    expect(detail).toMatchObject({ left: 0, top: 0, width: 600, height: 600 })
  })

  it('drops the margin and the resolution, never the cap, when the part in view alone passes it', () => {
    const visible = { left: 0, top: 0, width: 3000, height: 2000 }
    const detail = detailCanvas(visible, page, 2, DESKTOP_CANVAS_CAP)!
    expect(detail).toMatchObject(visible)
    expect(detail.backing.width * detail.backing.height).toBeLessThanOrEqual(DESKTOP_CANVAS_CAP)
    expect(detail.backing.capped).toBe(true)
  })

  it('draws from the detail’s top left at its own scale', () => {
    const detail = detailCanvas({ left: 1000, top: 2000, width: 400, height: 400 }, page, 2, DESKTOP_CANVAS_CAP)!
    const [sx, , , sy, dx, dy] = detail.transform
    expect(sx).toBe(detail.backing.sx)
    expect(sy).toBe(detail.backing.sy)
    expect(dx).toBeCloseTo(-detail.left * sx, 9)
    expect(dy).toBeCloseTo(-detail.top * sy, 9)
  })

  it('needs drawing again only once the part in view leaves the part drawn', () => {
    const drawn = { left: 100, top: 100, width: 500, height: 500 }
    expect(containsRect(drawn, { left: 100, top: 100, width: 500, height: 500 })).toBe(true)
    expect(containsRect(drawn, { left: 150, top: 120, width: 300, height: 300 })).toBe(true)
    expect(containsRect(drawn, { left: 99, top: 120, width: 300, height: 300 })).toBe(false)
    expect(containsRect(drawn, { left: 150, top: 120, width: 300, height: 481 })).toBe(false)
    expect(detailCanvas({ left: 0, top: 0, width: 0, height: 10 }, page, 2, DESKTOP_CANVAS_CAP)).toBeNull()
  })
})

describe('the total budget', () => {
  const visible = { first: 10, last: 11 }
  const canvas = (index: number, pixels = 100) => ({ index, pixels })

  it('releases nothing while everything fits', () => {
    expect(canvasesToRelease([canvas(9), canvas(10), canvas(11), canvas(12)], visible, 400)).toEqual([])
  })

  it('releases the furthest pages first, until the rest fit', () => {
    const live = [canvas(5), canvas(9), canvas(10), canvas(11), canvas(12), canvas(20)]
    expect(canvasesToRelease(live, visible, 400).map((c) => c.index)).toEqual([20, 5])
  })

  it('on a tie releases the page above before the page below', () => {
    const live = [canvas(9), canvas(10), canvas(11), canvas(12)]
    expect(canvasesToRelease(live, visible, 300).map((c) => c.index)).toEqual([9])
  })

  it('never releases a page in view, even over the total', () => {
    const live = [canvas(10, 500), canvas(11, 500), canvas(12, 100)]
    expect(canvasesToRelease(live, visible, 400).map((c) => c.index)).toEqual([12])
  })

  it('hands back the very canvases it was given, so the caller knows which to release', () => {
    const detail = { index: 3, pixels: 300, kind: 'detail' as const }
    expect(canvasesToRelease([detail, canvas(10, 200)], visible, 250)).toEqual([detail])
  })
})
