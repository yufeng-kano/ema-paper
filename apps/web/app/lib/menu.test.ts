import { describe, expect, it } from 'vitest'
import { placeMenu } from './menu'

const viewport = { width: 1000, height: 800 }
const menu = { width: 200, height: 300 }

describe('placeMenu', () => {
  it('opens at the pointer when the menu fits there', () => {
    expect(placeMenu({ x: 120, y: 200 }, menu, viewport)).toEqual({ x: 120, y: 200 })
  })

  it('flips to the other side of the pointer at the right and bottom edges', () => {
    // 950 + 200 would run off the right; 700 + 300 off the bottom.
    expect(placeMenu({ x: 950, y: 700 }, menu, viewport)).toEqual({ x: 750, y: 400 })
  })

  it('never leaves the flipped menu past the far edge either', () => {
    // A click in the very corner: flipping puts the menu at 790/490, both inside the margin.
    expect(placeMenu({ x: 990, y: 790 }, menu, viewport)).toEqual({ x: 790, y: 490 })
  })

  it('ends a menu asked for above at the point, even where it would fit below', () => {
    // A control at 500 with 300 to spare under it: the menu still ends at its top.
    expect(placeMenu({ x: 120, y: 500 }, menu, viewport, { above: true })).toEqual({ x: 120, y: 200 })
  })

  it('opens a menu asked for above under the point when there is no room over it', () => {
    expect(placeMenu({ x: 120, y: 200 }, menu, viewport, { above: true })).toEqual({ x: 120, y: 200 })
    // No room on either side: the near edge, as for any menu.
    expect(placeMenu({ x: 5, y: 100 }, menu, { width: 150, height: 150 }, { above: true })).toEqual({ x: 8, y: 8 })
  })

  it('falls back to the near edge when nothing fits', () => {
    // A viewport smaller than the menu — flipping runs off the near edge, so the margin wins
    // and the far end is clipped. Showing the top of the menu beats starting it off-screen.
    expect(placeMenu({ x: 5, y: 5 }, menu, { width: 150, height: 150 })).toEqual({ x: 8, y: 8 })
  })
})
