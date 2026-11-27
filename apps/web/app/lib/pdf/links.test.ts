import { describe, expect, it } from 'vitest'
import { destinationFraction, externalUrl, linkBoxes, namedActionPage, parseExplicitDestination } from './links'
import type { LinkBox } from './links'

const LETTER = [0, 0, 612, 792]

function inside(box: LinkBox): boolean {
  return box.left >= 0 && box.top >= 0 && box.left + box.width <= 100 + 1e-9 && box.top + box.height <= 100 + 1e-9
}

describe('link boxes', () => {
  it('places a link from the page’s top left, in percent, with y turned down', () => {
    const [box] = linkBoxes([61.2, 712.8, 122.4, 792], null, LETTER)
    expect(box!.left).toBeCloseTo(10, 9)
    expect(box!.top).toBeCloseTo(0, 9)
    expect(box!.width).toBeCloseTo(10, 9)
    expect(box!.height).toBeCloseTo(10, 9)
  })

  it('reads a rectangle written corner to corner either way', () => {
    expect(linkBoxes([122.4, 792, 61.2, 712.8], null, LETTER)).toEqual(linkBoxes([61.2, 712.8, 122.4, 792], null, LETTER))
  })

  it('measures from a page whose box does not start at the origin', () => {
    const [box] = linkBoxes([150, 250, 200, 300], null, [100, 200, 300, 400])
    expect(box).toEqual({ left: 25, top: 50, width: 25, height: 25 })
  })

  it('cuts a link to the page, and drops one with nothing left', () => {
    const [box] = linkBoxes([-50, -50, 306, 396], null, LETTER)
    expect(inside(box!)).toBe(true)
    expect(box).toEqual({ left: 0, top: 50, width: 50, height: 50 })
    expect(linkBoxes([700, 0, 800, 100], null, LETTER)).toEqual([])
    expect(linkBoxes([10, 10, 10, 50], null, LETTER)).toEqual([])
  })

  it('draws one box per line of a link over two lines, cut to the link', () => {
    // Two quadrilaterals, each top left, top right, bottom left, bottom right: x 300-650 on the
    // upper line, which runs past the rect, and 0-200 on the lower.
    const quads = [300, 700, 650, 700, 300, 680, 650, 680, 0, 660, 200, 660, 0, 640, 200, 640]
    const boxes = linkBoxes([0, 640, 612, 700], quads, LETTER)
    expect(boxes).toHaveLength(2)
    expect(boxes.every(inside)).toBe(true)
    expect(boxes[0]!.left).toBeCloseTo((100 * 300) / 612, 9)
    expect(boxes[0]!.left + boxes[0]!.width).toBeCloseTo(100, 9)
    expect(boxes[1]!.top).toBeCloseTo((100 * (792 - 660)) / 792, 9)
  })

  it('falls back to the rectangle when no quadrilateral is usable', () => {
    expect(linkBoxes([0, 0, 100, 100], [1, 2, 3], LETTER)).toEqual(linkBoxes([0, 0, 100, 100], null, LETTER))
    expect(linkBoxes([0, 0, 100, 100], [0, 0, 0, 0, 0, 0, 0, 0], LETTER)).toEqual(linkBoxes([0, 0, 100, 100], null, LETTER))
  })
})

describe('external links', () => {
  it('follows http and https only', () => {
    expect(externalUrl('https://example.org/a?b#c')?.host).toBe('example.org')
    expect(externalUrl('http://example.org:8080/')?.host).toBe('example.org:8080')
    for (const url of ['mailto:a@example.org', 'tel:+1555', 'ftp://example.org/', 'javascript:alert(1)', 'not a url', '', undefined, 42]) {
      expect(externalUrl(url)).toBeNull()
    }
  })
})

describe('explicit destinations', () => {
  it('reads a page reference or a page index, and nothing else', () => {
    expect(parseExplicitDestination([{ num: 12, gen: 0 }, { name: 'Fit' }])?.page).toEqual({ ref: { num: 12, gen: 0 } })
    expect(parseExplicitDestination([4, { name: 'Fit' }])?.page).toEqual({ index: 4 })
    expect(parseExplicitDestination([-1, { name: 'Fit' }])).toBeNull()
    expect(parseExplicitDestination([1.5, { name: 'Fit' }])).toBeNull()
    expect(parseExplicitDestination([{ num: 'x' }, { name: 'Fit' }])).toBeNull()
    expect(parseExplicitDestination([4, { name: 'Zoom' }])).toBeNull()
    expect(parseExplicitDestination([4])).toBeNull()
    expect(parseExplicitDestination('chapter-2')).toBeNull()
  })

  it('reads the part of the page each kind names, never its zoom', () => {
    expect(parseExplicitDestination([0, { name: 'XYZ' }, 72, 700, 2])).toEqual({ page: { index: 0 }, left: 72, bottom: 700, right: 72, top: 700 })
    expect(parseExplicitDestination([0, { name: 'XYZ' }, null, null, null])).toEqual({ page: { index: 0 }, left: null, bottom: null, right: null, top: null })
    expect(parseExplicitDestination([0, { name: 'FitH' }, 500])).toMatchObject({ top: 500, left: null })
    expect(parseExplicitDestination([0, { name: 'FitBV' }, 30])).toMatchObject({ left: 30, top: null })
    expect(parseExplicitDestination([0, { name: 'FitR' }, 10, 20, 30, 40])).toMatchObject({ left: 10, bottom: 20, right: 30, top: 40 })
  })

  it('finds the destination’s top as a fraction down the drawn page', () => {
    const xyz = parseExplicitDestination([0, { name: 'XYZ' }, 0, 594])!
    expect(destinationFraction(xyz, LETTER, 0)).toBeCloseTo(0.25, 12)
    expect(destinationFraction(xyz, LETTER, 180)).toBeCloseTo(0.75, 12)
    expect(destinationFraction(xyz, LETTER, -180)).toBeCloseTo(0.75, 12)
    const fitR = parseExplicitDestination([0, { name: 'FitR' }, 153, 198, 306, 594])!
    expect(destinationFraction(fitR, LETTER, 0)).toBeCloseTo(0.25, 12)
    expect(destinationFraction(fitR, LETTER, 90)).toBeCloseTo(0.25, 12)
    expect(destinationFraction(fitR, LETTER, 270)).toBeCloseTo(0.5, 12)
  })

  it('measures from a page box that does not start at the origin', () => {
    const dest = parseExplicitDestination([0, { name: 'FitH' }, 350])!
    expect(destinationFraction(dest, [100, 200, 300, 400], 0)).toBeCloseTo(0.25, 12)
  })

  it('keeps the top on the page, and gives none where the destination names none', () => {
    expect(destinationFraction(parseExplicitDestination([0, { name: 'FitH' }, 5000])!, LETTER, 0)).toBe(0)
    expect(destinationFraction(parseExplicitDestination([0, { name: 'FitH' }, -10])!, LETTER, 0)).toBe(1)
    expect(destinationFraction(parseExplicitDestination([0, { name: 'Fit' }])!, LETTER, 0)).toBeNull()
    expect(destinationFraction(parseExplicitDestination([0, { name: 'FitV' }, 100])!, LETTER, 0)).toBeNull()
    expect(destinationFraction(parseExplicitDestination([0, { name: 'FitH' }, 100])!, LETTER, 90)).toBeNull()
  })
})

describe('named actions', () => {
  it('steps and jumps inside the document, and ignores the rest', () => {
    expect(namedActionPage('NextPage', 3, 10)).toBe(4)
    expect(namedActionPage('NextPage', 9, 10)).toBe(9)
    expect(namedActionPage('PrevPage', 0, 10)).toBe(0)
    expect(namedActionPage('PrevPage', 3, 10)).toBe(2)
    expect(namedActionPage('FirstPage', 3, 10)).toBe(0)
    expect(namedActionPage('LastPage', 3, 10)).toBe(9)
    expect(namedActionPage('GoBack', 3, 10)).toBeNull()
    expect(namedActionPage('Print', 3, 10)).toBeNull()
  })
})
