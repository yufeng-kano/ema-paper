import { describe, expect, it } from 'vitest'
import { SLASH_CONVERSIONS, matchSlash, opensSlash, skillIcon, slashQuery } from './slash'

describe('the slash menu', () => {
  it('opens on a slash alone, ASCII or full width', () => {
    expect(opensSlash('/')).toBe(true)
    expect(opensSlash('／')).toBe(true)
    expect(opensSlash('/a')).toBe(false)
    expect(opensSlash('a/')).toBe(false)
  })

  it('reads what follows the slash as its filter', () => {
    expect(slashQuery('/Quo')).toBe('quo')
    expect(slashQuery('／標')).toBe('標')
    expect(slashQuery('text')).toBeNull()
  })

  it('matches keywords in either language by prefix', () => {
    const keys = (query: string) => matchSlash(SLASH_CONVERSIONS, query).map((item) => item.key)
    expect(keys('標')).toEqual(['heading', 'subheading'])
    expect(keys('引')).toEqual(['quote'])
    expect(keys('quote')).toEqual(['quote'])
    expect(keys('list')).toEqual(['bulleted', 'numbered'])
    expect(keys('')).toHaveLength(SLASH_CONVERSIONS.length)
    expect(keys('zzz')).toEqual([])
  })

  it('gives each skill its action’s glyph where there is one', () => {
    expect(skillIcon('translate')).toBe('languages')
    expect(skillIcon('fix-math')).toBe('square-function')
    expect(skillIcon('check-figures')).toBe('image')
    expect(skillIcon('summarize')).toBe('square-slash')
  })
})
