import type { AgentSettings } from './api'

/**
 * A skill's or a subagent's name, and the cap on what tells the model when to use it — the
 * API's `NAME_PATTERN` and `DESCRIPTION_MAX_CHARS` (apps/api/src/sightread/agents/skills.py),
 * which the dialogs check before Save so the reader sees the rule rather than a 400.
 */
export const NAME_RULE = /^[a-z0-9][a-z0-9-]{0,63}$/
export const DESCRIPTION_MAX = 1024

/** A text's length as the server caps it: trimmed, in code points rather than UTF-16 units. */
export function charLength(text: string): number {
  return [...text.trim()].length
}

/** The instructions section's values, in the form its controls hold them: `''` for none. */
export interface InstructionsValues {
  instructions: string
  answer: string
  translate: string
}

/**
 * What the chat agent's instructions section's one Save writes, or null when Save has
 * nothing to send. The languages go with every save; the instructions only when their text
 * changed, and only within the cap. The cap applies to writes, not to a stored value, so
 * instructions stored longer than it never stand in the way of a language change.
 */
export function instructionsUpdate(
  draft: InstructionsValues,
  saved: InstructionsValues,
  maxChars: number | null,
): Partial<AgentSettings> | null {
  const text = draft.instructions.trim()
  const textChanged = text !== saved.instructions.trim()
  if (textChanged && maxChars !== null && charLength(text) > maxChars) {
    return null
  }
  if (!textChanged && draft.answer === saved.answer && draft.translate === saved.translate) {
    return null
  }
  return {
    ...(textChanged ? { instructions: text || null } : {}),
    answer_language: draft.answer || null,
    translate_language: draft.translate || null,
  }
}
