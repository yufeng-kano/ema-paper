import type { Ref } from 'vue'
import { COLUMN_WIDTHS, collapses, type Column } from '~/lib/columns'

/** What a host gives its grip. */
export interface ColumnGripOptions {
  column: Column
  /** The side of its grip the card is on: a drag or an arrow away from the grip widens it. */
  side: 'left' | 'right'
  /** The card's width as drawn. */
  width: Ref<number>
  /** Draws the card at `value`, as far as the window allows. */
  setWidth: (value: number) => void
  /** Keeps `value` as the reader's chosen width. */
  choose: (value: number) => void
  /** The card, which a drag that hides it takes the focus out of. */
  card: () => HTMLElement | null
  /** A release with the card hidden: it closes. `startWidth` is its width before the drag. */
  close: (startWidth: number) => void
}

/** Bound with `v-bind`, not `v-on`: the keys are attribute names. */
interface GripHandlers {
  onPointerdown: (event: PointerEvent) => void
  onPointermove: (event: PointerEvent) => void
  onPointerup: () => void
  onPointercancel: () => void
  onDblclick: () => void
  onKeydown: (event: KeyboardEvent) => void
}

/**
 * The grip that resizes a card (docs/web.md § Column widths): the drag, the card hidden while
 * a drag holds it below half its minimum, the focus a hidden card held, the cancel, the arrow
 * keys and the double-click back to the initial width. One instance per card: the result
 * viewer's notes and agent cards, and the Files page's Agent card.
 *
 * The host keeps what differs: how far the window lets the card grow (`setWidth` clamps), the
 * grip's and the card's layout, and what a release while hidden does (`close`). The cursor the
 * drag holds over the whole page is the global `:root.col-resizing` rule.
 */
export function useColumnGrip(options: ColumnGripOptions) {
  const { column, side, width, setWidth, choose, card, close } = options
  /** The drag: where it started, and the card's width then. */
  const resizing = ref<{ x: number; width: number } | null>(null)
  /** A drag below half the minimum hides the card while it lasts, and closes it if it ends there. */
  const hidden = ref(false)
  /** What held focus in the card a drag hid: hiding a card drops its focus to the page, so it
   *  is given back as the card returns. */
  let heldFocus: HTMLElement | null = null

  function holdFocus() {
    const element = card()
    const active = document.activeElement
    heldFocus = element && active instanceof HTMLElement && element.contains(active) ? active : null
  }

  /** Called once the card is shown again in state: the wait is for the render that shows it,
   *  since a card still hidden takes no focus. */
  async function giveFocusBack() {
    const held = heldFocus
    heldFocus = null
    await nextTick()
    if (held?.isConnected && (!document.activeElement || document.activeElement === document.body)) {
      held.focus({ preventScroll: true })
    }
  }

  function stop() {
    resizing.value = null
    hidden.value = false
    document.documentElement.classList.remove('col-resizing')
  }

  function onPointerdown(event: PointerEvent) {
    if (event.button !== 0 || !(event.currentTarget instanceof HTMLElement)) {
      return
    }
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    resizing.value = { x: event.clientX, width: width.value }
    // The column-resize cursor holds over the whole page while the drag lasts.
    document.documentElement.classList.add('col-resizing')
  }

  function onPointermove(event: PointerEvent) {
    const drag = resizing.value
    if (!drag) {
      return
    }
    const moved = event.clientX - drag.x
    const next = drag.width + (side === 'left' ? moved : -moved)
    // Below half its minimum the card goes, live, and what is beside it takes its room;
    // dragged back past that line it returns at its minimum.
    const hide = collapses(column, next)
    if (hide !== hidden.value) {
      if (hide) {
        holdFocus()
      }
      hidden.value = hide
      if (!hide) {
        // After the change, so its wait is for the render that shows the card again.
        void giveFocusBack()
      }
    }
    if (!hide) {
      setWidth(next)
    }
  }

  /** The drag's release: a card released hidden closes, its chosen width the one it had before
   *  the drag; otherwise the width as it now stands is the reader's choice — unless the press
   *  moved nothing, which chooses nothing. */
  function onPointerup() {
    const drag = resizing.value
    if (!drag) {
      return
    }
    const wasHidden = hidden.value
    stop()
    if (wasHidden) {
      heldFocus = null
      close(drag.width)
    } else if (width.value !== drag.width) {
      choose(width.value)
    }
  }

  /** A cancelled drag puts the width back and never closes the card. */
  function onPointercancel() {
    const drag = resizing.value
    if (drag) {
      const wasHidden = hidden.value
      stop()
      width.value = drag.width
      if (wasHidden) {
        void giveFocusBack()
      }
    }
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return
    }
    event.preventDefault()
    // The arrow moves the edge: away from the grip's side widens the card. It stops at the
    // card's minimum: only a drag puts a card away.
    const outward = event.key === (side === 'left' ? 'ArrowRight' : 'ArrowLeft')
    const before = width.value
    const step = event.shiftKey ? 64 : 16
    setWidth(before + (outward ? step : -step))
    // A key that moves nothing — the edge already at a bound, its own or the window's —
    // chooses nothing, as a press that drags nothing does not.
    if (width.value !== before) {
      choose(width.value)
    }
  }

  onBeforeUnmount(() => {
    if (resizing.value) {
      document.documentElement.classList.remove('col-resizing')
    }
  })

  const handlers: GripHandlers = {
    onPointerdown,
    onPointermove,
    onPointerup,
    onPointercancel,
    onDblclick: () => choose(COLUMN_WIDTHS[column].initial),
    onKeydown,
  }

  return {
    /** A drag is running: the host's clamp leaves the card under the pointer alone. */
    dragging: computed(() => resizing.value !== null),
    /** The drag holds the card hidden. */
    hidden: readonly(hidden),
    handlers,
  }
}
