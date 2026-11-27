import { describe, expect, it } from 'vitest'
import { mediaFailure, type MediaErrorCause } from './failure'

/** An `<img>`'s error: no code, never after metadata. */
const image: MediaErrorCause = { code: null, afterMetadata: false }
const media = (code: number | null, afterMetadata = false): MediaErrorCause => ({ code, afterMetadata })

describe('a failure read from the one-byte read', () => {
  it('shows nothing when the read was aborted, even offline', () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    expect(mediaFailure({ error: abort }, false, image)).toBeNull()
    expect(mediaFailure({ error: abort }, true, media(2))).toBeNull()
  })

  it('takes anything while offline as a network failure, a server’s answer included', () => {
    expect(mediaFailure({ status: 404 }, true, image)).toBe('network')
    expect(mediaFailure({ status: 206 }, true, image)).toBe('network')
  })

  it('takes a read that never reached the server as a network failure', () => {
    expect(mediaFailure({ error: new TypeError('Failed to fetch') }, false, image)).toBe('network')
    expect(mediaFailure({ error: new DOMException('Timed out', 'TimeoutError') }, false, image)).toBe('network')
  })

  it('reads the server’s answer: signed out, gone, a server failure', () => {
    expect(mediaFailure({ status: 401 }, false, image)).toBe('signed-out')
    expect(mediaFailure({ status: 404 }, false, image)).toBe('gone')
    expect(mediaFailure({ status: 0 }, false, image)).toBe('network')
    expect(mediaFailure({ status: 500 }, false, image)).toBe('network')
    expect(mediaFailure({ status: 503 }, false, image)).toBe('network')
  })

  it('takes a file the server serves and the browser cannot draw as not shown', () => {
    expect(mediaFailure({ status: 206 }, false, image)).toBe('corrupt')
    expect(mediaFailure({ status: 200 }, false, image)).toBe('corrupt')
    // An empty file answers a one-byte range with 416.
    expect(mediaFailure({ status: 416 }, false, image)).toBe('corrupt')
    expect(mediaFailure({ status: 403 }, false, image)).toBe('corrupt')
  })
})

describe('a media element’s failure', () => {
  it('takes a broken transfer as a network failure whatever the read answered', () => {
    expect(mediaFailure({ status: 206 }, false, media(2))).toBe('network')
    expect(mediaFailure({ status: 206 }, false, media(2, true))).toBe('network')
  })

  it('puts the server’s answer before the element’s code', () => {
    expect(mediaFailure({ status: 404 }, false, media(2))).toBe('gone')
    expect(mediaFailure({ status: 401 }, false, media(2))).toBe('signed-out')
  })

  it('takes a format error before the metadata as a file this browser cannot play', () => {
    expect(mediaFailure({ status: 206 }, false, media(4))).toBe('corrupt')
    expect(mediaFailure({ status: 206 }, false, media(3))).toBe('corrupt')
  })

  it('takes an error after the metadata as a lost connection, unless it is a decode error', () => {
    expect(mediaFailure({ status: 206 }, false, media(4, true))).toBe('network')
    expect(mediaFailure({ status: 206 }, false, media(null, true))).toBe('network')
    expect(mediaFailure({ status: 206 }, false, media(3, true))).toBe('corrupt')
  })
})
