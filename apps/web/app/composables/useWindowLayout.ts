import type { Ref } from 'vue'
import { viewerLayout, type ViewerLayout } from '~/lib/columns'

/**
 * The window's viewer layout for the file viewer (docs/media-viewer.md § The shell, *On a
 * phone*): `viewerLayout` read at once — Files renders on the client, so the first paint is
 * already the right one — and again on a resize and when the pointer changes between fine and
 * coarse. The result viewer keeps its own `readLayout`, since it also keeps the coarse pointer
 * for its toolbar and assigns its layout from those same two listeners; the rule itself is
 * `viewerLayout` in both, so only this plumbing is written twice.
 */
export function useWindowLayout(): Readonly<Ref<ViewerLayout>> {
  const coarseQuery = window.matchMedia('(pointer: coarse)')

  function readLayout(): ViewerLayout {
    return viewerLayout({ width: window.innerWidth, height: window.innerHeight }, coarseQuery.matches)
  }

  const layout = ref<ViewerLayout>(readLayout())

  function onChange() {
    layout.value = readLayout()
  }

  onMounted(() => {
    window.addEventListener('resize', onChange)
    coarseQuery.addEventListener('change', onChange)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('resize', onChange)
    coarseQuery.removeEventListener('change', onChange)
  })

  return readonly(layout)
}
