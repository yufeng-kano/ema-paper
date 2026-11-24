/**
 * The Agent settings section's signal (`/settings/agent`) that what the chat agent receives
 * may have changed (docs/web.md § Pages): bumped after every save that feeds the system
 * blocks — the agent's settings, a memory row, a skill or a subagent — and watched by
 * *What the agent receives*, which reloads while it is open rather than showing the parts
 * as they were when it opened, and by the chat panel (ChatPanel, docs/web.md § The agent
 * panel), which reads its memories and the chat agent's settings again, the settings dialog
 * keeping it mounted.
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
