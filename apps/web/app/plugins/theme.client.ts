/** Applies the stored theme choice on the client (docs/web.md § Design system, *Theme*). */
export default defineNuxtPlugin(() => {
  useTheme().load()
})
