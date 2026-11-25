import type { Ref } from 'vue'
import type { Point } from '~/lib/menu'

/**
 * A button that toggles a context menu opened over it, ending just above it — the composer's
 * foot controls (docs/web.md § The agent panel). Bind `onPointerdown` and `toggle` on the
 * button, and `at`, `fromKey` (as `focus-first`) and `close` on the `UiContextMenu`.
 *
 * The press that closed the menu (a press outside it, caught first) is the same press that
 * lands on the button: its click must not open the menu again. A pick, Escape or a press
 * elsewhere leaves the next press free. A click the keyboard made carries no pointer detail
 * and no press came before it, so it always toggles, and the menu focuses its first row.
 */
export function useMenuButton(
  button: Ref<HTMLElement | null>,
  hooks: {
    /** Runs as the menu opens, before it is placed. */
    onOpen?: () => void
    /** Runs when the menu closes itself (a pick, Escape, a press outside). */
    onClose?: () => void
  } = {},
) {
  const at = ref<Point | null>(null)
  const fromKey = ref(false)
  /** The event that last closed the menu, if one did. */
  let closedBy: Event | null = null
  /** The press now on the button is the one that closed the menu. */
  let pressClosedMenu = false

  function onPointerdown(event: PointerEvent) {
    pressClosedMenu = closedBy === event
  }

  function toggle(event: MouseEvent) {
    const keyed = event.detail === 0
    if (at.value || (pressClosedMenu && !keyed)) {
      pressClosedMenu = false
      at.value = null
      return
    }
    const box = button.value?.getBoundingClientRect()
    fromKey.value = keyed
    hooks.onOpen?.()
    // The controls sit at the foot of the column: a menu opened down would cover them.
    at.value = box ? { x: box.left, y: box.top - 4 } : { x: 0, y: 0 }
  }

  function close(cause?: Event) {
    at.value = null
    closedBy = cause ?? null
    hooks.onClose?.()
  }

  return { at, fromKey, onPointerdown, toggle, close }
}
