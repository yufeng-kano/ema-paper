/**
 * The Agent page's signal that what the chat agent receives may have changed (docs/web.md
 * § Pages): bumped after every save that feeds the system blocks — the agent's settings,
 * a memory row, a skill or a subagent — and watched by *What the agent receives*, which
 * reloads while it is open rather than showing the parts as they were when it opened.
 *
 * A counter, not the settings themselves: a memory row or a skill changes what is sent
 * without touching the agent's settings.
 */
export function useAgentContextVersion() {
  const version = useState<number>('agents:context-version', () => 0)

  return {
    version: readonly(version),
    /** Called once a save has been answered, so a reload reads what was stored. */
    changed: () => {
      version.value += 1
    },
  }
}
