/**
 * The status note and the polite live region beside it (docs/web.md § Editing, *Undo and the
 * status note*; § Files): the one line an action with nothing else to show confirms in. A note
 * carrying *Undo* or *View* waits for the reader — until their next action, or for
 * `NOTE_UNDO_MS`, held while the pointer rests on it; any other shows for `NOTE_MS`. Each
 * caller gets its own note, timer and live region.
 */

/** How long the status note shows, and how long one carrying *Undo* or *View* waits. */
const NOTE_MS = 1600
const NOTE_UNDO_MS = 8000

export function useStatusNote() {
  const note = ref<{ text: string; undo: boolean; view: boolean } | null>(null)
  let timer: ReturnType<typeof setTimeout> | undefined
  let held = false

  /** The one polite live region: what each note says, and anything else the caller announces. */
  const liveText = ref('')

  function announce(text: string) {
    liveText.value = ''
    void nextTick(() => (liveText.value = text))
  }

  function time() {
    clearTimeout(timer)
    const shown = note.value
    if (shown && !held) {
      timer = setTimeout(() => (note.value = null), shown.undo || shown.view ? NOTE_UNDO_MS : NOTE_MS)
    }
  }

  function show(text: string, undoable: boolean, view = false) {
    note.value = { text, undo: undoable, view }
    announce(text)
    time()
  }

  /** The pointer resting on the note holds it; leaving starts its time again. */
  function hold(holding: boolean) {
    held = holding
    if (holding) {
      clearTimeout(timer)
    } else {
      time()
    }
  }

  function hide() {
    clearTimeout(timer)
    note.value = null
  }

  /** A note carrying *Undo* or *View* stays until the reader's next action. */
  function hideSticky() {
    if (note.value?.undo || note.value?.view) {
      hide()
    }
  }

  onBeforeUnmount(() => clearTimeout(timer))

  return { note, show, hold, hide, hideSticky, liveText, announce }
}
