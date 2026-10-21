import { describe, expect, it } from 'vitest'
import { ariaShortcut, hasMod, isApplePlatform, matchesShortcut, shortcutLabel } from './shortcuts'

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
