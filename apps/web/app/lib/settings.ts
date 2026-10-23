import type { IconName } from './icons'

/**
 * The sections of the settings dialog, in the order its section list shows them
 * (docs/web.md § Pages, *The settings dialog*). Shared by the dialog and by the layout that
 * reads `?settings=<section>` off the route, so the two can never disagree on what a section is.
 */
export const SETTINGS_SECTIONS = [
  { key: 'general', icon: 'settings' },
  { key: 'account', icon: 'user' },
  { key: 'billing', icon: 'credit-card' },
  { key: 'agent', icon: 'agent' },
  { key: 'keys', icon: 'keys' },
  { key: 'mcp', icon: 'connect' },
  { key: 'usage', icon: 'dashboard' },
] as const satisfies readonly { key: string; icon: IconName }[]

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]['key']

/** The query parameter that opens the dialog: `?settings=general`. */
export const SETTINGS_QUERY = 'settings'

/**
 * The section a route's `settings` query names: `null` when the dialog is closed, *general*
 * when it is open on a section that does not exist, as the account menu would open it.
 */
export function settingsSectionOf(value: unknown): SettingsSection | null {
  if (value === undefined) {
    return null
  }
  const found = SETTINGS_SECTIONS.find((entry) => entry.key === value)
  return found ? found.key : 'general'
}
