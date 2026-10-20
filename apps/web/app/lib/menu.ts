import type { InjectionKey, Ref } from 'vue'
import type { IconName } from './icons'

/**
 * The open dialog's panel, provided by `UiModal`. A menu opened inside a dialog is teleported
 * there rather than to `<body>`: the panel is `aria-modal`, so assistive tech treats anything
 * outside it as hidden, and the dialog's Tab trap treats it as outside too.
 */
export const modalPanelKey: InjectionKey<Ref<HTMLElement | null>> = Symbol('modal-panel')

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

export interface PlaceOptions {
  margin?: number
  /** Open over the point rather than under it, wherever there is room there: a menu asked
   *  for at the top of a control low in the window, which it must not cover. */
  above?: boolean
}

export function placeMenu(at: Point, menu: Size, viewport: Size, options: PlaceOptions = {}): Point {
  const margin = options.margin ?? MARGIN
  return {
    x: fit(at.x, menu.width, viewport.width, margin),
    y: options.above ? fitAbove(at.y, menu.height, viewport.height, margin) : fit(at.y, menu.height, viewport.height, margin),
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

/** The same axis the other way first: ending at the point when the menu fits before it, and
 *  only otherwise placed as `fit` places it. */
function fitAbove(at: number, size: number, viewport: number, margin: number): number {
  if (at - size >= margin) {
    return Math.min(at - size, viewport - margin - size)
  }
  return fit(at, size, viewport, margin)
}
