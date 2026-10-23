/**
 * The reader's theme: `system` (follow `prefers-color-scheme`), `light` or `dark`.
 *
 * Applied as `data-theme` on `<html>`, which pins `color-scheme` and with it every
 * `light-dark()` token (docs/web.md § Design system, *Theme*). `system` is the absence of the
 * attribute, so a reader who never chose keeps following the system. The choice lives in
 * `localStorage`; an inline script in `<head>` (`nuxt.config.ts`) reads the same key before
 * first paint, so a pinned theme never flashes the other one on load.
 */
export type Theme = 'system' | 'light' | 'dark'

export const THEME_KEY = 'sightread.theme'
export const THEMES: readonly Theme[] = ['system', 'light', 'dark']

function readStored(): Theme {
  try {
    const value = localStorage.getItem(THEME_KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

function apply(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') {
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', theme)
  }
}

export function useTheme() {
  const theme = useState<Theme>('theme', () => 'system')

  /** Reads the stored choice and applies it — once, from the client plugin. */
  function load() {
    theme.value = readStored()
    apply(theme.value)
  }

  function set(next: Theme) {
    theme.value = next
    try {
      if (next === 'system') {
        localStorage.removeItem(THEME_KEY)
      } else {
        localStorage.setItem(THEME_KEY, next)
      }
    } catch {
      // Storage can be unavailable (private mode, blocked site data); the choice then
      // holds for this page only.
    }
    apply(next)
  }

  return { theme: readonly(theme), load, set }
}
