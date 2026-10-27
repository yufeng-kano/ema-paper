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
