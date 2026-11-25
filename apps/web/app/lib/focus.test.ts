import { afterEach, describe, expect, it, vi } from 'vitest'
import { wrapTab } from './focus'

/** Structural stand-ins: the wrap reads `document.activeElement`, `contains` and `focus`. */
function control(name: string) {
  return { name, focus: vi.fn() } as unknown as HTMLElement & { focus: ReturnType<typeof vi.fn> }
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
