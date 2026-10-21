/**
 * Keys as the reader's own keyboard labels them (docs/web.md § Design system, *Shortcuts are
 * labelled for the platform*).
 *
 * Every key a label, a hint or a `Kbd` shows is written here — Apple's symbols on Apple
 * platforms (`⌘⇧↑`, `⌘/`, `⌘[`), `Ctrl+Shift+↑` elsewhere — and every shortcut is read off a
 * key event by the same rule: Meta on Apple platforms, Ctrl elsewhere, with Ctrl accepted on
 * Apple platforms too, where the browser leaves Ctrl with these keys alone. A label naming a
 * key the reader's keyboard does not have teaches the wrong habit.
 *
 * A shortcut is written as its parts joined by `+`, in the order a label shows them:
 * `mod+shift+up`, `mod+/`, `shift+mod+z`. `mod` is the platform's command key; `updown` is
 * the pair of arrows a legend shows as one.
 */

/** Whether a `navigator.platform` (or user agent) names an Apple platform. */
export function isApplePlatform(platform: string): boolean {
  return /mac|iphone|ipad|ipod/i.test(platform)
}

const APPLE: Record<string, string> = {
  mod: '⌘',
  shift: '⇧',
  alt: '⌥',
  up: '↑',
  down: '↓',
  updown: '↑↓',
  enter: '↵',
  esc: 'Esc',
  backspace: '⌫',
  delete: '⌦',
  home: 'Home',
  end: 'End',
  f2: 'F2',
}

const OTHER: Record<string, string> = {
  mod: 'Ctrl',
  shift: 'Shift',
  alt: 'Alt',
  up: '↑',
  down: '↓',
  updown: '↑↓',
  enter: '↵',
  esc: 'Esc',
  backspace: 'Backspace',
  delete: 'Delete',
  home: 'Home',
  end: 'End',
  f2: 'F2',
}

const MODIFIERS = new Set(['mod', 'shift', 'alt'])

function parts(spec: string): string[] {
  // `mod++` would be the plus key; nothing here uses it, so a plain split is enough.
  return spec.split('+').filter(Boolean)
}

/** A shortcut as the platform writes it: `⌘⇧↑` on Apple platforms, `Ctrl+Shift+↑` elsewhere
 *  (modifiers first there, in the conventional order). */
export function shortcutLabel(spec: string, apple: boolean): string {
  const names = apple ? APPLE : OTHER
  const keys = parts(spec)
  const ordered = apple
    ? keys
    : [...['mod', 'alt', 'shift'].filter((key) => keys.includes(key)), ...keys.filter((key) => !MODIFIERS.has(key))]
  const labels = ordered.map((key) => names[key] ?? (key.length === 1 ? key.toUpperCase() : key))
  return apple ? labels.join('') : labels.join('+')
}

/** Named keys by their `KeyboardEvent.key`, for `aria-keyshortcuts` and for reading an event. */
const KEY_NAMES: Record<string, string> = {
  up: 'ArrowUp',
  down: 'ArrowDown',
  enter: 'Enter',
  esc: 'Escape',
  backspace: 'Backspace',
  delete: 'Delete',
  home: 'Home',
  end: 'End',
  f2: 'F2',
}

/** The same shortcut for `aria-keyshortcuts`, in `KeyboardEvent.key` names. */
export function ariaShortcut(spec: string, apple: boolean): string {
  return parts(spec)
    .map((key) => (key === 'mod' ? (apple ? 'Meta' : 'Control') : key === 'shift' ? 'Shift' : key === 'alt' ? 'Alt' : (KEY_NAMES[key] ?? key)))
    .join('+')
}

/** What a key event carries, of what a shortcut is read from. */
export interface KeyEventLike {
  key: string
  code?: string
  metaKey: boolean
  ctrlKey: boolean
  shiftKey: boolean
  altKey: boolean
}

/** The platform's command key, and only it: Meta or Ctrl — not both — on Apple platforms,
 *  Ctrl without Meta elsewhere. */
export function hasMod(event: KeyEventLike, apple: boolean): boolean {
  return apple ? event.metaKey !== event.ctrlKey : event.ctrlKey && !event.metaKey
}

/** The `KeyboardEvent.code` a character key sits on, so a shortcut is read on any layout. */
const CODES: Record<string, string> = { '/': 'Slash', '[': 'BracketLeft', ']': 'BracketRight' }

/** Whether a key event is the shortcut: its modifiers exactly, and its key by name or by the
 *  physical key — `⌘⇧C` arrives as `C`, and `⌘/` as `?` where Shift is part of the slash. */
export function matchesShortcut(event: KeyEventLike, spec: string, apple: boolean): boolean {
  const keys = parts(spec)
  const key = keys.find((part) => !MODIFIERS.has(part))
  if (!key || keys.includes('mod') !== hasMod(event, apple)) {
    return false
  }
  if (!keys.includes('mod') && (event.metaKey || event.ctrlKey)) {
    return false
  }
  if (keys.includes('alt') !== event.altKey) {
    return false
  }
  const shift = keys.includes('shift') === event.shiftKey
  const name = KEY_NAMES[key]
  if (name) {
    return shift && event.key === name
  }
  if (key.length === 1 && /[a-z]/i.test(key)) {
    return shift && (event.key.toLowerCase() === key.toLowerCase() || event.code === `Key${key.toUpperCase()}`)
  }
  // Punctuation is the character typed, whatever it took to type it — `/` is Shift+7 on a
  // German keyboard — or, failing that, the key it sits on in the US layout.
  return event.key === key || (shift && CODES[key] !== undefined && event.code === CODES[key])
}
