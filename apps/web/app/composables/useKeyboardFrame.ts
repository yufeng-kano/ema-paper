import type { Ref } from 'vue'
import { keyboardFrame, type KeyboardFrame } from '~/lib/viewport'

/** WebKit fires one `resize` as the keyboard's ~200ms animation starts, and another when the
 *  QuickType or Passwords bar appears: after a field takes focus it is measured this often,
 *  for this long. */
const SETTLE_STEP_MS = 50
const SETTLE_FOR_MS = 600
/** How far above the pane's foot the caret is kept. */
const CARET_GAP_PX = 24

/** A field the on-screen keyboard comes up for. */
function typesText(element: Element | null): element is HTMLElement {
  if (!(element instanceof HTMLElement)) {
    return false
  }
  if (element.isContentEditable || element instanceof HTMLTextAreaElement) {
    return true
  }
  return element instanceof HTMLInputElement && !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(element.type)
}

/**
 * The viewer's box while the on-screen keyboard is up (docs/web.md § Result viewer, *On a phone
 * and a tablet*, *The on-screen keyboard*): where the visual viewport stands while a text field
 * inside `panel` has focus, null otherwise. `box` is the overlay, which the keyboard never
 * moves; `enabled` is read once, at mount — the full-screen dialog's only.
 */
export function useKeyboardFrame(box: Ref<HTMLElement | null>, panel: Ref<HTMLElement | null>, enabled: boolean) {
  const frame = ref<KeyboardFrame | null>(null)
  let pending = 0
  let settle: ReturnType<typeof setInterval> | undefined
  let settleEnd: ReturnType<typeof setTimeout> | undefined

  function editing(): boolean {
    const active = document.activeElement
    return typesText(active) && Boolean(panel.value?.contains(active))
  }

  function measure() {
    pending = 0
    const view = window.visualViewport
    const rest = box.value?.getBoundingClientRect()
    const next = view && rest && editing() ? keyboardFrame(rest, view) : null
    const resized = next !== null && next.height !== frame.value?.height
    if (next?.top !== frame.value?.top || next?.height !== frame.value?.height) {
      frame.value = next
    }
    // A box made shorter can leave the caret under its foot: brought back into its pane once
    // the box is drawn at its new height. Not on a pan alone, which the reader's own
    // scrolling also makes.
    if (resized) {
      void nextTick(() => requestAnimationFrame(revealCaret))
    }
  }

  function schedule() {
    if (!pending) {
      pending = requestAnimationFrame(measure)
    }
  }

  /** The caret's line, or the field itself, scrolled up into its pane if the foot covers it. */
  function revealCaret() {
    const active = document.activeElement
    if (!typesText(active)) {
      return
    }
    const selection = window.getSelection()
    const anchor = active.isContentEditable && selection?.rangeCount ? selection.anchorNode : null
    let rect: DOMRect | null = null
    if (anchor) {
      rect = selection!.getRangeAt(0).getBoundingClientRect()
      if (!rect.height) {
        // A caret on an empty line has no box of its own: its line's.
        rect = (anchor instanceof Element ? anchor : anchor.parentElement)?.getBoundingClientRect() ?? null
      }
    } else if (!active.isContentEditable) {
      rect = active.getBoundingClientRect()
    }
    let scroller = (anchor instanceof Element ? anchor : anchor?.parentElement) ?? active.parentElement
    while (scroller && scroller !== panel.value) {
      const overflow = getComputedStyle(scroller).overflowY
      if ((overflow === 'auto' || overflow === 'scroll') && scroller.scrollHeight > scroller.clientHeight) {
        break
      }
      scroller = scroller.parentElement
    }
    if (!rect || !scroller || scroller === panel.value) {
      return
    }
    const limit = scroller.getBoundingClientRect().bottom - CARET_GAP_PX
    if (rect.bottom > limit) {
      scroller.scrollTop += rect.bottom - limit
    }
  }

  function onFocusIn(event: FocusEvent) {
    if (!typesText(event.target as Element | null) || !panel.value?.contains(event.target as Node)) {
      return
    }
    schedule()
    clearInterval(settle)
    clearTimeout(settleEnd)
    settle = setInterval(schedule, SETTLE_STEP_MS)
    settleEnd = setTimeout(() => clearInterval(settle), SETTLE_FOR_MS)
  }

  /** WebKit reports the close only after its animation: dropped at once when focus leaves the
   *  fields, not when the visual viewport says so. */
  function onFocusOut(event: FocusEvent) {
    const next = event.relatedTarget as Element | null
    if (!typesText(next) || !panel.value?.contains(next)) {
      clearInterval(settle)
      frame.value = null
    }
  }

  function onVisibility() {
    if (document.visibilityState === 'hidden') {
      frame.value = null
    }
  }

  onMounted(() => {
    if (!enabled) {
      return
    }
    window.visualViewport?.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('scroll', schedule)
    window.addEventListener('resize', schedule)
    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('focusout', onFocusOut)
    document.addEventListener('visibilitychange', onVisibility)
  })

  onBeforeUnmount(() => {
    if (!enabled) {
      return
    }
    cancelAnimationFrame(pending)
    clearInterval(settle)
    clearTimeout(settleEnd)
    window.visualViewport?.removeEventListener('resize', schedule)
    window.visualViewport?.removeEventListener('scroll', schedule)
    window.removeEventListener('resize', schedule)
    document.removeEventListener('focusin', onFocusIn)
    document.removeEventListener('focusout', onFocusOut)
    document.removeEventListener('visibilitychange', onVisibility)
  })

  return frame
}
