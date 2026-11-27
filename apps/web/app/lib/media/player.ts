/**
 * The player's pure geometry and timing (docs/media-viewer.md § The player): the box a video
 * takes in the pane, a seek kept inside the video, and how long a stopped read waits before the
 * no-preload fallback shows the controls.
 */
import type { Size } from '~/lib/pdf/layout'

/** `loading` holds through the no-preload wait and the track check; `ready` is a player shown. */
export type MediaStatus = 'loading' | 'ready' | 'failed'

/** What the file viewer reads for More's facts. */
export interface MediaPlayerState {
  status: MediaStatus
  /** `videoWidth` × `videoHeight` once known and non-zero; null for audio and for no picture. */
  dimensions: Size | null
  /** Seconds, finite only; null while unknown, and for a stream the browser calls infinite. */
  duration: number | null
}

/** The box of a video whose size is not known, shown only once the no-preload wait runs out
 *  (docs/media-viewer.md § A device that does not preload). */
export const FALLBACK_ASPECT: Size = { width: 16, height: 9 }

/** How long a `suspend` with nothing loaded waits for any read before the fallback shows. */
export const NO_PRELOAD_MS = 3000

/**
 * The largest box of the video's aspect ratio inside `pane` (its client box) less `gutter` on
 * each side, in whole pixels, or null while a dimension is 0. A small video grows to it. Floored
 * with `boxPx`'s allowance (lib/pdf/layout.ts), so the limiting side meets the gutter exactly
 * and not a pixel short, and the box is never outside the pane.
 */
export function videoBox(video: Size, pane: Size, gutter: number): Size | null {
  if (video.width === 0 || video.height === 0) {
    return null
  }
  const availWidth = Math.max(0, pane.width - 2 * gutter)
  const availHeight = Math.max(0, pane.height - 2 * gutter)
  const k = Math.min(availWidth / video.width, availHeight / video.height)
  return { width: Math.floor(video.width * k + 1e-6), height: Math.floor(video.height * k + 1e-6) }
}

/** `current` moved by `delta` seconds, kept from 0 to `duration` when that is known. A
 *  `currentTime` the element cannot give yet counts as 0. */
export function seekTime(current: number, delta: number, duration: number): number {
  const time = Math.max(0, (Number.isFinite(current) ? current : 0) + delta)
  return Number.isFinite(duration) ? Math.min(duration, time) : time
}
