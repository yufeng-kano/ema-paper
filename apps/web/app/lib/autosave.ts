/**
 * The viewer's autosave for one file of a library document — its markdown or its notes
 * (docs/web.md § Editing): a debounce after the last change, one save at a time, each sent
 * against the revision the one before it produced, a 409 that reloads, and a failure kept as
 * unsaved work. Pure over what the caller injects: the state, error and revision it keeps in
 * boxes (a Vue ref is one), the text to send, the PUT and what a conflict or a failure does.
 */

/** One file's save state. The header shows one word for the two files: a failed save first,
 *  otherwise the busier of the rest. */
export type SaveState = 'saved' | 'editing' | 'saving' | 'failed'

/** A value the caller reads back: a Vue ref, or a plain object in a test. */
export interface Box<T> {
  value: T
}

export interface AutosaveOptions {
  state: Box<SaveState>
  error: Box<string | null>
  /** The revision the next save is made against, moved on by each save that lands. */
  revision: Box<string>
  /** The library document, or null where there is none to save to. */
  documentId(): number | null
  /** The text a save sends, read again when it lands: a change since is sent after it. */
  snapshot(): string
  put(id: number, text: string, baseRevision: string): Promise<{ revision: string }>
  /** Whether a failed save met the 409 that means the file moved under the reader. */
  isConflict(error: unknown): boolean
  /** After a conflict, the state already `saved`: what is on disk wins, and the reader is told. */
  onConflict(): Promise<void>
  /** After any other failure, the state already `failed`: why, for the banner. */
  describe(error: unknown): Promise<string>
  /** After the error is set: the failed text stays the reader's, and the caller may retry. */
  onFailure(): void
  /** A reload deferred behind the save in flight, made once it settles. */
  reload(): void
  delay: number
}

export class Autosave {
  /** A reload that arrived while a save was in flight, to be made again after it. */
  reloadAfterSave = false
  private timer: ReturnType<typeof setTimeout> | null = null
  /** The save in flight, if any: one at a time. */
  private inFlight: Promise<void> | null = null
  /** A save asked for while one was in flight, sent once it settles. */
  private again = false
  /** How many saves met a 409: `settle` reports whether any did while it waited. */
  private conflicts = 0
  private readonly options: AutosaveOptions

  constructor(options: AutosaveOptions) {
    this.options = options
  }

  /** A change: saved once the debounce passes with no other. */
  schedule() {
    this.options.state.value = 'editing'
    if (this.timer !== null) {
      clearTimeout(this.timer)
    }
    this.timer = setTimeout(() => void this.save(), this.options.delay)
  }

  /**
   * One save at a time. A change made while one is in flight is sent after it, from the
   * revision that save produced — never from the one it was made against, which is stale
   * the moment the first save lands. The promise settles with the save in flight.
   */
  save(): Promise<void> {
    const id = this.options.documentId()
    if (id === null) {
      return Promise.resolve()
    }
    if (this.inFlight) {
      this.again = true
      return this.inFlight
    }
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
    this.inFlight = this.write(id)
    return this.inFlight
  }

  /** Waits for the saves to settle: true once what the reader sees is saved and no save met
   *  a 409 meanwhile. */
  async settle(): Promise<boolean> {
    const { state } = this.options
    const conflicts = this.conflicts
    for (let round = 0; round < 5; round += 1) {
      if (state.value === 'saved' && !this.inFlight) {
        return this.conflicts === conflicts
      }
      if (state.value === 'failed') {
        return false
      }
      await this.save()
    }
    return false
  }

  /** Closing: a change still in the debounce is sent now, and a failed save gets one more
   *  try too, since it left its text unsaved with no timer. */
  flush() {
    if (this.timer !== null || this.options.state.value === 'failed') {
      if (this.timer !== null) {
        clearTimeout(this.timer)
      }
      void this.save()
    }
  }

  private async write(id: number) {
    const { state, error, revision, snapshot } = this.options
    state.value = 'saving'
    error.value = null
    const sent = snapshot()
    try {
      const saved = await this.options.put(id, sent, revision.value)
      revision.value = saved.revision
      if (snapshot() === sent) {
        state.value = 'saved'
      } else if (this.again) {
        state.value = 'editing'
      } else {
        this.schedule()
      }
    } catch (failure) {
      if (this.options.isConflict(failure)) {
        this.conflicts += 1
        state.value = 'saved'
        await this.options.onConflict()
      } else {
        state.value = 'failed'
        error.value = await this.options.describe(failure)
        this.options.onFailure()
      }
    } finally {
      this.inFlight = null
      if (this.reloadAfterSave) {
        this.reloadAfterSave = false
        this.options.reload()
      }
      if (this.again) {
        this.again = false
        if (state.value === 'editing') {
          void this.save()
        }
      }
    }
  }
}
