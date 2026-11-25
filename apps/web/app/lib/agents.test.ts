import { describe, expect, it } from 'vitest'
import { charLength, instructionsUpdate, type InstructionsValues } from './agents'

describe('charLength', () => {
  it('counts the trimmed text in code points', () => {
    expect(charLength('  ab\n')).toBe(2)
    // One emoji is two UTF-16 units and one character to the server.
    expect(charLength('😀x')).toBe(2)
    expect(charLength('   ')).toBe(0)
  })
})

describe('instructionsUpdate', () => {
  const saved: InstructionsValues = { instructions: 'Answer briefly.', answer: '', translate: 'en' }

  it('has nothing to send while nothing changed', () => {
    expect(instructionsUpdate({ ...saved }, saved, 100)).toBeNull()
    // Whitespace around the text is not a change: the server trims it.
    expect(instructionsUpdate({ ...saved, instructions: ' Answer briefly.\n' }, saved, 100)).toBeNull()
  })

  it('sends the trimmed instructions with the languages, null for none', () => {
    expect(instructionsUpdate({ ...saved, instructions: ' Be terse. ' }, saved, 100)).toEqual({
      instructions: 'Be terse.',
      answer_language: null,
      translate_language: 'en',
    })
    expect(instructionsUpdate({ ...saved, instructions: '' }, saved, 100)).toEqual({
      instructions: null,
      answer_language: null,
      translate_language: 'en',
    })
  })

  it('refuses changed instructions past the cap, and counts without a cap while it is unknown', () => {
    expect(instructionsUpdate({ ...saved, instructions: 'x'.repeat(11) }, saved, 10)).toBeNull()
    expect(instructionsUpdate({ ...saved, instructions: 'x'.repeat(11) }, saved, null)).not.toBeNull()
  })

  it('saves a language change under stored instructions longer than the cap, without them', () => {
    const long: InstructionsValues = { ...saved, instructions: 'x'.repeat(20) }
    const update = instructionsUpdate({ ...long, answer: 'ja' }, long, 10)
    expect(update).toEqual({ answer_language: 'ja', translate_language: 'en' })
    expect(update).not.toHaveProperty('instructions')
  })
})
