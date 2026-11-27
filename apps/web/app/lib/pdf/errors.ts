/**
 * Why a PDF, or one of its pages, could not be shown (docs/pdf-viewer.md § Stage 1: the Canvas
 * 2D viewer, *Errors*), read off whatever pdf.js rejected with. The viewer says each kind in the
 * reader's language and offers its way out; no error code or message of pdf.js's ever reaches
 * the screen.
 *
 * pdf.js's exceptions cross the worker as plain objects, so they are told apart by `name`,
 * `status` and `missing` rather than by class (`wrapReason`, build/pdf.mjs).
 */

/** `corrupt`: unreadable, offer the download. `network`: the server or the connection, offer
 *  a retry. `signed-out`: the session ended (401). `gone`: the file is no longer there (404).
 *  `cancelled`: the viewer stopped the work itself, so nothing shows. */
export type LoadErrorKind = 'corrupt' | 'network' | 'signed-out' | 'gone' | 'cancelled'

/** A page that failed to draw: the document-level kinds that say the cause is elsewhere, or
 *  `page` for this page alone. */
export type PageErrorKind = Exclude<LoadErrorKind, 'corrupt'> | 'page'

/** How each engine words a fetch that never reached the server: Chrome, Safari, Firefox. */
const FETCH_FAILED = /failed to fetch|load failed|networkerror/i

function field(error: unknown, name: string): unknown {
  return typeof error === 'object' && error !== null ? (error as Record<string, unknown>)[name] : undefined
}

/**
 * The kind of a failure to open a document. `offline` is `navigator.onLine === false`, which
 * makes any failure but a cancel or a server's answer a network one. Cancels include the plain
 * `Loading aborted` and `Worker was destroyed` errors a destroyed loading task rejects with.
 * A server answer is a network failure when it is 5xx (or 0, a request that never got an
 * answer), since a retry may pass; any other answer the docs do not name is unreadable.
 */
export function classifyLoadError(error: unknown, offline = false): LoadErrorKind {
  const name = field(error, 'name')
  const message = String(field(error, 'message') ?? '')
  if (
    name === 'AbortException' ||
    name === 'RenderingCancelledException' ||
    message === 'Loading aborted' ||
    message.startsWith('Worker was destroyed')
  ) {
    return 'cancelled'
  }
  const status = field(error, 'status')
  if (name === 'ResponseException' || typeof status === 'number') {
    if (status === 401) {
      return 'signed-out'
    }
    if (status === 404 || field(error, 'missing') === true) {
      return 'gone'
    }
    return status === 0 || (typeof status === 'number' && status >= 500) ? 'network' : 'corrupt'
  }
  if (offline || FETCH_FAILED.test(message) || FETCH_FAILED.test(String(field(error, 'details') ?? ''))) {
    return 'network'
  }
  return 'corrupt'
}

/** The kind of a `getPage` or `render` failure: the same causes as a document's, and `page`
 *  where a document would be `corrupt`. */
export function classifyPageError(error: unknown, offline = false): PageErrorKind {
  const kind = classifyLoadError(error, offline)
  return kind === 'corrupt' ? 'page' : kind
}
