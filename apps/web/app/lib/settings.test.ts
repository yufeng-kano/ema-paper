import { describe, expect, it } from 'vitest'
import { settingsSectionOf } from './settings'

describe('settingsSectionOf', () => {
  it('is null while the query does not open the dialog', () => {
    expect(settingsSectionOf(undefined)).toBeNull()
  })

  it('opens the section the query names', () => {
    expect(settingsSectionOf('usage')).toBe('usage')
    expect(settingsSectionOf('keys')).toBe('keys')
  })

  it('opens Usage for billing, where Kano Pay sends the reader back', () => {
    expect(settingsSectionOf('billing')).toBe('usage')
  })

  it('opens General for a section that does not exist', () => {
    expect(settingsSectionOf('dashboard')).toBe('general')
    expect(settingsSectionOf(['usage'])).toBe('general')
  })
})
