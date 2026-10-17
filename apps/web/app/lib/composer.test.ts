import { describe, expect, it } from 'vitest'
import {
  acceptList,
  commandArguments,
  commandQuery,
  extensionOf,
  formatBytes,
  isImageFile,
  matchCommands,
  refuseAttachment,
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
