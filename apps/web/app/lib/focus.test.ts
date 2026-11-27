import { afterEach, describe, expect, it, vi } from 'vitest'
import { FOCUSABLE, wrapTab } from './focus'

/** Structural stand-ins: the wrap reads `document.activeElement`, `contains`, `focus` and
 *  whether the last control is a media element (`matches`). */
function control(name: string, media = false) {
  return { name, focus: vi.fn(), matches: () => media } as unknown as HTMLElement & { focus: ReturnType<typeof vi.fn> }
}

function press(shiftKey: boolean) {
  return { shiftKey, preventDefault: vi.fn() } as unknown as KeyboardEvent & { preventDefault: ReturnType<typeof vi.fn> }
}

const first = control('first')
const middle = control('middle')
const last = control('last')
const items = [first, middle, last]
const page = control('page')
const container = { contains: (el: unknown) => el === container || items.includes(el as never) } as unknown as HTMLElement

function focusOn(el: unknown) {
  vi.stubGlobal('document', { activeElement: el })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('wrapTab', () => {
  it('wraps Tab from the last control to the first, and Shift+Tab back', () => {
    focusOn(last)
    const tab = press(false)
    wrapTab(tab, container, items)
    expect(tab.preventDefault).toHaveBeenCalledOnce()
    expect(first.focus).toHaveBeenCalledOnce()

    focusOn(first)
    const back = press(true)
    wrapTab(back, container, items)
    expect(back.preventDefault).toHaveBeenCalledOnce()
    expect(last.focus).toHaveBeenCalledOnce()
  })

  it('leaves Tab between inner controls to the browser', () => {
    focusOn(middle)
    for (const shift of [false, true]) {
      const event = press(shift)
      wrapTab(event, container, items)
      expect(event.preventDefault).not.toHaveBeenCalled()
    }
  })

  it('leaves Tab on a last control that is a video or an audio player to the browser, which walks its native controls', () => {
    const player = control('player', true)
    const withPlayer = [first, middle, player]
    const dialog = { contains: (el: unknown) => withPlayer.includes(el as never) } as unknown as HTMLElement
    focusOn(player)
    const tab = press(false)
    wrapTab(tab, dialog, withPlayer)
    expect(tab.preventDefault).not.toHaveBeenCalled()
    expect(first.focus).not.toHaveBeenCalled()

    // Once Tab has left the last native control, focus is outside and comes back to the first.
    focusOn(page)
    wrapTab(press(false), dialog, withPlayer)
    expect(first.focus).toHaveBeenCalledOnce()

    // Shift+Tab on the first control still goes to the player.
    focusOn(first)
    wrapTab(press(true), dialog, withPlayer)
    expect(player.focus).toHaveBeenCalledOnce()
  })

  it('sends Shift+Tab on the container itself to the last control', () => {
    focusOn(container)
    const back = press(true)
    wrapTab(back, container, items)
    expect(last.focus).toHaveBeenCalledOnce()
  })

  it('pulls focus back in from outside the container', () => {
    focusOn(page)
    wrapTab(press(false), container, items)
    expect(first.focus).toHaveBeenCalledOnce()

    wrapTab(press(true), container, items)
    expect(last.focus).toHaveBeenCalledOnce()
  })
})

describe('FOCUSABLE', () => {
  const clauses = FOCUSABLE.split(',').map((clause) => clause.trim())

  it('counts a video’s and an audio file’s native controls, which carry no tabindex', () => {
    expect(clauses).toContain('video[controls]')
    expect(clauses).toContain('audio[controls]')
  })

  it('never counts a video or an audio element without its controls', () => {
    expect(clauses).not.toContain('video')
    expect(clauses).not.toContain('audio')
  })
})
