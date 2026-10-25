/**
 * The result viewer's layout as the reader chose it: whether the notes and agent cards are
 * open, and the width each was given (docs/web.md § Result viewer, *Column widths*).
 *
 * Module scope, not component state: the cards and their widths are reading preferences, so
 * opening the next document keeps the choice just made instead of asking for it again — and
 * so does the next visit, from the browser. The viewer and Settings › General share this one
 * state, so a viewer on screen follows a width picked in Settings at once, and neither ever
 * writes back a width the other replaced.
 */
import { clampWidth, storedWidth, type Column } from '~/lib/columns'

/** Where the viewer's layout is kept in the browser between visits. */
const LAYOUT_KEY = 'sightread.viewer.layout'

type Layout = { notes: boolean; agent: boolean; notesWidth: number; agentWidth: number }

function storedLayout(): Partial<Layout> {
  if (!import.meta.client) {
    return {}
  }
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? '{}')
    return stored && typeof stored === 'object' ? stored : {}
  } catch {
    return {}
  }
}

const layout = storedLayout()
const notesOpen = ref(layout.notes === true)
const agentOpen = ref(layout.agent === true)
/** The widths the reader set — by a drag, a key, a double-click or a step in Settings — which
 *  is what is stored: a width the window clamped is not a width the reader chose. */
const chosenWidth = reactive<Record<Column, number>>({
  notes: storedWidth('notes', layout.notesWidth),
  agent: storedWidth('agent', layout.agentWidth),
})

function saveLayout() {
  const next: Layout = {
    notes: notesOpen.value,
    agent: agentOpen.value,
    notesWidth: chosenWidth.notes,
    agentWidth: chosenWidth.agent,
  }
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(next))
  } catch {
    // Storage full or blocked: the layout still holds for as long as the page is loaded.
  }
}

watch([notesOpen, agentOpen], saveLayout)

/** The width the reader chose for a card, held to the card's own bounds and kept. */
function chooseWidth(column: Column, width: number) {
  chosenWidth[column] = clampWidth(column, width)
  saveLayout()
}

export function useViewerLayout() {
  return { notesOpen, agentOpen, chosenWidth: readonly(chosenWidth), chooseWidth }
}
