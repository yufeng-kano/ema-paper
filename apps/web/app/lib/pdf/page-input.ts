/**
 * The page indicator's field (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Page
 * indicator*): what the reader typed comes to a page to jump to, or to nothing.
 *
 * One rule for Enter and for leaving the field (Tab, a click elsewhere, iPhone's *完成*), on
 * every platform: a number jumps, clamped to the document; a blank or anything else cancels
 * and the indicator shows the current page again. What differs, where focus goes afterwards,
 * is the viewer's.
 */

/** A page number as typed, or null for a blank or anything but digits. Full-width digits, which
 *  a Chinese input method may type, count as digits. */
export function parsePageNumber(text: string): number | null {
  const digits = text.normalize('NFKC').trim()
  return /^\d+$/.test(digits) ? Number(digits) : null
}

/** The page to jump to, from 1 and clamped to 1…`total`, or null for nothing to do: a blank, a
 *  non-number, or the page already current (typing it changes nothing). */
export function pageInputTarget(text: string, current: number, total: number): number | null {
  const typed = parsePageNumber(text)
  if (typed === null) {
    return null
  }
  const page = Math.min(total, Math.max(1, typed))
  return page === current ? null : page
}
