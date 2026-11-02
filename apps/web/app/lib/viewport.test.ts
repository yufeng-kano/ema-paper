import { describe, expect, it } from 'vitest'
import { keyboardFrame } from './viewport'

describe('keyboard frame', () => {
  const box = { height: 874 }

  it('stands at the visual viewport while a keyboard covers the foot', () => {
    expect(keyboardFrame(box, { offsetTop: 0, height: 538, scale: 1 })).toEqual({ top: 0, height: 538 })
  })

  it('follows the pan Safari makes to reveal a field near the foot', () => {
    expect(keyboardFrame(box, { offsetTop: 336, height: 538, scale: 1 })).toEqual({ top: 336, height: 538 })
    expect(keyboardFrame(box, { offsetTop: 120.4, height: 537.6, scale: 1 })).toEqual({ top: 120, height: 538 })
  })

  it('leaves the box alone without a keyboard, or for an iPad’s shortcut bar', () => {
    expect(keyboardFrame(box, { offsetTop: 0, height: 874, scale: 1 })).toBeNull()
    expect(keyboardFrame(box, { offsetTop: 0, height: 819, scale: 1 })).toBeNull()
    expect(keyboardFrame(box, { offsetTop: 0, height: 774, scale: 1 })).toEqual({ top: 0, height: 774 })
  })

  it('leaves a browser that already shrank the layout alone', () => {
    expect(keyboardFrame({ height: 538 }, { offsetTop: 0, height: 538, scale: 1 })).toBeNull()
  })

  it('leaves a phone held sideways to the browser’s own pan', () => {
    const sideways = { height: 402 }
    expect(keyboardFrame(sideways, { offsetTop: 202, height: 200, scale: 1 })).toBeNull()
    expect(keyboardFrame({ height: 597 }, { offsetTop: 0, height: 320, scale: 1 })).toEqual({ top: 0, height: 320 })
  })

  it('never fights a pinch zoom', () => {
    expect(keyboardFrame(box, { offsetTop: 200, height: 300, scale: 2 })).toBeNull()
  })
})
