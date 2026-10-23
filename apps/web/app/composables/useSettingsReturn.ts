/**
 * Where the settings sheet goes back to when it is closed: the route the account menu
 * opened it from. Empty on a deep link, in which case the sheet closes to `/files`
 * (docs/web.md § Pages, `/settings/<section>`).
 */
export function useSettingsReturn() {
  return useState<string | null>('settings:returnTo', () => null)
}
