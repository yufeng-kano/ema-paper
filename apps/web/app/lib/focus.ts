/**
 * The Tab trap that modal surfaces share — `UiModal` and the signed-in layout's drawer: what
 * can take focus inside the surface, and Tab wrapping from its last control to its first and
 * back. Each caller keeps its own handling of a surface with nothing focusable, its Escape
 * and its check that it is the topmost surface.
 */

/** What Tab can reach. A `<video controls>` and an `<audio controls>` are named: Tab reaches
 *  their native controls though they carry no `tabindex` (and WebKit reports `tabIndex` -1 for
 *  them), while a bare `<video>` or `<audio>` is no Tab stop. */
export const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), video[controls], audio[controls], [tabindex]:not([tabindex="-1"])'

/** The controls inside `container` that Tab can reach, in document order. A hidden one
 *  (no `offsetParent`) and one inside an inert region — a pane of the phone's reader that
 *  is not shown — take no focus; the focused element always counts, so the wrap can tell
 *  where focus is. */
export function focusablesIn(container: HTMLElement | null): HTMLElement[] {
  if (!container) {
    return []
  }
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) => (el.offsetParent !== null && !el.closest('[inert]')) || el === document.activeElement,
  )
}

/**
 * Keeps a Tab press inside `container`: Shift+Tab on the first control — or on the container
 * itself, which holds focus when it opens — goes to the last, Tab on the last goes to the
 * first, and either one pulls focus back in from outside (a click on the scrim, a control
 * that disabled itself) rather than letting it walk through the page behind. `items` is
 * `focusablesIn(container)` and must not be empty.
 */
export function wrapTab(event: KeyboardEvent, container: HTMLElement, items: HTMLElement[]): void {
  const first = items[0]!
  const last = items[items.length - 1]!
  const active = document.activeElement
  const outside = !container.contains(active)

  if (event.shiftKey && (active === first || active === container || outside)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || outside)) {
    event.preventDefault()
    first.focus()
  }
}
