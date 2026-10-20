import { describe, expect, it } from 'vitest'
import { LANGUAGE_TAGS, isListedLanguage, languageOptions } from './languages'

describe('languageOptions', () => {
  it('lists the fixed languages when nothing, or a listed tag, is stored', () => {
    expect(languageOptions(null)).toEqual([...LANGUAGE_TAGS])
    expect(languageOptions(undefined)).toEqual([...LANGUAGE_TAGS])
    expect(languageOptions('ja')).toEqual([...LANGUAGE_TAGS])
  })

  it('keeps a stored tag the list lacks as the last option, so it still reads as current', () => {
    expect(languageOptions('pt-BR')).toEqual([...LANGUAGE_TAGS, 'pt-BR'])
  })

  it('is a copy: changing what it returns never changes the list', () => {
    languageOptions(null).push('xx')
    expect(LANGUAGE_TAGS).not.toContain('xx')
  })
})

describe('isListedLanguage', () => {
  it('matches tags exactly, case included', () => {
    expect(isListedLanguage('zh-TW')).toBe(true)
    expect(isListedLanguage('zh-tw')).toBe(false)
    expect(isListedLanguage('')).toBe(false)
  })
})
