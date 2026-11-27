/**
 * A page's links as the Original view draws them (docs/pdf-viewer.md § Stage 1: the Canvas 2D
 * viewer, *Links*): our own `<a>` elements over the page, placed with the arithmetic of pdf.js's
 * `AnnotationElement._createContainer` (build/pdf.mjs, Apache-2.0) in percentages of the page,
 * so a zoom never touches them; which web addresses are followed; and where an internal link's
 * destination lands.
 *
 * Framework-free and pdf.js-free: the engine hands in the annotation's fields and the page's
 * `view` (`[x1, y1, x2, y2]` in PDF user space, y up) as plain numbers.
 */

/** A link's box over the page, in percent of the unrotated page (the link layer turns with
 *  the page through `data-main-rotation`). */
export interface LinkBox {
  left: number
  top: number
  width: number
  height: number
}

/** Two PDF rectangles' overlap, normalised to `[left, bottom, right, top]`, or null when they
 *  do not overlap. */
function overlap(a: ArrayLike<number>, b: ArrayLike<number>): [number, number, number, number] | null {
  const left = Math.max(Math.min(a[0]!, a[2]!), Math.min(b[0]!, b[2]!))
  const bottom = Math.max(Math.min(a[1]!, a[3]!), Math.min(b[1]!, b[3]!))
  const right = Math.min(Math.max(a[0]!, a[2]!), Math.max(b[0]!, b[2]!))
  const top = Math.min(Math.max(a[1]!, a[3]!), Math.max(b[1]!, b[3]!))
  return right > left && top > bottom ? [left, bottom, right, top] : null
}

function percentBox([left, bottom, right, top]: [number, number, number, number], view: ArrayLike<number>): LinkBox {
  const width = view[2]! - view[0]!
  const height = view[3]! - view[1]!
  return {
    left: (100 * (left - view[0]!)) / width,
    top: (100 * (view[3]! - top)) / height,
    width: (100 * (right - left)) / width,
    height: (100 * (top - bottom)) / height,
  }
}

/**
 * The boxes to draw one link as, from its `rect` and `quadPoints`: one per quadrilateral when
 * it has them (a link over two lines of text is two boxes, not one over the space between),
 * each cut to the `rect` as pdf.js clips them; the `rect` alone otherwise. Every box lies
 * inside the page, and an empty one is left out.
 */
export function linkBoxes(
  rect: ArrayLike<number>,
  quadPoints: ArrayLike<number> | null | undefined,
  view: ArrayLike<number>,
): LinkBox[] {
  const whole = view[2]! > view[0]! && view[3]! > view[1]! ? overlap(rect, view) : null
  if (!whole) {
    return []
  }
  const boxes: LinkBox[] = []
  // Each quadrilateral is eight numbers, with its top right corner at 2-3 and its bottom left
  // at 4-5 (`_setQuadPoints` in build/pdf.mjs).
  for (let at = 0; quadPoints && at + 8 <= quadPoints.length; at += 8) {
    const quad = overlap([quadPoints[at + 4]!, quadPoints[at + 5]!, quadPoints[at + 2]!, quadPoints[at + 3]!], whole)
    if (quad) {
      boxes.push(percentBox(quad, view))
    }
  }
  return boxes.length > 0 ? boxes : [percentBox(whole, view)]
}

/** A link's web address, followed only over `http:` and `https:` (pdf.js also lets `ftp:`,
 *  `mailto:` and `tel:` through). Only the sanitised `url` is ever passed, never `unsafeUrl`. */
export function externalUrl(url: unknown): URL | null {
  if (typeof url !== 'string') {
    return null
  }
  // `new URL` rather than `URL.canParse`, which the browsers the legacy pdf.js build serves
  // may not have.
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed : null
}

/** A page reference in an explicit destination, as pdf.js hands it over. */
export interface RefLike {
  num: number
  gen: number
}

/**
 * An explicit destination, read: its page (a reference for `getPageIndex`, or a 0-based page
 * index, which some files write instead) and the part of the page it names, in PDF user space.
 * An `XYZ` point has `left` = `right` and `top` = `bottom`; `FitH` names a top only, `FitV` a
 * left only, `FitR` all four, `Fit` none. The destination's zoom is never read: following a
 * link never changes the zoom.
 */
export interface ExplicitDestination {
  page: { ref: RefLike } | { index: number }
  left: number | null
  bottom: number | null
  right: number | null
  top: number | null
}

function coordinate(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** An explicit destination array (`[page, { name }, ...args]`), or null for anything else, so
 *  a malformed link does nothing. */
export function parseExplicitDestination(dest: unknown): ExplicitDestination | null {
  if (!Array.isArray(dest) || dest.length < 2) {
    return null
  }
  const [target, kind] = dest as unknown[]
  let page: ExplicitDestination['page']
  if (Number.isInteger(target) && (target as number) >= 0) {
    page = { index: target as number }
  } else if (Number.isInteger(field(target, 'num')) && Number.isInteger(field(target, 'gen'))) {
    page = { ref: { num: field(target, 'num') as number, gen: field(target, 'gen') as number } }
  } else {
    return null
  }
  const [a, b, c, d] = [coordinate(dest[2]), coordinate(dest[3]), coordinate(dest[4]), coordinate(dest[5])]
  switch (field(kind, 'name')) {
    case 'XYZ':
      return { page, left: a, bottom: b, right: a, top: b }
    case 'FitH':
    case 'FitBH':
      return { page, left: null, bottom: a, right: null, top: a }
    case 'FitV':
    case 'FitBV':
      return { page, left: a, bottom: null, right: a, top: null }
    case 'FitR':
      return { page, left: a, bottom: b, right: c, top: d }
    case 'Fit':
    case 'FitB':
      return { page, left: null, bottom: null, right: null, top: null }
    default:
      return null
  }
}

function field(value: unknown, name: string): unknown {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>)[name] : undefined
}

/**
 * How far down the drawn page a destination's top is, as a fraction of the page's height
 * (for `destinationOffset`), or null when the destination names no top and the page's own top
 * is the place. `rotation` is the page's `/Rotate`: on a page turned a quarter, the PDF's x
 * runs down the screen. The arithmetic is `PageViewport`'s transform at scale 1.
 */
export function destinationFraction(dest: ExplicitDestination, view: ArrayLike<number>, rotation: number): number | null {
  const turn = ((rotation % 360) + 360) % 360
  const [x1, y1, x2, y2] = [view[0]!, view[1]!, view[2]!, view[3]!]
  const across = turn === 90 || turn === 270
  const height = across ? x2 - x1 : y2 - y1
  const down = (value: number) =>
    turn === 0 ? y2 - value : turn === 180 ? value - y1 : turn === 90 ? value - x1 : x2 - value
  let highest: number | null = null
  for (const value of across ? [dest.left, dest.right] : [dest.bottom, dest.top]) {
    if (value !== null) {
      highest = Math.min(highest ?? Infinity, down(value))
    }
  }
  if (highest === null || !(height > 0)) {
    return null
  }
  return Math.min(1, Math.max(0, highest / height))
}

/** The page (from 0) a named action goes to from page `index` of `count`, or null for an
 *  action the viewer does not follow. */
export function namedActionPage(action: unknown, index: number, count: number): number | null {
  switch (action) {
    case 'NextPage':
      return Math.min(count - 1, index + 1)
    case 'PrevPage':
      return Math.max(0, index - 1)
    case 'FirstPage':
      return 0
    case 'LastPage':
      return count - 1
    default:
      return null
  }
}
