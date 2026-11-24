import { describe, expect, it } from 'vitest'
import { insertAtCaret } from './caret'

describe('insertAtCaret', () => {
  it('inserts at the caret and puts the caret after the token', () => {
    expect(insertAtCaret('ab', 1, 1, '{page}')).toEqual({ text: 'a{page}b', caret: 7 })
  })

  it('replaces the selection', () => {
    expect(insertAtCaret('run X now', 4, 5, '$ARGUMENTS')).toEqual({ text: 'run $ARGUMENTS now', caret: 14 })
  })
})
