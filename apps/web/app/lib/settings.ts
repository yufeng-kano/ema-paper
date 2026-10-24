import type { IconName } from './icons'

/**
 * The sections of the settings dialog, in the order its section list shows them
 * (docs/web.md § Pages, *The settings dialog*). Shared by the dialog and by the layout that
 * reads `?settings=<section>` off the route, so the two can never disagree on what a section is.
 * `tint` is the token of the rounded square each section's glyph sits on, as the system's
 * settings window draws its list.
 */
export const SETTINGS_SECTIONS = [
  { key: 'general', icon: 'settings', tint: '--tint-gray' },
  { key: 'account', icon: 'user', tint: '--tint-blue' },
  { key: 'usage', icon: 'dashboard', tint: '--tint-green' },
  { key: 'agent', icon: 'agent', tint: '--tint-purple' },
  { key: 'keys', icon: 'keys', tint: '--tint-orange' },
  { key: 'mcp', icon: 'connect', tint: '--tint-indigo' },
] as const satisfies readonly { key: string; icon: IconName; tint: string }[]

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]['key']

/** The query parameter that opens the dialog: `?settings=general`. */
export const SETTINGS_QUERY = 'settings'

/**
 * Where a visitor meant to go before sign-in stood in the way — the pricing page's *Upgrade*
 * is the Usage section. Kept for this tab only (`sessionStorage`), read once by the
 * sign-in page's signed-in redirect, and never a path off this origin (docs/web.md § Pages).
 */
const AFTER_SIGN_IN_KEY = 'sightread.after-sign-in'

export function keepAfterSignIn(path: string): void {
  try {
    sessionStorage.setItem(AFTER_SIGN_IN_KEY, path)
  } catch {
    // Storage blocked: the visitor lands on Files as before.
  }
}

/** The kept destination, taken as it is read; null when none, or when it is not a path here. */
export function takeAfterSignIn(): string | null {
  try {
    const path = sessionStorage.getItem(AFTER_SIGN_IN_KEY)
    sessionStorage.removeItem(AFTER_SIGN_IN_KEY)
    return path && path.startsWith('/') && !path.startsWith('//') ? path : null
  } catch {
    return null
  }
}

/**
 * The section a route's `settings` query names: `null` when the dialog is closed, *general*
 * when it is open on a section that does not exist, as the account menu would open it.
 * `billing` is *usage*: the section's name until 2026-10-24, and still where Kano Pay's
 * checkout and portal send the reader back to (docs/billing.md § Kano Pay).
 */
export function settingsSectionOf(value: unknown): SettingsSection | null {
  if (value === undefined) {
    return null
  }
  if (value === 'billing') {
    return 'usage'
  }
  const found = SETTINGS_SECTIONS.find((entry) => entry.key === value)
  return found ? found.key : 'general'
}
