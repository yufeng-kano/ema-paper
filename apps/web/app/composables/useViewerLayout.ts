/**
 * The result viewer's layout as the reader chose it: whether the notes and agent cards are
 * open, the width each was dragged to, and the body margins (docs/web.md § Result viewer,
 * *Column widths* and *Body margins*).
 *
 * Module scope, not component state: the cards, their widths and the margins are reading
 * preferences, so opening the next document keeps the choice just made instead of asking
 * for it again — and so does the next visit, from the browser. The viewer and Settings ›
 * General share this one state, so a viewer on screen follows a step picked in Settings at
 * once.
 */
import { clampWidth, storedWidth, type Column } from '~/lib/columns'

/** Where the viewer's layout is kept in the browser between visits. */
const LAYOUT_KEY = 'sightread.viewer.layout'

/** The room left at each side of the body text, as Settings offers it: the document column
 *  is 86%, 70% or 54% of its pane (the viewer's `--page-width`). */
export const PAPER_MARGINS = ['narrow', 'standard', 'wide'] as const
export type PaperMargin = (typeof PAPER_MARGINS)[number]

type Layout = { notes: boolean; agent: boolean; notesWidth: number; agentWidth: number; paperMargin: PaperMargin }

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
/** The widths the reader set — by a drag, a key or a double-click — which is what is stored:
 *  a width the window clamped is not a width the reader chose. */
const chosenWidth = reactive<Record<Column, number>>({
  notes: storedWidth('notes', layout.notesWidth),
  agent: storedWidth('agent', layout.agentWidth),
})
/** A stored value that is no step — an older layout, a hand edit — reads as Standard. */
const paperMargin = ref<PaperMargin>(PAPER_MARGINS.includes(layout.paperMargin as PaperMargin) ? (layout.paperMargin as PaperMargin) : 'standard')

function saveLayout() {
  const next: Layout = {
    notes: notesOpen.value,
    agent: agentOpen.value,
    notesWidth: chosenWidth.notes,
    agentWidth: chosenWidth.agent,
    paperMargin: paperMargin.value,
  }
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(next))
  } catch {
    // Storage full or blocked: the layout still holds for as long as the page is loaded.
  }
}

watch([notesOpen, agentOpen, paperMargin], saveLayout)

/** The width the reader chose for a card, held to the card's own bounds and kept. */
function chooseWidth(column: Column, width: number) {
  chosenWidth[column] = clampWidth(column, width)
  saveLayout()
}

export function useViewerLayout() {
  return { notesOpen, agentOpen, chosenWidth: readonly(chosenWidth), chooseWidth, paperMargin }
}
