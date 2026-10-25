import { describe, expect, it } from 'vitest'
import { heightAbove, openSide, placeMenu, placeSubmenu } from './menu'

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

  it('holds a long menu asked for above to the room over the point, so it never covers it', () => {
    // An outline of 30 headings under a control at 761 in an 800-high window.
    const room = heightAbove({ x: 900, y: 761 })
    expect(room).toBe(753)
    const held = { width: 280, height: Math.min(784, room!) }
    expect(placeMenu({ x: 900, y: 761 }, held, viewport, { above: true }).y + held.height).toBe(761)
    // Too little room over the point: no cap, and the menu opens as any other.
    expect(heightAbove({ x: 900, y: 100 })).toBeNull()
  })

  it('falls back to the near edge when nothing fits', () => {
    // A viewport smaller than the menu — flipping runs off the near edge, so the margin wins
    // and the far end is clipped. Showing the top of the menu beats starting it off-screen.
    expect(placeMenu({ x: 5, y: 5 }, menu, { width: 150, height: 150 })).toEqual({ x: 8, y: 8 })
  })
})

describe('openSide', () => {
  // A pane from 100 to 900, a line 20 high; 4 kept clear at the line and at the pane's edge.
  const pane = { top: 100, bottom: 900 }

  it('opens under the line where the pane has room there for all it needs', () => {
    expect(openSide({ top: 400, bottom: 420 }, pane, 300, 4)).toEqual({ above: false, room: 472 })
  })

  it('stands over the line where the room under it is short and the room over it larger', () => {
    // 900 - 780 - 8 = 112 under, 760 - 100 - 8 = 652 over.
    expect(openSide({ top: 760, bottom: 780 }, pane, 300, 4)).toEqual({ above: true, room: 652 })
  })

  it('stays under the line when the room over it is no larger, short or not', () => {
    // A pane too short for the surface on either side: under wins a tie.
    expect(openSide({ top: 490, bottom: 510 }, pane, 600, 4)).toEqual({ above: false, room: 382 })
    expect(openSide({ top: 140, bottom: 160 }, { top: 100, bottom: 300 }, 300, 4)).toEqual({ above: false, room: 132 })
  })

  it('opens under a line whose room there fits exactly what the surface needs', () => {
    expect(openSide({ top: 760, bottom: 780 }, pane, 112, 4)).toEqual({ above: false, room: 112 })
  })
})

describe('placeSubmenu', () => {
  const row = { left: 100, top: 200, right: 300, bottom: 230 }
  const parent = { left: 96, top: 150, right: 304, bottom: 400 }
  const sub = { width: 220, height: 260 }

  it('opens to the right of the menu, top-aligned with its row', () => {
    expect(placeSubmenu(row, parent, sub, viewport)).toEqual({ x: 304, y: 200 })
  })

  it('opens to the left where the right has no room', () => {
    const right = { left: 700, top: 150, right: 900, bottom: 400 }
    expect(placeSubmenu({ ...row, left: 704, right: 896 }, right, sub, viewport)).toEqual({ x: 480, y: 200 })
  })

  it('opens over the parent, down and in from the row, where neither side has room', () => {
    const phone = { width: 390, height: 800 }
    const wide = { left: 20, top: 150, right: 370, bottom: 400 }
    expect(placeSubmenu({ left: 24, top: 200, right: 366, bottom: 230 }, wide, sub, phone)).toEqual({ x: 32, y: 238 })
  })

  it('stays inside the viewport vertically', () => {
    expect(placeSubmenu({ ...row, top: 700, bottom: 730 }, parent, sub, viewport)).toEqual({ x: 304, y: 532 })
  })
})
