import { describe, expect, it } from 'vitest'
import { carries, carriesFiles, claimDrag, dragClaim, droppedFiles, setDropEffect } from './dnd'

/** A drag event is a plain object here — these helpers only ever touch four properties. */
function dragEvent(transfer: Partial<DataTransfer> | null = null): DragEvent {
  return { dataTransfer: transfer } as unknown as DragEvent
}

describe('claims', () => {
  it('gives the event to the first handler that takes it', () => {
    const event = dragEvent()
    expect(dragClaim(event)).toBeUndefined()

    claimDrag(event, 'target')
    // The list wraps the row, so it sees the same event a moment later — and must not
    // take it away from the row that is actually under the pointer.
    claimDrag(event, 'list')

    expect(dragClaim(event)).toBe('target')
  })

  it('keeps claims per event, so one frame never speaks for the next', () => {
    const first = dragEvent()
    const second = dragEvent()
    claimDrag(first, 'list')

    expect(dragClaim(first)).toBe('list')
    expect(dragClaim(second)).toBeUndefined()
  })
})

describe('carries', () => {
  it('tells whether a drag carries a type', () => {
    const transfer = { types: ['text/plain', 'application/x-sightread-rows'] } as unknown as DataTransfer
    expect(carries(transfer, 'application/x-sightread-rows')).toBe(true)
    expect(carries(transfer, 'Files')).toBe(false)
  })

  it('is false for no transfer at all', () => {
    expect(carries(null, 'text/plain')).toBe(false)
  })
})

describe('carriesFiles', () => {
  it('recognises a drag from outside the browser', () => {
    expect(carriesFiles({ types: ['Files'] } as unknown as DataTransfer)).toBe(true)
  })

  it('is false for a row of this page, and for no transfer at all', () => {
    expect(carriesFiles({ types: ['text/plain'] } as unknown as DataTransfer)).toBe(false)
    expect(carriesFiles(null)).toBe(false)
  })
})

describe('setDropEffect', () => {
  it('sets the cursor the drop will produce', () => {
    const transfer = { dropEffect: 'copy' } as DataTransfer
    setDropEffect(dragEvent(transfer), 'move')
    expect(transfer.dropEffect).toBe('move')
  })

  it('does nothing when the event carries no transfer', () => {
    expect(() => setDropEffect(dragEvent(null), 'none')).not.toThrow()
  })
})

describe('droppedFiles', () => {
  it('reads the files off a drop, and is empty for a row drag', () => {
    const file = { name: 'a.pdf' } as File
    expect(droppedFiles(dragEvent({ files: [file] } as unknown as DataTransfer))).toEqual([file])
    expect(droppedFiles(dragEvent({ files: [] } as unknown as DataTransfer))).toEqual([])
    expect(droppedFiles(dragEvent(null))).toEqual([])
  })
})
