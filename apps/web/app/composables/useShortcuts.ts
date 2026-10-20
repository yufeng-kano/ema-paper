import { ariaShortcut, isApplePlatform, matchesShortcut, shortcutLabel, type KeyEventLike } from '~/lib/shortcuts'

/**
 * Shortcuts for the reader's platform (docs/web.md § Design system, *Shortcuts are labelled
 * for the platform*): `lib/shortcuts.ts` with the platform read once, in the browser.
 * `userAgentData` where the browser has it, since `navigator.platform` is frozen in some.
 */
export function useShortcuts() {
  const platform = import.meta.client
    ? ((navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ||
      navigator.platform ||
      navigator.userAgent)
    : ''
  const apple = isApplePlatform(platform)
  return {
    apple,
    label: (spec: string) => shortcutLabel(spec, apple),
    aria: (spec: string) => ariaShortcut(spec, apple),
    matches: (event: KeyEventLike, spec: string) => matchesShortcut(event, spec, apple),
  }
}
