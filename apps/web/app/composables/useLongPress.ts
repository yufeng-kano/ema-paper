import type { Point } from '~/lib/menu'

/**
 * The touch half of "right-click for actions" (docs/web.md § Files).
 *
 * A pointer held still on a row for half a second is the same gesture as a right-click, and
 * on a phone it is the only one there is. Four details are what make it feel native rather
 * than accidental:
 *
 *  - **A mouse is not involved.** It has a right button, and `contextmenu` is a better event
 *    than any timer — so a mouse press starts nothing here.
 *  - **A drag is not a press.** Moving past a few pixels is a scroll or a drag, and both must
 *    cancel the hold rather than open a menu under a finger that has left.
 *  - **Text is not selected under the finger.** `holding` is true from a touch press to its
 *    release, so a page can turn selection off for exactly that long — keyed to the gesture,
 *    not to the device, because a touchscreen laptop with a trackpad reports `hover: hover`.
 *  - **The click that follows is eaten.** The browser still sends one when the finger lifts,
 *    and a long press on a document would otherwise open the result viewer behind its own
 *    menu. `onClickCapture` runs before the row's own button and stops it.
 *
 * One instance serves a whole screen: only one finger can be holding at a time, so the state
 * is shared and the row is whatever `on()` closed over.
 */

/** How long a finger has to rest before the menu opens. The platform convention. */
const HOLD_MS = 500
/** How far it may wander first — past this it is a scroll or a drag, not a press. */
const SLOP = 10
/**
 * The innermost element wins. A press on a row is also a press on the list behind it, and
 * without a claim the bubbling event would start the list's hold *after* the row's and open
 * the wrong menu (the same rule `lib/dnd.ts` follows for `dragover`).
 */
const CLAIMED = Symbol('long-press')

/** Bound with `v-bind`, not `v-on`: the keys are attribute names, as `rowAttrs` needs them. */
export interface PressHandlers {
  onPointerdown: (event: PointerEvent) => void
  onPointermove: (event: PointerEvent) => void
  onPointerup: () => void
  onPointercancel: () => void
  onClickCapture: (event: MouseEvent) => void
}

export function useLongPress() {
  let timer: ReturnType<typeof setTimeout> | undefined
  let origin: Point | null = null
  /** Set when the hold opened a menu, and cleared by the click it has to swallow. */
  let fired = false
  /** A touch or pen is down. Outlives `cancel()`: a menu opening mid-hold is still a hold. */
  const holding = ref(false)

  function cancel() {
    clearTimeout(timer)
    timer = undefined
    origin = null
  }

  function release() {
    cancel()
    holding.value = false
  }

  onBeforeUnmount(cancel)

  /** Handlers for one element. `open` gets the point the finger has been resting on. */
  function on(open: (point: Point) => void): PressHandlers {
    return {
      onPointerdown: (event: PointerEvent) => {
        const claimable = event as PointerEvent & { [CLAIMED]?: true }
        if (claimable[CLAIMED]) {
          return
        }
        claimable[CLAIMED] = true
        fired = false
        cancel()
        if (event.pointerType === 'mouse') {
          return
        }
        holding.value = true
        const at = { x: event.clientX, y: event.clientY }
        origin = at
        timer = setTimeout(() => {
          timer = undefined
          fired = true
          open(at)
        }, HOLD_MS)
      },
      onPointermove: (event: PointerEvent) => {
        if (origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > SLOP) {
          cancel()
        }
      },
      onPointerup: release,
      onPointercancel: release,
      onClickCapture: (event: MouseEvent) => {
        if (!fired) {
          return
        }
        fired = false
        event.preventDefault()
        event.stopPropagation()
      },
    }
  }

  return { on, cancel, holding }
}
