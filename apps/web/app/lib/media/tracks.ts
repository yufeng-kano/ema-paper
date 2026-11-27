/**
 * Whether an MP4 or a QuickTime file holds a video track, read from its box tree
 * (docs/media-viewer.md § The player, *A video with no picture*). Chrome and Firefox report a
 * video whose picture they cannot decode exactly as a file with sound alone, and neither has
 * `videoTracks` to ask, so the player reads the movie header's tracks and their handler itself,
 * over `Range` reads of the file's own address.
 *
 * The scan is pure: it is given the bytes read so far and answers either with the result or with
 * the next range to read. One read when the movie header is at the start of the file, two or
 * three when it is at the end; the caller stops after `TRACK_READS_MAX`.
 */

export type TrackScan =
  | { done: 'video' | 'no-video' | 'unknown' }
  /** Read these bytes and scan again. */
  | { read: { start: number; length: number } }

/** The first read, and each read for a top-level box's header. */
export const TRACK_HEAD_BYTES = 65_536
/** A movie header larger than this is not read. */
export const TRACK_MOOV_MAX = 16 * 1_048_576
/** Reads per file, after which the answer is `unknown`. */
export const TRACK_READS_MAX = 4

/** The boxes an ISO or QuickTime file can start with; anything else (a WebM's `1A 45 DF A3`
 *  included) is a file this scan does not read. */
const FIRST_BOXES = new Set(['ftyp', 'moov', 'mdat', 'free', 'skip', 'wide', 'pnot', 'uuid'])

const UNKNOWN: TrackScan = { done: 'unknown' }

interface BoxHeader {
  type: string
  /** The header's own length: 8, or 16 with a 64-bit size. */
  header: number
  /** The whole box's length; 0 means it runs to the end of what holds it. */
  size: number
}

/** The header at `pos`, null when fewer bytes than it needs are left before `end`, or
 *  `'bad'` for a size no box can have. */
function readHeader(view: DataView, pos: number, end: number): BoxHeader | null | 'bad' {
  if (end - pos < 8) {
    return null
  }
  const size = view.getUint32(pos)
  const type = String.fromCharCode(
    view.getUint8(pos + 4),
    view.getUint8(pos + 5),
    view.getUint8(pos + 6),
    view.getUint8(pos + 7),
  )
  if (size !== 1) {
    return size !== 0 && size < 8 ? 'bad' : { type, header: 8, size }
  }
  if (end - pos < 16) {
    return null
  }
  const high = view.getUint32(pos + 8)
  const low = view.getUint32(pos + 12)
  // Past 2^53 a size is no longer an exact number; no real file is that long.
  if (high >= 2 ** 21) {
    return 'bad'
  }
  const large = high * 2 ** 32 + low
  return large < 16 ? 'bad' : { type, header: 16, size: large }
}

interface ChildBox {
  type: string
  /** Where its body starts, after the header. */
  body: number
  end: number
}

/** The boxes inside `start`…`end`, or null when one overruns it. */
function children(view: DataView, start: number, end: number): ChildBox[] | null {
  const boxes: ChildBox[] = []
  let pos = start
  while (pos < end) {
    const header = readHeader(view, pos, end)
    if (header === null || header === 'bad') {
      return null
    }
    const size = header.size === 0 ? end - pos : header.size
    if (pos + size > end) {
      return null
    }
    boxes.push({ type: header.type, body: pos + header.header, end: pos + size })
    pos += size
  }
  return boxes
}

/** A whole `moov`: `video` as soon as a track's media handler is `vide`. Only `mdia`'s own
 *  `hdlr` counts; the one under `minf` names the data reference (`dhlr`, `alis`). */
function scanMovie(view: DataView, start: number, end: number): TrackScan {
  const movie = children(view, start, end)
  // A compressed QuickTime header is not read.
  if (!movie || movie.some((box) => box.type === 'cmov')) {
    return UNKNOWN
  }
  let tracks = 0
  for (const trak of movie.filter((box) => box.type === 'trak')) {
    tracks++
    const track = children(view, trak.body, trak.end)
    if (!track) {
      return UNKNOWN
    }
    const mdia = track.find((box) => box.type === 'mdia')
    if (!mdia) {
      continue
    }
    const media = children(view, mdia.body, mdia.end)
    if (!media) {
      return UNKNOWN
    }
    const hdlr = media.find((box) => box.type === 'hdlr')
    // After version and flags (4 bytes) and `pre_defined`, which QuickTime calls the component
    // type (4 bytes, `mhlr`), comes the handler type.
    if (hdlr && hdlr.end - hdlr.body >= 12) {
      const offset = hdlr.body + 8
      const handler = String.fromCharCode(
        view.getUint8(offset),
        view.getUint8(offset + 1),
        view.getUint8(offset + 2),
        view.getUint8(offset + 3),
      )
      if (handler === 'vide') {
        return { done: 'video' }
      }
    }
  }
  return tracks > 0 ? { done: 'no-video' } : UNKNOWN
}

/**
 * Scans `bytes`, read from offset `at` of a file `total` bytes long (null when the answer did
 * not say), over its top-level boxes until the `moov`. A `moov` wholly in `bytes` is answered;
 * one that starts here and runs past them is read whole next; a box that runs past them is
 * skipped by reading at its end.
 */
export function scanIsoTracks(bytes: Uint8Array, at: number, total: number | null): TrackScan {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  let pos = 0
  while (true) {
    const header = readHeader(view, pos, bytes.length)
    if (header === null) {
      // The end of the file with no `moov` (or too little of it left for a box), or a header
      // still to read.
      return total !== null && total - (at + pos) < 8 ? UNKNOWN : { read: { start: at + pos, length: TRACK_HEAD_BYTES } }
    }
    if (header === 'bad' || (at === 0 && pos === 0 && !FIRST_BOXES.has(header.type))) {
      return UNKNOWN
    }
    if (header.type === 'moov') {
      // A `moov` with size 0 runs to the end of the file, or of what was read when that is unknown.
      const size = header.size === 0 ? (total ?? at + bytes.length) - (at + pos) : header.size
      if (pos + size <= bytes.length) {
        return scanMovie(view, pos + header.header, pos + size)
      }
      if (size > TRACK_MOOV_MAX || (total !== null && at + pos + size > total)) {
        return UNKNOWN
      }
      return { read: { start: at + pos, length: size } }
    }
    // Any other box that runs to the end of the file leaves no room for a `moov`.
    if (header.size === 0) {
      return UNKNOWN
    }
    pos += header.size
  }
}
