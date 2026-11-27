import { describe, expect, it } from 'vitest'
import {
  boxPx,
  createLayout,
  currentPage,
  DESKTOP_SPACING,
  destinationOffset,
  jumpOffset,
  pageAt,
  pageRect,
  pagesToMeasure,
  PHONE_SPACING,
  renderQueue,
  renderWindow,
  scaleLayout,
  setPageSizes,
  visibleRange,
  visibleRect,
} from './layout'

const LETTER = { width: 612, height: 792 }
const LANDSCAPE = { width: 792, height: 612 }

describe('page boxes', () => {
  it('draws a Letter page at 816 × 1056 at 100%, in whole px rounded down', () => {
    const layout = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    expect([layout.widths[0], layout.heights[0]]).toEqual([816, 1056])
    expect(boxPx(612, 1.1)).toBe(Math.floor(612 * 1.1 * (96 / 72)))
  })

  it('fills the width a fit scale was measured for, without losing a pixel to rounding', () => {
    for (const width of [375, 390, 414, 1024, 1179]) {
      expect(boxPx(LETTER.width, width / (LETTER.width * (96 / 72)))).toBe(width)
    }
  })

  it('stacks pages under the top padding, a gap apart, and closes with the padding', () => {
    const layout = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    expect(Array.from(layout.tops)).toEqual([16, 16 + 1056 + 16, 16 + 2 * (1056 + 16)])
    expect(layout.height).toBe(16 + 3 * 1056 + 2 * 16 + 16)
    expect(layout.width).toBe(816 + 2 * 16)
  })

  it('holds unread pages at page 1’s size until they are measured', () => {
    const layout = createLayout(4, LETTER, 1, DESKTOP_SPACING)
    expect(Array.from(layout.measured)).toEqual([1, 0, 0, 0])
    expect(Array.from(layout.heights)).toEqual([1056, 1056, 1056, 1056])
  })

  it('gives each zoom a layout of its own, keeping the sizes read so far', () => {
    const layout = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    setPageSizes(layout, [{ index: 2, size: LANDSCAPE }], 0)
    const twice = scaleLayout(layout, 2)
    expect(twice.widths[2]).toBe(2 * 1056)
    expect(twice.measured[2]).toBe(1)
    expect(layout.widths[0]).toBe(816)
    setPageSizes(twice, [{ index: 1, size: LANDSCAPE }], 0)
    expect(layout.measured[1]).toBe(0)
  })

  it('makes the column as wide as the widest page and its gutters', () => {
    const layout = createLayout(3, LETTER, 1, PHONE_SPACING)
    setPageSizes(layout, [{ index: 1, size: LANDSCAPE }], 0)
    expect(layout.width).toBe(1056)
  })
})

describe('finding pages', () => {
  const layout = createLayout(5, LETTER, 1, DESKTOP_SPACING)
  const pitch = 1056 + 16

  it('finds the page under a point, the one above a gap, and page 1 above the column', () => {
    expect(pageAt(layout, 0)).toBe(0)
    expect(pageAt(layout, 16 + pitch)).toBe(1)
    expect(pageAt(layout, 16 + pitch - 1)).toBe(0)
    expect(pageAt(layout, 16 + 1056 + 8)).toBe(0)
    expect(pageAt(layout, 1e9)).toBe(4)
  })

  it('lists the pages crossing a band, edges excluded, and none for a band inside a gap', () => {
    expect(visibleRange(layout, 0, 800)).toEqual({ first: 0, last: 0 })
    expect(visibleRange(layout, 16 + 1056, 16 + pitch)).toBeNull()
    expect(visibleRange(layout, 16 + 1056 - 1, 16 + pitch + 1)).toEqual({ first: 0, last: 1 })
    expect(visibleRange(layout, 1e9, 1e9 + 800)).toBeNull()
  })

  it('takes the page filling most of the pane as current, the lower number on a tie', () => {
    expect(currentPage(layout, 0, 800)).toBe(0)
    // Page 1 shows 400px and page 2 shows 384px.
    expect(currentPage(layout, 16 + 1056 - 400, 800)).toBe(0)
    // Page 1 shows 392px and page 2 the same.
    expect(currentPage(layout, 16 + 1056 - 392, 800)).toBe(0)
    expect(currentPage(layout, 16 + 1056 - 391, 800)).toBe(1)
  })

  it('counts a short page that is whole in view against a tall one cut by the pane', () => {
    const mixed = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    setPageSizes(mixed, [{ index: 1, size: { width: 612, height: 150 } }], 0)
    // Page 2 is 200px tall and whole in view; page 1 shows 300px, then 100px.
    expect(currentPage(mixed, 16 + 1056 - 300, 600)).toBe(0)
    expect(currentPage(mixed, 16 + 1056 - 100, 400)).toBe(1)
  })

  it('draws the pages in view and one either side, nearest the middle first', () => {
    expect(renderWindow(layout, 0, 800)).toEqual({ first: 0, last: 1 })
    expect(renderWindow(layout, 2 * pitch, 800)).toEqual({ first: 1, last: 3 })
    expect(renderWindow(layout, layout.height - 800, 800)).toEqual({ first: 3, last: 4 })
    const scroll = 16 + pitch + 1056 - 300
    expect(renderQueue(layout, scroll, 800)).toEqual([2, 1, 3, 0])
  })

  it('reads sizes within a pane height of the pane, nearest first, and only for unread pages', () => {
    const fresh = createLayout(8, LETTER, 0.5, DESKTOP_SPACING)
    const pitchHalf = 528 + 16
    const scroll = 16 + 3 * pitchHalf
    expect(pagesToMeasure(fresh, scroll, 600)).toEqual([3, 4, 2, 5, 1])
    setPageSizes(fresh, [{ index: 4, size: LETTER }], scroll)
    expect(pagesToMeasure(fresh, scroll, 600)).not.toContain(4)
  })
})

describe('size changes', () => {
  it('compensates a change above the pane exactly, and leaves the rest in place', () => {
    const layout = createLayout(6, LETTER, 1, DESKTOP_SPACING)
    const scrollTop = layout.tops[3]! + 100
    const before = layout.tops[3]! - scrollTop
    const correction = setPageSizes(
      layout,
      [
        { index: 1, size: LANDSCAPE },
        { index: 4, size: LANDSCAPE },
      ],
      scrollTop,
    )
    expect(correction).toBe(816 - 1056)
    expect(layout.tops[3]! - (scrollTop + correction)).toBe(before)
  })

  it('treats a page cut by the pane’s top as above it, so the part in view stays put', () => {
    const layout = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    const scrollTop = layout.tops[1]! + 500
    const next = layout.tops[2]! - scrollTop
    const correction = setPageSizes(layout, [{ index: 1, size: { width: 612, height: 900 } }], scrollTop)
    expect(correction).toBe(1200 - 1056)
    expect(layout.tops[2]! - (scrollTop + correction)).toBe(next)
  })

  it('changes a page at the pane’s top in place', () => {
    const layout = createLayout(3, LETTER, 1, DESKTOP_SPACING)
    expect(setPageSizes(layout, [{ index: 1, size: LANDSCAPE }], layout.tops[1]!)).toBe(0)
    expect(setPageSizes(layout, [], 1e6)).toBe(0)
  })
})

describe('jumps and destinations', () => {
  const layout = createLayout(4, LETTER, 1, DESKTOP_SPACING)

  it('puts a page’s top one gap below the pane’s top', () => {
    expect(jumpOffset(layout, 0)).toBe(0)
    expect(jumpOffset(layout, 2)).toBe(layout.tops[2]! - 16)
    const phone = createLayout(4, LETTER, 0.5, PHONE_SPACING)
    expect(jumpOffset(phone, 1)).toBe(phone.tops[1]! - 10)
  })

  it('puts a destination’s top where a jump puts a page’s, and a page’s top without one', () => {
    expect(destinationOffset(layout, 2, null)).toBe(jumpOffset(layout, 2))
    expect(destinationOffset(layout, 2, 0)).toBe(jumpOffset(layout, 2))
    expect(destinationOffset(layout, 2, 0.5)).toBe(layout.tops[2]! + 528 - 16)
  })
})

describe('page rectangles', () => {
  const layout = createLayout(2, LETTER, 1, DESKTOP_SPACING)

  it('centres a page in a pane wider than the column, and in the column when it is wider', () => {
    expect(pageRect(layout, 0, 1200)).toEqual({ left: (1200 - 816) / 2, top: 16, width: 816, height: 1056 })
    expect(pageRect(layout, 0, 500).left).toBe(16)
  })

  it('keeps a page that fits the pane centred in it when a wider page widens the column', () => {
    const phone = createLayout(2, LETTER, 390 / 816, PHONE_SPACING)
    setPageSizes(phone, [{ index: 1, size: { width: 792, height: 612 } }], 0)
    expect(phone.width).toBeGreaterThan(390)
    expect(pageRect(phone, 0, 390).left).toBe(0)
    expect(pageRect(phone, 1, 390).left).toBe(0)
  })

  it('gives the part of a page in view from the page’s top left, and nothing for a page out of view', () => {
    expect(visibleRect(layout, 0, { left: 100, top: 216 }, { width: 400, height: 300 })).toEqual({
      left: 84,
      top: 200,
      width: 400,
      height: 300,
    })
    expect(visibleRect(layout, 0, { left: 0, top: 0 }, { width: 400, height: 300 })).toEqual({
      left: 0,
      top: 0,
      width: 384,
      height: 284,
    })
    expect(visibleRect(layout, 1, { left: 0, top: 0 }, { width: 400, height: 300 })).toBeNull()
  })
})
