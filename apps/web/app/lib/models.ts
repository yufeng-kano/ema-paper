/**
 * The model pickers' shared reading of an agent's catalog (docs/web.md § Pages, § Files):
 * how a model is named, which stored choice the catalog does not list, and the parse
 * catalog the Retry and Re-convert dialogs offer. The chat catalog stays in the order the
 * server sends, whose first entry is the default (docs/billing.md § Models).
 */
import { listAgentModels, listAgents, type AgentEntry, type ConnectionModel } from './api'

export const PARSE_AGENT = 'parse'

/** A model as a picker lists it: its catalog name and id, or the id alone. */
export function modelLabel(model: ConnectionModel): string {
  return model.name ? `${model.name} · ${model.id}` : model.id
}

/** A current choice the catalog does not list (delisted, or not loaded) is still the choice,
 *  so the picker shows it rather than silently blanking it; null when there is none to add. */
export function injectedModel(current: string | null | undefined, models: ConnectionModel[] | null): string | null {
  return current && !(models ?? []).some((model) => model.id === current) ? current : null
}

/** The registry and the parse agent's catalog, sorted by id, read together. */
export async function loadParseModels(): Promise<{ agents: AgentEntry[]; models: ConnectionModel[] }> {
  const [{ agents }, catalog] = await Promise.all([listAgents(), listAgentModels(PARSE_AGENT)])
  return { agents, models: [...catalog.data].sort((left, right) => left.id.localeCompare(right.id)) }
}
