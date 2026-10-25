/**
 * The agent panel's list of conversations (docs/web.md § The agent panel, *Conversations*):
 * its order, and what a rename sends.
 */
import type { Conversation } from './api'

/** The thread continued last first, as the server lists them; a local insert or a read
 *  merged in keeps the same order. */
export function byRecency(rows: readonly Conversation[]): Conversation[] {
  return [...rows].sort((left, right) => right.updated_at.localeCompare(left.updated_at))
}

/** The invisible and control characters the server's cleaner removes from a title
 *  (`clean_text` in `agents/memory.py`), tab, newline and the other breaks aside. */
// eslint-disable-next-line no-control-regex
const INVISIBLE = /[\u200b\u2060\ufeff\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069\x00-\x08\x0e-\x1b\x7f]/g

/**
 * The title a rename field's text makes — one line, its whitespace runs one space, cut at the
 * server's cap — or null when the rename changes nothing: a field left blank keeps the old
 * title, as one left as it was does, and so does one holding only what the server takes out
 * (`INVISIBLE`), which it would refuse as empty. The server cleans it again and its answer is
 * the title shown; this is what the row shows until then.
 */
export function renamedTitle(text: string, before: string | null, max?: number): string | null {
  const line = text.replace(INVISIBLE, '').replace(/\s+/g, ' ').trim()
  const title = (max === undefined ? line : line.slice(0, max)).trim()
  return title && title !== (before ?? '') ? title : null
}
