import type { MaybeRefOrGetter } from 'vue'

/**
 * Holds off the new-build reload while `busy` is true (docs/web.md § Rules) — a page's way
 * of saying a reload right now would cost the user something. Released on unmount, so a
 * page that is left never blocks the pages after it.
 */
export function useBuildHold(busy: MaybeRefOrGetter<boolean>): void {
  const { $buildUpdate } = useNuxtApp()
  let release: (() => void) | null = null

  watch(
    () => toValue(busy),
    (held) => {
      if (held && !release) {
        release = $buildUpdate.hold()
      } else if (!held && release) {
        release()
        release = null
      }
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    release?.()
    release = null
  })
}
