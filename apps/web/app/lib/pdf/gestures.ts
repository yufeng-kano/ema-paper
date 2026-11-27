/**
 * The Original view's zoom gestures (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer,
 * *Zoom*, *Phone*): a pinch, however the browser reports it, and a double tap. This reads the
 * events and reports a pinch as a start, a cumulative ratio and an end; the viewer turns that
 * into the CSS transform during the gesture and the zoom commit after it.
 *
 * A pinch comes as one of three:
 * - `wheel` with `ctrlKey`, from a trackpad in Chrome and Firefox (and Ctrl with a mouse wheel);
 * - WebKit's `gesturestart`, `gesturechange` and `gestureend`, from a trackpad in Safari, and on
 *   iOS beside the touches;
 * - two fingers on a touch screen.
 * Only one is followed at a time. Every `preventDefault` here is there because the browser
 * would otherwise zoom the whole web page instead of the PDF; one-finger scrolling and the long
 * press that selects text are never touched.
 */

import { SETTLE_MS } from './layout'

export interface GesturePoint {
  clientX: number
  clientY: number
}

export interface PinchChange extends GesturePoint {
  /** The scale since the pinch started: 2 is twice as large. */
  ratio: number
}

export interface GestureHandlers {
  /** A pinch began about this point. */
  start(point: GesturePoint): void
  /** It moved on; the point is the pointer or the fingers' midpoint now. */
  change(change: PinchChange): void
  /** It is over: commit the zoom. */
  end(): void
  /** Two taps of one finger close together, not on a link. */
  doubleTap?(point: GesturePoint): void
}

/** Browsers write one wheel notch as anything from 3 lines to 120 px; past this many px an
 *  event counts as this many, so a mouse wheel with Ctrl steps rather than leaps. A trackpad
 *  pinch sends small deltas and is never cut. */
const WHEEL_DELTA_LIMIT = 25

/** A line of a line-mode wheel event (Firefox with a mouse), in px. */
const WHEEL_LINE_PX = 16

const DOUBLE_TAP_MS = 300
const DOUBLE_TAP_DISTANCE = 30
/** How far a finger may move and still tap rather than scroll. */
const TAP_SLOP = 10

/** WebKit's `GestureEvent`, which TypeScript's DOM types do not have. */
interface GestureEventLike extends Event {
  scale: number
  clientX: number
  clientY: number
}

function midpoint(a: Touch, b: Touch): GesturePoint {
  return { clientX: (a.clientX + b.clientX) / 2, clientY: (a.clientY + b.clientY) / 2 }
}

function spread(a: Touch, b: Touch): number {
  return Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY)
}

/**
 * Follows pinches and double taps on `pane` until the returned function is called or `signal`
 * aborts. Neither of those calls `end` for a pinch in progress.
 */
export function attachPinch(pane: HTMLElement, handlers: GestureHandlers, signal?: AbortSignal): () => void {
  const controller = new AbortController()
  const options = { signal: controller.signal }
  const blocking = { signal: controller.signal, passive: false }
  let source: 'wheel' | 'gesture' | 'touch' | null = null
  let ratio = 1
  let wheelTimer: ReturnType<typeof setTimeout> | undefined
  let fingers = 0
  let startSpread = 0
  let touchMoves: AbortController | null = null

  function begin(kind: 'wheel' | 'gesture' | 'touch', point: GesturePoint): void {
    if (source) {
      finish()
    }
    source = kind
    ratio = 1
    handlers.start(point)
  }

  function finish(): void {
    clearTimeout(wheelTimer)
    touchMoves?.abort()
    touchMoves = null
    if (source) {
      source = null
      handlers.end()
    }
  }

  pane.addEventListener(
    'wheel',
    (event) => {
      if (!event.ctrlKey) {
        return
      }
      // Chrome and Firefox zoom the whole web page on Ctrl and the wheel, which is how they
      // report a trackpad pinch.
      event.preventDefault()
      if (source === 'gesture' || source === 'touch') {
        return
      }
      const point = { clientX: event.clientX, clientY: event.clientY }
      if (source !== 'wheel') {
        begin('wheel', point)
      }
      const pixels =
        event.deltaMode === WheelEvent.DOM_DELTA_LINE
          ? event.deltaY * WHEEL_LINE_PX
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
            ? event.deltaY * pane.clientHeight
            : event.deltaY
      // pdf.js's viewer turns a pinch's wheel delta into a zoom factor the same way
      // (`Math.exp(-deltaY / 100)`, web/app.js `_onWheel`).
      ratio *= Math.exp(-Math.max(-WHEEL_DELTA_LIMIT, Math.min(WHEEL_DELTA_LIMIT, pixels)) / 100)
      handlers.change({ ratio, ...point })
      clearTimeout(wheelTimer)
      wheelTimer = setTimeout(finish, SETTLE_MS)
    },
    blocking,
  )

  for (const type of ['gesturestart', 'gesturechange', 'gestureend'] as const) {
    pane.addEventListener(
      type,
      (raw) => {
        const event = raw as GestureEventLike
        // Safari zooms the whole web page on a trackpad pinch (and iOS on a two-finger one)
        // unless its gesture events are cancelled.
        event.preventDefault()
        // On iOS the same pinch also arrives as touches, and the touch handlers report it.
        if (fingers > 0 || source === 'touch') {
          return
        }
        const point = { clientX: event.clientX, clientY: event.clientY }
        if (type === 'gesturestart') {
          begin('gesture', point)
        } else if (source === 'gesture') {
          if (type === 'gesturechange') {
            ratio = event.scale
            handlers.change({ ratio, ...point })
          } else {
            finish()
          }
        }
      },
      blocking,
    )
  }

  let tap: (GesturePoint & { time: number; target: EventTarget | null; multi: boolean }) | null = null
  let lastTap: (GesturePoint & { time: number }) | null = null

  function onTouchMove(event: TouchEvent): void {
    if (source !== 'touch' || event.touches.length < 2) {
      return
    }
    // Two fingers moving: Safari would zoom the whole web page (touch-action alone does not
    // stop it in every version) and scroll the pane under the pinch.
    event.preventDefault()
    const [a, b] = [event.touches[0]!, event.touches[1]!]
    ratio = startSpread > 0 ? spread(a, b) / startSpread : 1
    handlers.change({ ratio, ...midpoint(a, b) })
  }

  // Not passive, as pdf.js's TouchManager, so that the touchmove listener added for a second
  // finger takes effect within the same touch sequence. It never cancels anything itself.
  pane.addEventListener(
    'touchstart',
    (event) => {
      fingers = event.touches.length
      if (fingers === 1) {
        const touch = event.touches[0]!
        tap = { clientX: touch.clientX, clientY: touch.clientY, time: event.timeStamp, target: event.target, multi: false }
        return
      }
      if (tap) {
        tap.multi = true
      }
      if (fingers === 2) {
        const [a, b] = [event.touches[0]!, event.touches[1]!]
        startSpread = spread(a, b)
        begin('touch', midpoint(a, b))
        touchMoves = new AbortController()
        pane.addEventListener('touchmove', onTouchMove, { signal: AbortSignal.any([controller.signal, touchMoves.signal]), passive: false })
      }
    },
    blocking,
  )

  function onTouchEnd(event: TouchEvent): void {
    fingers = event.touches.length
    if (source === 'touch' && fingers < 2) {
      finish()
    }
    if (fingers > 0 || !tap) {
      return
    }
    const ended = event.changedTouches[0]
    const first = tap
    tap = null
    if (
      event.type !== 'touchend' ||
      !ended ||
      first.multi ||
      Math.hypot(ended.clientX - first.clientX, ended.clientY - first.clientY) > TAP_SLOP ||
      event.timeStamp - first.time > DOUBLE_TAP_MS ||
      (first.target instanceof Element && first.target.closest('a'))
    ) {
      lastTap = null
      return
    }
    const point = { clientX: first.clientX, clientY: first.clientY }
    if (
      lastTap &&
      first.time - lastTap.time <= DOUBLE_TAP_MS &&
      Math.hypot(point.clientX - lastTap.clientX, point.clientY - lastTap.clientY) <= DOUBLE_TAP_DISTANCE
    ) {
      lastTap = null
      handlers.doubleTap?.(point)
    } else {
      lastTap = { ...point, time: event.timeStamp }
    }
  }

  pane.addEventListener('touchend', onTouchEnd, options)
  pane.addEventListener('touchcancel', onTouchEnd, options)

  function detach(): void {
    clearTimeout(wheelTimer)
    controller.abort()
    touchMoves = null
    source = null
  }
  if (signal?.aborted) {
    detach()
  } else {
    signal?.addEventListener('abort', detach, { once: true, signal: controller.signal })
  }
  return detach
}
