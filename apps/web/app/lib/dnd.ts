/**
 * The three things HTML5 drag events do not tell you on their own (docs/web.md § Files).
 *
 * **What is being dragged.** `dataTransfer.getData` is empty during a drag — the payload
 * only becomes readable on drop, by design, so a page cannot read what you are dragging
 * over it. `types` is readable throughout, which is how a drag from the desktop is told
 * apart from a row of this page.
 *
 * **Who is handling it.** `dragover` fires on the element under the pointer and bubbles all
 * the way to the window, so several handlers see the same event and each would happily
 * decide the drop is theirs. A claim marks the event on the way up: the innermost handler
 * that wants it takes it, and everything above reads the mark instead of guessing.
 *
 * The claim is also what replaces `dragleave` for un-highlighting a drop target. `dragleave`
 * fires for every child element the pointer crosses and bubbles like everything else, so a
 * row built from a caret and a label reports "left" halfway across itself; browsers also
 * disagree about whether `relatedTarget` is populated on drag events. `dragover` has none of
 * that ambiguity — it fires continuously on exactly one innermost element — so the window
 * clears whatever the last `dragover` did not claim.
 *
 * The claims live in a `WeakMap` keyed by the event object: an event is over in a frame, and
 * nothing here should keep one alive.
 */

/** The type a drag of the result viewer's blocks carries, and that the notes read on drop
 *  (docs/web.md § Notes, *A block dragged into the notes lands as blocks*). */
export const BLOCK_DRAG_TYPE = 'application/x-sightread-blocks'

/** The type a block dragged from an agent answer carries besides `BLOCK_DRAG_TYPE`: the one
 *  the document lands as a copy, so a drag of its own a reload cut short is never taken for
 *  one (docs/web.md § The agent panel, *An answer is blocks*). */
export const ANSWER_DRAG_TYPE = 'application/x-sightread-answer'

/** The type a drag of the library's own rows carries: what tells it from text or files
 *  dragged in from elsewhere, which carry the same `text/plain` (docs/web.md § Files,
 *  *Moving*). */
export const ROWS_DRAG_TYPE = 'application/x-sightread-rows'

/** What a handler claimed a drag event for. */
export type DragClaim =
  /** A folder is the destination under the pointer, and it is drawn as the target. */
  | 'target'
  /** The open folder's list is under the pointer — where dropped files land, and rows a
   *  drag brought from another folder. */
  | 'list'
  /** A block of the result viewer is under a block being dragged — where it would land. */
  | 'block'
  /** The open document or its notes are under image files being dragged — where they would
   *  be inserted (docs/web.md § Editing, *Images*). */
  | 'image'

const claims = new WeakMap<DragEvent, DragClaim>()

/** Take this event, unless something further in has already taken it. */
export function claimDrag(event: DragEvent, claim: DragClaim): void {
  if (!claims.has(event)) {
    claims.set(event, claim)
  }
}

export function dragClaim(event: DragEvent): DragClaim | undefined {
  return claims.get(event)
}

/** A drag carrying files from outside the browser, rather than a row of this page. */
export function carriesFiles(transfer: DataTransfer | null): boolean {
  return [...(transfer?.types ?? [])].includes('Files')
}

/**
 * The cursor the drop would produce. Set on every `dragover` or the browser picks its own,
 * which is how a valid drop ends up showing the "copy" cursor for a move — or the barred
 * circle for a drop that will in fact work.
 */
export function setDropEffect(event: DragEvent, effect: 'move' | 'copy' | 'none'): void {
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = effect
  }
}

/** The files a drop carried, if any. */
export function droppedFiles(event: DragEvent): File[] {
  return [...(event.dataTransfer?.files ?? [])]
}
