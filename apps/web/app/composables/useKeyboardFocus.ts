import type { Ref } from 'vue'

/**
 * Whether the keyboard is moving focus, for a viewer pane's ring (docs/media-viewer.md
 * § Accessibility): true from a Tab press, false from any pointer press. The pane binds it as
 * `.keyboard-focus` beside its `.keyboard-pane` class, and the rule in `main.css` draws the
 * ring, so every original's viewer draws the same one. Not `:focus-visible`, which a key
 * pressed after a click turns on (docs/web.md § Files).
 */
export function useKeyboardFocus(): Ref<boolean> {
  const keyboardFocus = ref(false)

  /** Tab, ⇧Tab and Safari's ⌥Tab move focus; with ⌘ or Ctrl held, Tab switches apps or tabs. */
  function onTabPress(event: KeyboardEvent) {
    if (event.key === 'Tab' && !event.metaKey && !event.ctrlKey) {
      keyboardFocus.value = true
    }
  }

  function onPointerPress() {
    keyboardFocus.value = false
  }

  onMounted(() => {
    window.addEventListener('keydown', onTabPress, true)
    window.addEventListener('keyup', onTabPress, true)
    window.addEventListener('pointerdown', onPointerPress, true)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onTabPress, true)
    window.removeEventListener('keyup', onTabPress, true)
    window.removeEventListener('pointerdown', onPointerPress, true)
  })

  return keyboardFocus
}
