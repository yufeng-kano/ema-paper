import { describe, expect, it } from 'vitest'
import { classifyLoadError, classifyPageError } from './errors'

/** pdf.js's exceptions as they arrive from the worker: plain objects with a name. */
const response = (status: number) => ({ name: 'ResponseException', message: 'Unexpected server response', status, missing: status === 404 })
const unknown = (message: string, details: string) => ({ name: 'UnknownErrorException', message, details })

describe('a document that cannot be opened', () => {
  it('reads the server’s answer: signed out, gone, a server failure', () => {
    expect(classifyLoadError(response(401))).toBe('signed-out')
    expect(classifyLoadError(response(404))).toBe('gone')
    expect(classifyLoadError({ name: 'ResponseException', status: 0, missing: true })).toBe('gone')
    expect(classifyLoadError(response(500))).toBe('network')
    expect(classifyLoadError(response(503))).toBe('network')
    expect(classifyLoadError(response(0))).toBe('network')
  })

  it('takes any other answer as unreadable', () => {
    expect(classifyLoadError(response(409))).toBe('corrupt')
    expect(classifyLoadError(response(416))).toBe('corrupt')
  })

  it('takes a parse failure as corrupt', () => {
    expect(classifyLoadError({ name: 'InvalidPDFException', message: 'Invalid PDF structure.' })).toBe('corrupt')
    expect(classifyLoadError(unknown('Bad XRef entry', 'FormatError: Bad XRef entry'))).toBe('corrupt')
    expect(classifyLoadError(new Error('anything'))).toBe('corrupt')
    expect(classifyLoadError('a string')).toBe('corrupt')
    expect(classifyLoadError(null)).toBe('corrupt')
  })

  it('takes a fetch that never reached the server as a network failure, in each engine’s words', () => {
    expect(classifyLoadError(unknown('Failed to fetch', 'TypeError: Failed to fetch'))).toBe('network')
    expect(classifyLoadError(unknown('Load failed', 'TypeError: Load failed'))).toBe('network')
    expect(classifyLoadError(new TypeError('NetworkError when attempting to fetch resource.'))).toBe('network')
  })

  it('takes any failure while offline as a network one, but not a server’s answer', () => {
    expect(classifyLoadError({ name: 'InvalidPDFException' }, true)).toBe('network')
    expect(classifyLoadError(response(404), true)).toBe('gone')
  })

  it('shows nothing for work the viewer cancelled itself', () => {
    expect(classifyLoadError({ name: 'AbortException', message: 'Loading aborted' })).toBe('cancelled')
    expect(classifyLoadError(new Error('Loading aborted'))).toBe('cancelled')
    expect(classifyLoadError(new Error('Worker was destroyed during onPassword callback'))).toBe('cancelled')
    expect(classifyLoadError({ name: 'RenderingCancelledException' }, true)).toBe('cancelled')
  })
})

describe('a page that cannot be drawn', () => {
  it('says the cause is elsewhere when it is, and the page’s own otherwise', () => {
    expect(classifyPageError(response(401))).toBe('signed-out')
    expect(classifyPageError(response(404))).toBe('gone')
    expect(classifyPageError(unknown('Failed to fetch', 'TypeError: Failed to fetch'))).toBe('network')
    expect(classifyPageError(new Error('Invalid page request.'))).toBe('page')
    expect(classifyPageError({ name: 'RenderingCancelledException', message: 'Rendering cancelled, page 3' })).toBe('cancelled')
  })
})
