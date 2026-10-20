/**
 * The rows of **What the agent receives** (docs/web.md § Pages, docs/api.md `GET
 * /api/agents/chat/context`), apart from their labels, which are the catalog's: the system
 * blocks in the order a turn sends them, then the context block every message carries,
 * then the document's facts — each with its size in characters.
 */
import type { AgentContext, AgentContextFacts, AgentContextPart } from './api'

/** The facts the prompt is rendered with, in the order the API lists them. */
export const FACT_NAMES: readonly (keyof AgentContextFacts)[] = [
  'document_path',
  'page_count',
  'bbox_format',
  'parse_model',
  'network',
]

export interface ReceivesFact {
  name: keyof AgentContextFacts
  value: string
}

interface RowBase {
  /** Stable across reloads: a row the reader opened stays open when a part is added or
   *  removed before it. */
  key: string
  /** Its size in characters (code points, as the server counts them). */
  chars: number
}

export type ReceivesRow =
  | (RowBase & { kind: 'part'; part: AgentContextPart; text: string })
  | (RowBase & { kind: 'turn'; text: string })
  | (RowBase & { kind: 'facts'; facts: ReceivesFact[] })

/** A fact's value, or — on the Agent page, where no document is open — its token. */
export function factValue(facts: AgentContextFacts | null, name: keyof AgentContextFacts): string {
  return facts?.[name] ?? `{${name}}`
}

/** A part that is not sent is not in the answer, so it is not listed. */
export function receivesRows(context: AgentContext): ReceivesRow[] {
  const seen = new Map<string, number>()
  const parts = context.parts.map((part): ReceivesRow => {
    const nth = seen.get(part.kind) ?? 0
    seen.set(part.kind, nth + 1)
    return { key: `${part.kind}-${nth}`, kind: 'part', part, text: part.text, chars: part.chars }
  })
  const facts = FACT_NAMES.map((name) => ({ name, value: factValue(context.facts, name) }))
  return [
    ...parts,
    { key: 'turn', kind: 'turn', text: context.turn.text, chars: context.turn.chars },
    // The facts are substituted into the prompt rather than sent as a block of their own;
    // their size is that of the values the row lists.
    { key: 'facts', kind: 'facts', facts, chars: facts.reduce((sum, fact) => sum + [...fact.value].length, 0) },
  ]
}

/**
 * Whether an Escape pressed anywhere on the page closes the drawer, which is non-modal:
 * not one a control already handled (an editor leaving its text, a memory edit cancelled,
 * an input method's composition), and not while a dialog or a menu is open, whose Escape
 * it is.
 */
export function escapeClosesDrawer(
  event: Pick<KeyboardEvent, 'key' | 'defaultPrevented' | 'isComposing'>,
  overlayOpen: boolean,
): boolean {
  return event.key === 'Escape' && !event.defaultPrevented && !event.isComposing && !overlayOpen
}
