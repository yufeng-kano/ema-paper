import type { InjectionKey, Ref } from 'vue'
import type { IconName } from './icons'

/**
 * The open dialog's panel, provided by `UiModal`. A menu opened inside a dialog is teleported
 * there rather than to `<body>`: the panel is `aria-modal`, so assistive tech treats anything
 * outside it as hidden, and the dialog's Tab trap treats it as outside too.
 */
export const modalPanelKey: InjectionKey<Ref<HTMLElement | null>> = Symbol('modal-panel')

/** Whether a dialog is inside one held above the screen's own dialogs (`UiModal`'s `above`):
 *  a dialog opened from inside it stands over it rather than going under it. */
export const modalAboveKey: InjectionKey<boolean> = Symbol('modal-above')

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
  /** A destination rather than an action: the item is drawn as a real link — the account
   *  menu's Admin row. */
  to?: string
  /** An address outside the app's routes, drawn as a plain `<a href>`: a file the browser shows
   *  itself, such as the result viewer's *Open in new tab* to the original. Disabled, it keeps
   *  no `href`, so there is nothing to follow. */
  href?: string
  /** With `href`: opened in a new tab (`target="_blank"`, `rel="noopener noreferrer"`), so the
   *  page that opened it can neither be reached from it nor named to it as the referrer. */
  newTab?: boolean
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
  /** Drawn right-aligned in `--faint`: the item's key (`⌘⇧↑`), or — `shortcutMono` — the
   *  markdown or command it stands for (`##`, `/translate`). */
  shortcut?: string
  shortcutMono?: boolean
  /** Drawn after the label on the same line in `--muted`: a disabled choice's reason, or a
   *  model's id beside its name (docs/web.md § Design system, *Primitives*). */
  description?: string
  /** A submenu, flown out beside this row: a short list of parallel choices under one action. */
  children?: MenuItem[]
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

interface Size {
  width: number
  height: number
}

/** How close to the viewport edge a menu is allowed to sit. */
const MARGIN = 8
/** How far down and in from its row's lower left a submenu opens over its parent. */
const INSET = 8

/** Below this much room over the point, a menu asked to open above it opens as any other. */
const ROOM_ABOVE_MIN = 120

/**
 * The tallest a menu opened `above` a point may be: the room between the point and the
 * window's top margin, so a long list scrolls inside that room rather than being pushed
 * down over the control it opened from. Null when there is too little room to be worth it.
 */
export function heightAbove(at: Point): number | null {
  const room = at.y - MARGIN
  return room >= ROOM_ABOVE_MIN ? room : null
}

/** Where a menu asked for at `at` opens. `above` opens it over the point rather than under
 *  it, wherever there is room there: a menu asked for at the top of a control low in the
 *  window, which it must not cover. */
export function placeMenu(at: Point, menu: Size, viewport: Size, above = false): Point {
  return {
    x: fit(at.x, menu.width, viewport.width),
    y: above ? fitAbove(at.y, menu.height, viewport.height) : fit(at.y, menu.height, viewport.height),
  }
}

/**
 * One axis: keep the requested edge if the menu fits after it, otherwise put the menu before
 * the point. A menu taller or wider than the viewport itself fits nowhere, and then the near
 * edge wins — clipping the far end is better than starting off-screen.
 */
function fit(at: number, size: number, viewport: number): number {
  if (at + size <= viewport - MARGIN) {
    return Math.max(MARGIN, at)
  }
  return Math.max(MARGIN, Math.min(at - size, viewport - MARGIN - size))
}

/** The same axis the other way first: ending at the point when the menu fits before it, and
 *  only otherwise placed as `fit` places it. */
function fitAbove(at: number, size: number, viewport: number): number {
  if (at - size >= MARGIN) {
    return Math.min(at - size, viewport - MARGIN - size)
  }
  return fit(at, size, viewport)
}

/** A box in viewport coordinates. */
interface Rect {
  left: number
  top: number
  right: number
  bottom: number
}

/**
 * Which side of a line a surface hung from it opens on, inside the pane that scrolls them —
 * the slash menu and the TeX field (docs/web.md § Editing): under the line, unless the pane
 * has less room there than the surface `needs` and more over the line, where it then stands.
 * With the room on that side, `gap` kept clear at the line and at the pane's edge.
 */
export function openSide(
  line: Pick<Rect, 'top' | 'bottom'>,
  pane: Pick<Rect, 'top' | 'bottom'>,
  needs: number,
  gap: number,
): { above: boolean; room: number } {
  const below = pane.bottom - line.bottom - 2 * gap
  const above = line.top - pane.top - 2 * gap
  return below < needs && above > below ? { above: true, room: above } : { above: false, room: below }
}

/**
 * Where a submenu opens (docs/web.md § Design system, *Primitives*): beside its parent menu,
 * top-aligned with the row it came from — to the right, or to the left where the viewport has
 * no room — and, where neither side has room (a phone), over the parent, `INSET` down and in
 * from the row's lower left, so the row stays in sight. Kept inside the viewport vertically.
 */
export function placeSubmenu(row: Rect, menu: Rect, sub: Size, viewport: Size): Point {
  const y = (top: number) => Math.max(MARGIN, Math.min(top, viewport.height - MARGIN - sub.height))
  if (menu.right + sub.width <= viewport.width - MARGIN) {
    return { x: menu.right, y: y(row.top) }
  }
  if (menu.left - sub.width >= MARGIN) {
    return { x: menu.left - sub.width, y: y(row.top) }
  }
  return {
    x: Math.max(MARGIN, Math.min(row.left + INSET, viewport.width - MARGIN - sub.width)),
    y: y(row.bottom + INSET),
  }
}

/** How far a floating card keeps from the control it hangs from. */
const CARD_GAP = 6

interface CardPlaceOptions {
  /** The side of the control the card opens on wherever it fits there. */
  prefer: 'above' | 'below'
}

/**
 * Where a floating card opens beside the control that opened it — the context ring's card
 * and the context card (docs/web.md § The agent panel): `CARD_GAP` from the control, on the side
 * it prefers when it fits there, else on the other side when it fits there, else under.
 * Under, it is moved up as far as it must to end inside the window's margin, never past the
 * top one; over, it ends `CARD_GAP` above the control. Its left edge is the control's, kept
 * inside the window.
 */
export function placeCard(
  anchor: Pick<Rect, 'left' | 'top' | 'bottom'>,
  card: Size,
  viewport: Size,
  options: CardPlaceOptions,
): Point {
  const fitsAbove = card.height <= anchor.top - CARD_GAP - MARGIN
  const fitsBelow = card.height <= viewport.height - MARGIN - (anchor.bottom + CARD_GAP)
  const over = options.prefer === 'above' ? fitsAbove : fitsAbove && !fitsBelow
  return {
    x: Math.max(MARGIN, Math.min(anchor.left, viewport.width - MARGIN - card.width)),
    y: over
      ? anchor.top - CARD_GAP - card.height
      : Math.min(anchor.bottom + CARD_GAP, Math.max(MARGIN, viewport.height - MARGIN - card.height)),
  }
}
