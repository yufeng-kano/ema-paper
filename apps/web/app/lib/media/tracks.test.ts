import { describe, expect, it } from 'vitest'
import { scanIsoTracks, TRACK_HEAD_BYTES, TRACK_MOOV_MAX, TRACK_READS_MAX } from './tracks'

// Files are built here, box by box: a box is its 32-bit size, its type, then its body.

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0))
  let pos = 0
  for (const part of parts) {
    out.set(part, pos)
    pos += part.length
  }
  return out
}

function ascii(text: string): Uint8Array {
  return Uint8Array.from(text, (char) => char.charCodeAt(0))
}

function box(type: string, ...body: Uint8Array[]): Uint8Array {
  const content = concat(...body)
  const header = new Uint8Array(8)
  new DataView(header.buffer).setUint32(0, 8 + content.length)
  header.set(ascii(type), 4)
  return concat(header, content)
}

/** A box header declaring `size`, with no body after it. */
function header(type: string, size: number): Uint8Array {
  const out = new Uint8Array(8)
  new DataView(out.buffer).setUint32(0, size)
  out.set(ascii(type), 4)
  return out
}

/** A 64-bit size header: size 1, the type, then the size as two 32-bit halves. */
function largeHeader(type: string, size: number): Uint8Array {
  const out = new Uint8Array(16)
  const view = new DataView(out.buffer)
  view.setUint32(0, 1)
  out.set(ascii(type), 4)
  view.setUint32(8, Math.floor(size / 2 ** 32))
  view.setUint32(12, size % 2 ** 32)
  return out
}

/** A handler box: version and flags, `pre_defined` (QuickTime's component type), the handler
 *  type, 12 reserved bytes and an empty name. */
function hdlr(handler: string, component = '\0\0\0\0'): Uint8Array {
  return box('hdlr', new Uint8Array(4), ascii(component), ascii(handler), new Uint8Array(13))
}

function trak(handler: string, ...extra: Uint8Array[]): Uint8Array {
  return box('trak', box('tkhd', new Uint8Array(84)), box('mdia', box('mdhd', new Uint8Array(24)), hdlr(handler), ...extra))
}

const ftyp = box('ftyp', ascii('isom'), new Uint8Array(4), ascii('isomiso2mp41'))
const mvhd = box('mvhd', new Uint8Array(100))
const filler = (type: string, length: number) => box(type, new Uint8Array(length - 8))

/** The player's loop (docs/media-viewer.md § The player): read, scan, follow a `read` until a
 *  result or the last allowed read. A read starting past the end is the server's 416. */
function run(file: Uint8Array, totalKnown = true): { result: string; reads: number } {
  let start = 0
  let length = TRACK_HEAD_BYTES
  for (let reads = 1; reads <= TRACK_READS_MAX; reads++) {
    if (start >= file.length) {
      return { result: 'unknown', reads }
    }
    const scan = scanIsoTracks(file.subarray(start, start + length), start, totalKnown ? file.length : null)
    if ('done' in scan) {
      return { result: scan.done, reads }
    }
    ;({ start, length } = scan.read)
  }
  return { result: 'unknown', reads: TRACK_READS_MAX }
}

describe('scanIsoTracks', () => {
  it('finds the video track beside a sound track, with the movie header at the start', () => {
    const file = concat(ftyp, box('moov', mvhd, trak('soun'), trak('vide')), filler('mdat', 200_000))
    expect(scanIsoTracks(file.subarray(0, TRACK_HEAD_BYTES), 0, file.length)).toEqual({ done: 'video' })
  })

  it('finds no video in a file with one sound track', () => {
    const file = concat(ftyp, box('moov', mvhd, trak('soun')), filler('mdat', 1000))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'no-video' })
  })

  it('reads a QuickTime handler past its component type', () => {
    const quickTime = box('trak', box('mdia', hdlr('vide', 'mhlr')))
    const file = concat(box('wide'), filler('mdat', 1000), box('moov', mvhd, quickTime))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'video' })
  })

  it('skips a large mdat to read at its end, then finds the movie header there', () => {
    const mdat = filler('mdat', 300_000)
    const file = concat(ftyp, mdat, box('moov', mvhd, trak('vide')))
    const end = ftyp.length + mdat.length
    const first = scanIsoTracks(file.subarray(0, TRACK_HEAD_BYTES), 0, file.length)
    expect(first).toEqual({ read: { start: end, length: TRACK_HEAD_BYTES } })
    expect(scanIsoTracks(file.subarray(end, end + TRACK_HEAD_BYTES), end, file.length)).toEqual({ done: 'video' })
    expect(run(file)).toEqual({ result: 'video', reads: 2 })
  })

  it('reads a movie header that starts in the buffer and runs past it, whole', () => {
    const moov = box('moov', mvhd, filler('free', 100_000), trak('soun'))
    const head = concat(ftyp, filler('free', 60_000))
    const file = concat(head, moov)
    expect(scanIsoTracks(file.subarray(0, TRACK_HEAD_BYTES), 0, file.length)).toEqual({
      read: { start: head.length, length: moov.length },
    })
    expect(run(file)).toEqual({ result: 'no-video', reads: 2 })
  })

  it('tells sound alone with the movie header at the end within the reads allowed', () => {
    const file = concat(ftyp, filler('free', 8), filler('mdat', 2_000_000), box('moov', mvhd, trak('soun')))
    const { result, reads } = run(file)
    expect(result).toBe('no-video')
    expect(reads).toBeLessThanOrEqual(3)
  })

  it('does not read a movie header larger than the limit', () => {
    const size = 17 * 1_048_576
    expect(size).toBeGreaterThan(TRACK_MOOV_MAX)
    const bytes = concat(ftyp, header('moov', size))
    expect(scanIsoTracks(bytes, 0, ftyp.length + size)).toEqual({ done: 'unknown' })
  })

  it('follows a 64-bit box size', () => {
    const size = 2 ** 32 + 100
    const bytes = concat(ftyp, largeHeader('mdat', size), new Uint8Array(64))
    expect(scanIsoTracks(bytes, 0, 2 ** 34)).toEqual({ read: { start: ftyp.length + size, length: TRACK_HEAD_BYTES } })
  })

  it('refuses a 64-bit size past what a number holds exactly', () => {
    const bytes = concat(ftyp, largeHeader('mdat', 2 ** 60))
    expect(scanIsoTracks(bytes, 0, null)).toEqual({ done: 'unknown' })
  })

  it('cannot tell a movie header with no track', () => {
    const file = concat(ftyp, box('moov', mvhd))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'unknown' })
  })

  it('cannot tell a track whose child overruns it', () => {
    const broken = box('trak', header('mdia', 500), new Uint8Array(20))
    const file = concat(ftyp, box('moov', mvhd, broken, trak('vide')))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'unknown' })
  })

  it('does not read a compressed QuickTime header', () => {
    const file = concat(box('wide'), box('moov', box('cmov', new Uint8Array(40))))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'unknown' })
  })

  it('reads only ISO and QuickTime files: a WebM ends at its first bytes', () => {
    const webm = concat(Uint8Array.of(0x1a, 0x45, 0xdf, 0xa3), new Uint8Array(200))
    expect(scanIsoTracks(webm, 0, webm.length)).toEqual({ done: 'unknown' })
  })

  it('ends at the end of a file with no movie header, even with a few bytes left over', () => {
    const file = concat(ftyp, filler('mdat', 1000), new Uint8Array(4))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'unknown' })
    expect(run(file)).toEqual({ result: 'unknown', reads: 1 })
  })

  it('cannot tell a truncated file whose movie header runs past its end', () => {
    const whole = concat(ftyp, filler('free', 70_000), box('moov', mvhd, filler('free', 50_000), trak('vide')))
    const cut = whole.subarray(0, whole.length - 30_000)
    expect(run(cut)).toEqual({ result: 'unknown', reads: 2 })
  })

  it('counts only the media handler, not the data handler under minf', () => {
    const minf = box('minf', box('smhd', new Uint8Array(8)), hdlr('alis', 'dhlr'))
    const file = concat(ftyp, box('moov', mvhd, trak('soun', minf)))
    expect(scanIsoTracks(file, 0, file.length)).toEqual({ done: 'no-video' })
  })

  it('gives up after the reads allowed when no length is known', () => {
    const boxes = Array.from({ length: 6 }, () => filler('free', 70_000))
    const file = concat(ftyp, ...boxes, box('moov', mvhd, trak('vide')))
    expect(run(file, false)).toEqual({ result: 'unknown', reads: TRACK_READS_MAX })
  })
})
