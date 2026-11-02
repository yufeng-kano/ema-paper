/**
 * The part of the window the on-screen keyboard leaves visible, for the full-screen viewer
 * (docs/web.md § Result viewer, *On a phone and a tablet*, *The on-screen keyboard*).
 *
 * Safari on iOS lays the keyboard over the page: the viewer's fixed box keeps the layout
 * viewport's height, and only the visual viewport shrinks — and pans, to reveal a field near
 * the foot. The viewer then stands where the visual viewport is.
 */

/** Less than this between the box and the visual viewport is not a keyboard: the shortcut bar
 *  an iPad shows over a hardware keyboard, a rounding difference. */
export const KEYBOARD_MIN_PX = 100

/** A zoom past this is the reader's pinch, which the viewer never fights. */
const PINCH_SCALE = 1.01

export interface KeyboardFrame {
  /** The box's top, from the layout viewport's: the visual viewport's `offsetTop`. */
  top: number
  height: number
}

/**
 * Where the viewer's box stands while the keyboard is up, or null where it keeps its own
 * place: no keyboard, a pinch zoom, or a browser that already shrank the layout for the
 * keyboard (Chrome on Android, a Safari with `interactive-widget`), whose box and visual
 * viewport then agree. `box` is the box at rest — the overlay's, which never moves.
 */
export function keyboardFrame(
  box: { height: number },
  view: { offsetTop: number; height: number; scale: number },
): KeyboardFrame | null {
  if (view.scale > PINCH_SCALE || box.height - view.height < KEYBOARD_MIN_PX) {
    return null
  }
  return { top: Math.max(0, Math.round(view.offsetTop)), height: Math.round(view.height) }
}
