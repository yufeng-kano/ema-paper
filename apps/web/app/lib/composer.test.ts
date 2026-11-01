import { describe, expect, it } from 'vitest'
import {
  acceptList,
  builtinCommands,
  commandArguments,
  commandQuery,
  extensionOf,
  formatBytes,
  isImageFile,
  matchCommands,
  passageTooLong,
  refuseAttachment,
  sectionHeading,
  sectionText,
  type CommandOption,
} from './composer'

const options: CommandOption[] = [
  { name: 'zh-summary', description: 'Add a summary after each section', hint: '[range]', group: 'skill' },
  { name: 'check-figures', description: 'Check every caption against its figure', hint: '', group: 'skill' },
  { name: 'new', description: 'Start a new conversation', hint: '', group: 'builtin' },
]

describe('commandQuery', () => {
  it('is the term after the slash while no space is typed', () => {
    expect(commandQuery('/')).toBe('')
    expect(commandQuery('/ZH')).toBe('zh')
    expect(commandQuery('/zh-summary 3')).toBeNull()
    expect(commandQuery('hello /x')).toBeNull()
  })
})

describe('matchCommands', () => {
  it('lists everything for an empty term', () => {
    expect(matchCommands(options, '').map((option) => option.name)).toEqual(['zh-summary', 'check-figures', 'new'])
  })

  it('puts name prefixes before names or descriptions that contain the term', () => {
    expect(matchCommands(options, 'ne').map((option) => option.name)).toEqual(['new'])
    expect(matchCommands(options, 'caption').map((option) => option.name)).toEqual(['check-figures'])
    expect(matchCommands(options, 's').map((option) => option.name)).toEqual(['zh-summary', 'check-figures', 'new'])
  })
})

describe('commandArguments', () => {
  it('takes the command off the front of the sent line', () => {
    expect(commandArguments('/zh-summary 3–5', 'zh-summary')).toBe('3–5')
    expect(commandArguments('/glossary', 'glossary')).toBe('')
    expect(commandArguments('plain', 'glossary')).toBe('plain')
  })
})

describe('attachments', () => {
  const limits = {
    attachment_max_bytes: 20 * 1024 * 1024,
    attachments_per_message: 2,
    attachment_extensions: ['.png', 'PDF', '.txt'],
  }

  it('reads the extension in lower case', () => {
    expect(extensionOf('Scan.PNG')).toBe('png')
    expect(extensionOf('.bashrc')).toBe('')
    expect(extensionOf('notes')).toBe('')
  })

  it('builds the picker accept list with dots', () => {
    expect(acceptList(limits.attachment_extensions)).toBe('.png,.pdf,.txt')
    expect(acceptList(undefined)).toBeUndefined()
  })

  it('tells an image by its type or its extension', () => {
    expect(isImageFile({ name: 'a.bin', type: 'image/png' })).toBe(true)
    expect(isImageFile({ name: 'photo.HEIC', type: '' })).toBe(true)
    expect(isImageFile({ name: 'paper.pdf', type: 'application/pdf' })).toBe(false)
  })

  it('refuses past the count, an unlisted type and past the size, in that order', () => {
    expect(refuseAttachment({ name: 'a.png', size: 10 }, limits, 2)).toEqual({ reason: 'count', max: 2 })
    expect(refuseAttachment({ name: 'a.mov', size: 10 }, limits, 0)).toEqual({ reason: 'type' })
    expect(refuseAttachment({ name: 'a.pdf', size: 30 * 1024 * 1024 }, limits, 0)).toEqual({
      reason: 'size',
      max: 20 * 1024 * 1024,
    })
    expect(refuseAttachment({ name: 'a.txt', size: 10 }, limits, 1)).toBeNull()
  })

  it('leaves a limit the server did not send to the server', () => {
    expect(refuseAttachment({ name: 'a.mov', size: 1e12 }, {}, 99)).toBeNull()
    expect(refuseAttachment({ name: 'a.mov', size: 1e12 }, null, 99)).toBeNull()
  })

  it('formats sizes as a person reads them', () => {
    expect(formatBytes(20 * 1024 * 1024)).toBe('20 MB')
    expect(formatBytes(2_400_000)).toBe('2.3 MB')
    expect(formatBytes(12_400)).toBe('12 KB')
    expect(formatBytes(10)).toBe('1 KB')
  })
})

describe('sectionText', () => {
  it('sends a section whole up to the cap, counted in code points', () => {
    expect(sectionText('## 3.2 注意力\n\n內文', 14)).toBe('## 3.2 注意力\n\n內文')
    // Twelve code points, more UTF-16 units than that: still within a cap of 12.
    expect(sectionText('𝑥'.repeat(12), 12)).toBe('𝑥'.repeat(12))
  })

  it('sends null rather than a cut section past the cap, and whole without one', () => {
    expect(sectionText('x'.repeat(13), 12)).toBeNull()
    expect(sectionText('x'.repeat(13), undefined)).toBe('x'.repeat(13))
  })
})

describe('sectionHeading', () => {
  it('sends a heading within the cap as it is, counted in code points', () => {
    expect(sectionHeading('### 3.2 Attention', 17)).toBe('### 3.2 Attention')
    expect(sectionHeading('𝑑'.repeat(12), 12)).toBe('𝑑'.repeat(12))
  })

  it('cuts a longer one to the cap, the ellipsis included, never inside a code point', () => {
    const cut = sectionHeading('𝑑'.repeat(20), 12)
    expect([...cut]).toHaveLength(12)
    expect(cut).toBe(`${'𝑑'.repeat(11)}…`)
    expect(sectionHeading('x'.repeat(600), undefined)).toHaveLength(600)
  })
})

describe('passageTooLong', () => {
  it('holds a passage past the cap, counted in code points, and never one within it', () => {
    expect(passageTooLong('𝑥'.repeat(12), 12)).toBe(false)
    expect(passageTooLong('x'.repeat(13), 12)).toBe(true)
  })

  it('holds nothing without a passage or without a cap from the server', () => {
    expect(passageTooLong(null, 12)).toBe(false)
    expect(passageTooLong('x'.repeat(600), undefined)).toBe(false)
  })
})

describe('builtinCommands', () => {
  const words = {
    newDescription: 'New',
    compactDescription: 'Compact',
    compactHint: '[focus]',
    renameDescription: 'Rename',
    renameHint: '[name]',
  }

  it('offers /compact and /rename only with a conversation open, never on the draft', () => {
    expect(builtinCommands(false, words).map((option) => option.name)).toEqual(['new'])
    expect(builtinCommands(true, words)).toEqual([
      { name: 'new', description: 'New', hint: '', group: 'builtin' },
      { name: 'compact', description: 'Compact', hint: '[focus]', group: 'builtin' },
      { name: 'rename', description: 'Rename', hint: '[name]', group: 'builtin' },
    ])
  })
})
