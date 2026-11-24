import type { Ref } from 'vue'

/**
 * What keeps a floating card — the context ring's and the context card (docs/web.md § The
 * agent panel) — placed and dismissed while it is open: placed again whenever the card's
 * size or the window's changes, and closed by a press outside it or by focus leaving it.
 * The control that opened it is not outside: its own click closes the card.
 *
 * `start` once the card is mounted and placed, `stop` as it goes; bind `onFocusout` on the
 * card. Escape, focus return and following the control stay with each card.
 */
export function useFloatingCard(options: {
  card: Ref<HTMLElement | null>
  /** The control that opened the card. */
  anchor: () => HTMLElement | null
  place: () => void
  close: () => void
}) {
  let observer: ResizeObserver | null = null

  function outside(target: EventTarget | null): boolean {
    return target instanceof Node && !options.card.value?.contains(target) && !options.anchor()?.contains(target)
  }

  function onPointerdown(event: PointerEvent) {
    if (outside(event.target)) {
      options.close()
    }
  }

  function onFocusout(event: FocusEvent) {
    if (outside(event.relatedTarget)) {
      options.close()
    }
  }

  function start() {
    observer = new ResizeObserver(() => options.place())
    if (options.card.value) {
      observer.observe(options.card.value)
    }
    window.addEventListener('pointerdown', onPointerdown, true)
    window.addEventListener('resize', options.place)
  }

  function stop() {
    observer?.disconnect()
    observer = null
    window.removeEventListener('pointerdown', onPointerdown, true)
    window.removeEventListener('resize', options.place)
  }

  return { start, stop, onFocusout }
}
