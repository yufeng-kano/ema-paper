/**
 * Loading state for one authenticated read, shared by every control-plane page: fetch on
 * mount, keep the last good data, and route failures through `useApiError`.
 *
 * Reads can overlap — a poll still in flight when an action refreshes — and only the newest
 * may write: an answer read before a change must not land after it and undo it on screen.
 */
export function useAuthedData<T>(load: () => Promise<T>) {
  const { resolve } = useApiError()
  const data = shallowRef<T | null>(null)
  const pending = ref(false)
  const errorMessage = ref<string | null>(null)
  /** Request generation: only the newest read may write. */
  let seq = 0

  async function refresh(): Promise<void> {
    const mine = ++seq
    pending.value = true
    errorMessage.value = null
    try {
      const loaded = await load()
      if (mine === seq) {
        data.value = loaded
      }
    } catch (error) {
      const message = await resolve(error)
      if (mine === seq) {
        errorMessage.value = message
      }
    } finally {
      if (mine === seq) {
        pending.value = false
      }
    }
  }

  onMounted(refresh)

  return { data, pending, errorMessage, refresh }
}
