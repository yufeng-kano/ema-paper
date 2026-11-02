/**
 * The result viewer's two side cards — the notes and the agent — and their widths
 * (docs/web.md § Result viewer, *Column widths*): the default, the clamp, and the line below
 * which a drag hides a card. What the window allows on top of these is measured by the
 * viewer.
 */
export type Column = 'notes' | 'agent'

/** `initial` is the width a card opens at until a drag, and what a double-click on a grip
 *  goes back to. */
export const COLUMN_WIDTHS = {
  notes: { initial: 320, min: 240, max: 560 },
  agent: { initial: 360, min: 300, max: 640 },
} as const satisfies Record<Column, { initial: number; min: number; max: number }>

/** A width held to the card's own bounds, in whole pixels. */
export function clampWidth(column: Column, width: number): number {
  const { min, max } = COLUMN_WIDTHS[column]
  return Math.round(Math.min(max, Math.max(min, width)))
}

/** A width read back from the browser: a number is clamped, anything else is the default. */
export function storedWidth(column: Column, width: unknown): number {
  return typeof width === 'number' && Number.isFinite(width) ? clampWidth(column, width) : COLUMN_WIDTHS[column].initial
}

/** A drag that would make the card narrower than half its minimum hides it — the rule the
 *  system's split views follow. `width` is the drag's raw width, before any clamp. */
export function collapses(column: Column, width: number): boolean {
  return width < COLUMN_WIDTHS[column].min / 2
}

/**
 * The viewer's three layouts (docs/web.md § Result viewer, *On a phone and a tablet*): a phone
 * shows one pane at a time, a tablet the document and one card, the desktop the document and
 * both. A phone is a narrow window, or a touch screen no taller than a phone turned sideways —
 * a short desktop window with a mouse stays a desktop.
 */
export type ViewerLayout = 'phone' | 'tablet' | 'desktop'

/** Below this width, one pane at a time: the document's minimum beside the smallest card no
 *  longer fits. */
export const PHONE_MAX_WIDTH = 700
/** Up to this width, one card at a time beside the document. */
export const TABLET_MAX_WIDTH = 900
/** A touch screen no taller than this is a phone on its side. */
export const PHONE_MAX_HEIGHT = 500

export function viewerLayout(view: { width: number; height: number }, coarse: boolean): ViewerLayout {
  if (view.width < PHONE_MAX_WIDTH || (coarse && view.height <= PHONE_MAX_HEIGHT)) {
    return 'phone'
  }
  return view.width <= TABLET_MAX_WIDTH ? 'tablet' : 'desktop'
}

/**
 * Which cards show beside the document: every open one on the desktop; on a tablet only the
 * one in front where both are open — the other stays mounted behind it. A phone has panes, not
 * cards, and shows none of them as a card.
 */
export function shownCards(layout: ViewerLayout, open: Record<Column, boolean>, front: Column): Record<Column, boolean> {
  if (layout === 'phone') {
    return { notes: false, agent: false }
  }
  if (layout === 'tablet' && open.notes && open.agent) {
    return { notes: front === 'notes', agent: front === 'agent' }
  }
  return { ...open }
}
