import type { IconName } from './icons'

/**
 * Where a context menu opens (docs/web.md § Files).
 *
 * A menu asked to open at the pointer cannot simply take that point: a right-click near the
 * right edge of the window would put half the items off-screen, and a menu opened at the
 * bottom of a long list would be clipped by the viewport rather than scrolling with it. So
 * the point is a request, and this decides what actually fits — the way every desktop menu
 * does it: flip to the other side of the pointer first, clamp only if flipping is not enough.
 *
 * Pure, and separate from the component, because this is the part with edges worth testing.
 */

export interface MenuItem {
  /** Returned by `select`. Identifies the action, never displayed. */
  key: string
  label: string
  icon?: IconName
  /**
   * A destination rather than an action: the item is a real link. The locale items are why
   * — the locale lives in the URL, so switching it is navigation and must be an `<a>`.
   */
  to?: string
  /**
   * Present when the item is one of a set of choices, and `true` on the chosen one — drawn
   * with a check and announced as a radio rather than a plain item.
   */
  checked?: boolean
  /** Drawn in the danger color — delete, and nothing else so far. */
  danger?: boolean
  disabled?: boolean
  /** A group rule above this item, as a native menu separates its groups. */
  separated?: boolean
}

/** A fact about what the menu is about, drawn under its header: it takes no focus and does
 *  nothing, as the header does not (docs/web.md § Design system). */
export interface MenuFact {
  label: string
  value: string
  /** An identifier — a model id, a format name — set in the monospace face. */
  mono?: boolean
}

export interface Point {
  x: number
  y: number
}

export interface Size {
  width: number
  height: number
}

/** How close to the viewport edge a menu is allowed to sit. */
const MARGIN = 8

export function placeMenu(at: Point, menu: Size, viewport: Size, margin = MARGIN): Point {
  return {
    x: fit(at.x, menu.width, viewport.width, margin),
    y: fit(at.y, menu.height, viewport.height, margin),
  }
}

/**
 * One axis: keep the requested edge if the menu fits after it, otherwise put the menu before
 * the point. A menu taller or wider than the viewport itself fits nowhere, and then the near
 * edge wins — clipping the far end is better than starting off-screen.
 */
function fit(at: number, size: number, viewport: number, margin: number): number {
  if (at + size <= viewport - margin) {
    return Math.max(margin, at)
  }
  return Math.max(margin, Math.min(at - size, viewport - margin - size))
}
