import { describe, expect, it } from 'vitest'
import {
  ariaShortcut,
  hasMod,
  isApplePlatform,
  matchesShortcut,
  MEDIA_KEYSHORTCUTS,
  mediaKey,
  shortcutLabel,
  ZOOM_SHORTCUTS,
  zoomKey,
  type MediaKeyEvent,
} from './shortcuts'

function key(key: string, mods: Partial<{ meta: boolean; ctrl: boolean; shift: boolean; alt: boolean }> = {}, code?: string) {
  return { key, code, metaKey: !!mods.meta, ctrlKey: !!mods.ctrl, shiftKey: !!mods.shift, altKey: !!mods.alt }
}

describe('shortcut labels', () => {
  it('writes Apple symbols on Apple platforms, in the order given', () => {
    expect(shortcutLabel('mod+shift+up', true)).toBe('⌘⇧↑')
    expect(shortcutLabel('shift+mod+z', true)).toBe('⇧⌘Z')
    expect(shortcutLabel('mod+/', true)).toBe('⌘/')
    expect(shortcutLabel('mod+shift+updown', true)).toBe('⌘⇧↑↓')
  })

  it('writes words elsewhere, modifiers first', () => {
    expect(shortcutLabel('mod+shift+up', false)).toBe('Ctrl+Shift+↑')
    expect(shortcutLabel('shift+mod+z', false)).toBe('Ctrl+Shift+Z')
    expect(shortcutLabel('mod+[', false)).toBe('Ctrl+[')
    expect(shortcutLabel('esc', false)).toBe('Esc')
  })

  it('names the file keys Finder and Explorer use', () => {
    expect(shortcutLabel('mod+backspace', true)).toBe('⌘⌫')
    expect(shortcutLabel('mod+backspace', false)).toBe('Ctrl+Backspace')
    expect(shortcutLabel('alt+mod+backspace', true)).toBe('⌥⌘⌫')
    expect(shortcutLabel('alt+mod+n', false)).toBe('Ctrl+Alt+N')
    expect(shortcutLabel('delete', true)).toBe('⌦')
    expect(shortcutLabel('delete', false)).toBe('Delete')
    expect(shortcutLabel('f2', true)).toBe('F2')
  })

  it('names keys for aria-keyshortcuts', () => {
    expect(ariaShortcut('mod+[', true)).toBe('Meta+[')
    expect(ariaShortcut('mod+shift+up', false)).toBe('Control+Shift+ArrowUp')
  })

  it('knows an Apple platform', () => {
    expect(isApplePlatform('MacIntel')).toBe(true)
    expect(isApplePlatform('iPhone')).toBe(true)
    expect(isApplePlatform('Win32')).toBe(false)
  })
})

describe('reading a shortcut off a key event', () => {
  it('takes Meta on Apple platforms, and Ctrl there too, never both', () => {
    expect(hasMod(key('z', { meta: true }), true)).toBe(true)
    expect(hasMod(key('z', { ctrl: true }), true)).toBe(true)
    expect(hasMod(key('z', { ctrl: true, meta: true }), true)).toBe(false)
    expect(hasMod(key('z', { meta: true }), false)).toBe(false)
    expect(hasMod(key('z', { ctrl: true }), false)).toBe(true)
  })

  it('matches modifiers exactly and keys by name or position', () => {
    expect(matchesShortcut(key('ArrowUp', { meta: true, shift: true }), 'mod+shift+up', true)).toBe(true)
    expect(matchesShortcut(key('ArrowUp', { meta: true }), 'mod+shift+up', true)).toBe(false)
    expect(matchesShortcut(key('C', { ctrl: true, shift: true }, 'KeyC'), 'mod+shift+c', false)).toBe(true)
    expect(matchesShortcut(key('z', { ctrl: true }), 'mod+shift+z', false)).toBe(false)
    expect(matchesShortcut(key('?', { meta: true, shift: true }, 'Slash'), 'mod+/', true)).toBe(false)
    expect(matchesShortcut(key('/', { meta: true }, 'Slash'), 'mod+/', true)).toBe(true)
    expect(matchesShortcut(key('/', { ctrl: true, shift: true }, 'Digit7'), 'mod+/', false)).toBe(true)
    expect(matchesShortcut(key('「', { ctrl: true }, 'BracketLeft'), 'mod+[', false)).toBe(true)
    expect(matchesShortcut(key('ArrowDown', { ctrl: true }), 'down', false)).toBe(false)
    expect(matchesShortcut(key('ArrowDown'), 'down', false)).toBe(true)
  })

  it('reads the file keys, the Option ones by the key they sit on', () => {
    expect(matchesShortcut(key('Backspace', { meta: true }), 'mod+backspace', true)).toBe(true)
    expect(matchesShortcut(key('Backspace', { meta: true, alt: true }), 'mod+backspace', true)).toBe(false)
    expect(matchesShortcut(key('Backspace', { meta: true, alt: true }), 'alt+mod+backspace', true)).toBe(true)
    expect(matchesShortcut(key('˜', { meta: true, alt: true }, 'KeyN'), 'alt+mod+n', true)).toBe(true)
    expect(matchesShortcut(key('√', { meta: true, alt: true }, 'KeyV'), 'mod+v', true)).toBe(false)
    expect(matchesShortcut(key('Delete'), 'delete', false)).toBe(true)
    expect(matchesShortcut(key('Delete', { ctrl: true }), 'delete', false)).toBe(false)
    expect(matchesShortcut(key('F2'), 'f2', true)).toBe(true)
    expect(matchesShortcut(key('End'), 'end', false)).toBe(true)
  })
})

describe('the PDF viewer’s zoom keys', () => {
  it('labels them as Safari and Chrome do, Ctrl in place of ⌘ elsewhere', () => {
    expect(shortcutLabel(ZOOM_SHORTCUTS.in, true)).toBe('⌘+')
    expect(shortcutLabel(ZOOM_SHORTCUTS.out, true)).toBe('⌘−')
    expect(shortcutLabel(ZOOM_SHORTCUTS.reset, true)).toBe('⌘0')
    expect(shortcutLabel(ZOOM_SHORTCUTS.in, false)).toBe('Ctrl++')
    expect(shortcutLabel(ZOOM_SHORTCUTS.out, false)).toBe('Ctrl+−')
    expect(shortcutLabel(ZOOM_SHORTCUTS.reset, false)).toBe('Ctrl+0')
    expect(ariaShortcut(ZOOM_SHORTCUTS.in, true)).toBe('Meta+Plus')
    expect(ariaShortcut(ZOOM_SHORTCUTS.out, false)).toBe('Control+-')
  })

  it('zooms in on ⌘=, ⌘⇧= and any key that types +', () => {
    expect(zoomKey(key('=', { meta: true }, 'Equal'), true)).toBe('in')
    expect(zoomKey(key('+', { meta: true, shift: true }, 'Equal'), true)).toBe('in')
    expect(zoomKey(key('+', { ctrl: true }, 'BracketRight'), false)).toBe('in')
    expect(zoomKey(key('+', { ctrl: true }, 'NumpadAdd'), false)).toBe('in')
  })

  it('zooms out on − and resets on 0, by the key typed or the key it sits on', () => {
    expect(zoomKey(key('-', { meta: true }, 'Minus'), true)).toBe('out')
    expect(zoomKey(key('-', { ctrl: true }, 'NumpadSubtract'), false)).toBe('out')
    expect(zoomKey(key('0', { meta: true }, 'Digit0'), true)).toBe('reset')
    expect(zoomKey(key('0', { ctrl: true }, 'Numpad0'), false)).toBe('reset')
    expect(zoomKey(key('à', { ctrl: true }, 'Digit0'), false)).toBe('reset')
  })

  it('needs the platform’s command key alone, and never Alt', () => {
    expect(zoomKey(key('='), true)).toBeNull()
    expect(zoomKey(key('=', { ctrl: true }), false)).toBe('in')
    expect(zoomKey(key('=', { meta: true }), false)).toBeNull()
    expect(zoomKey(key('=', { meta: true, ctrl: true }), true)).toBeNull()
    expect(zoomKey(key('≠', { meta: true, alt: true }, 'Equal'), true)).toBeNull()
    expect(zoomKey(key('0', { ctrl: true, alt: true }, 'Digit0'), false)).toBeNull()
  })

  it('leaves every other key with the command key alone', () => {
    expect(zoomKey(key(')', { meta: true, shift: true }, 'Digit0'), true)).toBeNull()
    expect(zoomKey(key('9', { meta: true }, 'Digit9'), true)).toBeNull()
    expect(zoomKey(key('f', { meta: true }, 'KeyF'), true)).toBeNull()
  })

  it('goes by the character for in and out, not the US key it sits on', () => {
    expect(zoomKey(key('ß', { ctrl: true }, 'Minus'), false)).toBeNull()
    expect(zoomKey(key('´', { ctrl: true }, 'Equal'), false)).toBeNull()
    expect(zoomKey(key(')', { ctrl: true }, 'Minus'), false)).toBeNull()
  })
})

describe('the player’s keys', () => {
  function press(key: string, code?: string, more: Partial<MediaKeyEvent> = {}): MediaKeyEvent {
    return { key, code, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, repeat: false, ...more }
  }

  it('plays, seeks, goes full screen and mutes', () => {
    expect(mediaKey(press(' ', 'Space'))).toBe('toggle')
    expect(mediaKey(press('ArrowLeft', 'ArrowLeft'))).toBe('back')
    expect(mediaKey(press('ArrowRight', 'ArrowRight'))).toBe('forward')
    expect(mediaKey(press('f', 'KeyF'))).toBe('fullscreen')
    expect(mediaKey(press('F', 'KeyF'))).toBe('fullscreen')
    expect(mediaKey(press('m', 'KeyM'))).toBe('mute')
  })

  it('reads F and M by the key they sit on, under any input source', () => {
    expect(mediaKey(press('ㄑ', 'KeyF'))).toBe('fullscreen')
    expect(mediaKey(press('ㄩ', 'KeyM'))).toBe('mute')
  })

  it('leaves every key with a modifier to the browser, Shift included', () => {
    for (const modifier of ['metaKey', 'ctrlKey', 'altKey', 'shiftKey'] as const) {
      expect(mediaKey(press(' ', 'Space', { [modifier]: true }))).toBeNull()
      expect(mediaKey(press('ArrowRight', 'ArrowRight', { [modifier]: true }))).toBeNull()
      expect(mediaKey(press('f', 'KeyF', { [modifier]: true }))).toBeNull()
    }
  })

  it('takes a held Space, F or M without repeating it, and lets a held arrow repeat', () => {
    expect(mediaKey(press(' ', 'Space', { repeat: true }))).toBe('held')
    expect(mediaKey(press('f', 'KeyF', { repeat: true }))).toBe('held')
    expect(mediaKey(press('m', 'KeyM', { repeat: true }))).toBe('held')
    expect(mediaKey(press('ArrowRight', 'ArrowRight', { repeat: true }))).toBe('forward')
    expect(mediaKey(press('ArrowLeft', 'ArrowLeft', { repeat: true }))).toBe('back')
  })

  it('leaves F to the page on an audio file, held or not', () => {
    expect(mediaKey(press('f', 'KeyF'), 'audio')).toBeNull()
    expect(mediaKey(press('ㄑ', 'KeyF'), 'audio')).toBeNull()
    expect(mediaKey(press('f', 'KeyF', { repeat: true }), 'audio')).toBeNull()
    expect(mediaKey(press('m', 'KeyM'), 'audio')).toBe('mute')
    expect(mediaKey(press(' ', 'Space', { repeat: true }), 'audio')).toBe('held')
  })

  it('leaves a composing input method, Tab, Escape and other keys alone', () => {
    expect(mediaKey(press(' ', 'Space', { isComposing: true }))).toBeNull()
    expect(mediaKey(press('Process', 'KeyF'))).toBeNull()
    expect(mediaKey(press('Tab', 'Tab'))).toBeNull()
    expect(mediaKey(press('Escape', 'Escape'))).toBeNull()
    expect(mediaKey(press('k', 'KeyK'))).toBeNull()
    expect(mediaKey(press('ArrowUp', 'ArrowUp'))).toBeNull()
  })

  it('announces exactly the keys it reads, audio without F', () => {
    const events: Record<string, MediaKeyEvent> = {
      Space: press(' ', 'Space'),
      ArrowLeft: press('ArrowLeft', 'ArrowLeft'),
      ArrowRight: press('ArrowRight', 'ArrowRight'),
      F: press('f', 'KeyF'),
      M: press('m', 'KeyM'),
    }
    for (const [layout, names] of Object.entries(MEDIA_KEYSHORTCUTS) as [keyof typeof MEDIA_KEYSHORTCUTS, string][]) {
      for (const name of Object.keys(events)) {
        expect(mediaKey(events[name]!, layout) !== null).toBe(names.split(' ').includes(name))
      }
    }
    expect(MEDIA_KEYSHORTCUTS.video.split(' ')).toEqual(Object.keys(events))
    expect(MEDIA_KEYSHORTCUTS.audio.split(' ')).toEqual(Object.keys(events).filter((name) => name !== 'F'))
  })
})
