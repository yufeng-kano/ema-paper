/**
 * The Agent page's memory list, as display transforms over `GET /api/memories`
 * (docs/web.md § Pages, docs/chat.md § Memory): the order rows are listed in, the kind
 * filter and text search the reader narrows them with, and the counts the filter chips
 * carry. Framework-free, so it is unit-tested in the node suite (docs/testing.md).
 */
import { MEMORY_KINDS, type Memory, type MemoryKind } from './api'

/** A filter chip: one kind, or every memory. */
export type MemoryFilter = MemoryKind | 'all'

export const MEMORY_FILTERS: readonly MemoryFilter[] = ['all', ...MEMORY_KINDS]

function newestFirst(left: Memory, right: Memory): number {
  const byTime = Date.parse(right.updated_at) - Date.parse(left.updated_at)
  return byTime || right.id - left.id
}

/**
 * Sorted by kind — terms, preferences, about the reader, documents — and within a kind
 * pinned first, then the newest. "Newest" is the last change (`updated_at`), the order the
 * route lists them in, so a memory the reader just corrected moves to the top of its kind.
 */
export function sortMemories(memories: readonly Memory[]): Memory[] {
  return [...memories].sort(
    (left, right) =>
      MEMORY_KINDS.indexOf(left.kind) - MEMORY_KINDS.indexOf(right.kind) ||
      Number(right.pinned) - Number(left.pinned) ||
      newestFirst(left, right),
  )
}

/** The search field's rule: the text contains the query, case ignored; an empty or
 *  blank query matches everything. */
export function memoryMatches(memory: Memory, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase()
  return !needle || memory.text.toLocaleLowerCase().includes(needle)
}

/** The rows the list shows under a chip and a search, in the order they were given. */
export function filterMemories(memories: readonly Memory[], filter: MemoryFilter, query: string): Memory[] {
  return memories.filter((memory) => (filter === 'all' || memory.kind === filter) && memoryMatches(memory, query))
}

/** Each chip's count: how many memories it holds, whatever the search says — a count that
 *  moved with every keystroke would read as memories coming and going. */
export function countMemories(memories: readonly Memory[]): Record<MemoryFilter, number> {
  const counts: Record<MemoryFilter, number> = { all: memories.length, term: 0, preference: 0, about: 0, document: 0 }
  for (const memory of memories) {
    counts[memory.kind] += 1
  }
  return counts
}

/** A memory named inside a sentence — a button's label, a confirmation — cut to its first
 *  `max` characters (code points, so an emoji is never halved). */
export function memoryExcerpt(text: string, max = 60): string {
  const chars = [...text.trim()]
  return chars.length > max ? `${chars.slice(0, max).join('').trimEnd()}…` : chars.join('')
}

/**
 * A row's date in the reader's locale: month and day, the year only when it is not this
 * year's. `null` for today, which the page words itself ("Today") — `Intl` has no word for
 * a day that is not relative — and an empty string for a stamp that does not parse.
 */
export function formatMemoryDate(iso: string, locale: string, now: Date = new Date()): string | null {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) {
    return ''
  }
  if (at.getFullYear() === now.getFullYear() && at.getMonth() === now.getMonth() && at.getDate() === now.getDate()) {
    return null
  }
  return new Intl.DateTimeFormat(locale, {
    year: at.getFullYear() === now.getFullYear() ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(at)
}
