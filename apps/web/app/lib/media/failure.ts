/**
 * Why an image, a video or an audio file could not be shown (docs/media-viewer.md § The image
 * view, *Failure*; § The player, *Failure*). An `<img>` that fails says nothing of why, and a
 * media element's `MediaError` says little, so the viewer reads one byte of the same address
 * (`probeFileStatus`) and classes the failure by that answer, then by the media error's code and
 * whether the metadata had come.
 */
import type { LoadErrorKind } from '~/lib/pdf/errors'

/** The kinds a viewer words: a cancel shows nothing, so it is `null` here instead. */
export type MediaFailureKind = Exclude<LoadErrorKind, 'cancelled'>

/** The one-byte read's answer: its status, or what the fetch rejected with. */
export type ProbeOutcome = { status: number } | { error: unknown }

export interface MediaErrorCause {
  /** `MediaError.code` of a `<video>` or `<audio>`; null for an `<img>`, which has none. */
  code: number | null
  /** The element had reached `loadedmetadata` before the error. */
  afterMetadata: boolean
}

/** `MediaError.MEDIA_ERR_NETWORK` and `MEDIA_ERR_DECODE`, which the node test run has no
 *  `MediaError` to read from. */
const MEDIA_ERR_NETWORK = 2
const MEDIA_ERR_DECODE = 3

/**
 * The kind of a failure, or null when the read was aborted: the view went away, so nothing
 * shows. `offline` is `navigator.onLine === false`. The server's answer comes before the media
 * error's code, so a file that is gone or a session that ended reads as that whatever the
 * element said.
 */
export function mediaFailure(outcome: ProbeOutcome, offline: boolean, cause: MediaErrorCause): MediaFailureKind | null {
  if ('error' in outcome && outcome.error instanceof DOMException && outcome.error.name === 'AbortError') {
    return null
  }
  if (offline || 'error' in outcome) {
    return 'network'
  }
  const { status } = outcome
  if (status === 401) {
    return 'signed-out'
  }
  if (status === 404) {
    return 'gone'
  }
  if (status === 0 || status >= 500) {
    return 'network'
  }
  // The browser says the transfer broke; the read after it may simply have found the
  // connection back.
  if (cause.code === MEDIA_ERR_NETWORK) {
    return 'network'
  }
  // A file the browser had opened and then lost is not a format it cannot play.
  if (cause.afterMetadata && cause.code !== MEDIA_ERR_DECODE) {
    return 'network'
  }
  return 'corrupt'
}
