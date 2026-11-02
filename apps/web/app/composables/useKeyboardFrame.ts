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

/** The text styles a textarea wraps its lines by, copied onto the copy that measures it. */
const WRAP_STYLES = [
  'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'fontFamily', 'fontSize', 'fontStyle', 'fontWeight', 'fontStretch', 'letterSpacing', 'wordSpacing',
  'lineHeight', 'textIndent', 'textTransform', 'tabSize', 'whiteSpace', 'overflowWrap', 'wordBreak',
] as const

/**
 * The caret's line in a textarea, in the viewport. A textarea exposes no box for its caret:
 * its text up to the caret is laid out at the field's width in a hidden copy, and the line the
 * copy ends on is the caret's — a contents page's line wraps on a phone, so counting line
 * breaks would put the caret too high.
 */
function textareaCaret(field: HTMLTextAreaElement): DOMRect {
  const style = getComputedStyle(field)
  const copy = document.createElement('div')
  for (const name of WRAP_STYLES) {
    copy.style[name] = style[name]
  }
  Object.assign(copy.style, {
    position: 'absolute',
    top: '0',
    left: '0',
    visibility: 'hidden',
    boxSizing: 'border-box',
    width: `${field.clientWidth}px`,
  })
  copy.textContent = field.value.slice(0, field.selectionEnd)
  const caret = document.createElement('span')
  caret.textContent = '\u200b'
  copy.append(caret)
  document.body.append(copy)
  const top = caret.offsetTop
  const height = caret.offsetHeight
  copy.remove()
  const box = field.getBoundingClientRect()
  return new DOMRect(box.left, box.top + field.clientTop + top - field.scrollTop, 0, height)
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

  /** The caret's line, or a one-line field, scrolled up into its pane if the foot covers it. */
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
    } else if (active instanceof HTMLTextAreaElement) {
      rect = textareaCaret(active)
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
    const room = scroller.getBoundingClientRect()
    // Never past the top of the box revealed: a caret's line that has no box of its own falls
    // back to its element's, which can be taller than the pane.
    const by = Math.min(rect.bottom - (room.bottom - CARET_GAP_PX), rect.top - room.top)
    if (by > 0) {
      scroller.scrollTop += by
    }
  }

  function onFocusIn(event: FocusEvent) {
    if (!typesText(event.target as Element | null) || !panel.value?.contains(event.target as Node)) {
      // Focus arriving anywhere else has left the fields, whether or not one said so: WebKit
      // sends no `blur` for a field unmounted with focus — a block's editor closed by Escape.
      dropFrame()
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
  function dropFrame() {
    clearInterval(settle)
    frame.value = null
  }

  /** Bound to `blur` on capture rather than `focusout`: Chrome sends a field unmounted as it
   *  loses focus — a block's editor closed by a tap on another block — its `focusout` only once
   *  it has left the page, where nothing on the document hears it. */
  function onBlur(event: FocusEvent) {
    const next = event.relatedTarget as Element | null
    if (!typesText(next) || !panel.value?.contains(next)) {
      dropFrame()
    }
  }

  function onVisibility() {
    if (document.visibilityState === 'hidden') {
      dropFrame()
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
    document.addEventListener('blur', onBlur, true)
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
    document.removeEventListener('blur', onBlur, true)
    document.removeEventListener('visibilitychange', onVisibility)
  })

  return frame
}
