import { describe, expect, it } from 'vitest'
import {
  appendExcerpt,
  excerptMarkdown,
  excerptMarkers,
  excerptPassage,
  findPassage,
  linkResolves,
  matchExcerpts,
  sectionOf,
} from './excerpts'
import { documentOutline, parseResultMarkdown } from './markdown'

const document = [
  '<!-- page: 1 -->',
  'A preface before any heading.',
  '',
  '## 3.2.1 Scaled Dot-Product Attention',
  '',
  'We suspect that for large values of $d_k$, the dot products grow large in magnitude.',
  '',
  'To counteract this effect, we scale.',
  '',
  '## 4 Results',
  '',
  'To counteract this effect, we scale.',
].join('\n')
const pages = parseResultMarkdown(document)
const outline = documentOutline(pages)

describe('excerptPassage', () => {
  it('unwraps quotes and gives figures as their captions, dropping captionless ones', () => {
    expect(excerptPassage('> quoted text\n\n![f](sightread://p1/0,0,1,1)\nFigure 2: Curves\n\n![g](sightread://p1/0,0,1,1)')).toBe(
      'quoted text\n\nFigure 2: Curves',
    )
    expect(excerptPassage('![g](sightread://p1/0,0,1,1)')).toBeNull()
  })
})

describe('excerptMarkdown', () => {
  it('quotes the passage over its section’s link', () => {
    expect(excerptMarkdown('First.\n\nSecond.', { text: '3.2 [Attention]', slug: '32-attention' }, 'Beginning of the document')).toBe(
      '> First.\n>\n> Second.\n\n[3.2 \\[Attention\\]](#32-attention)',
    )
    expect(excerptMarkdown('Preface.', null, '文件開頭')).toBe('> Preface.\n\n[文件開頭](#)')
  })
})

describe('appendExcerpt', () => {
  const excerpt = (passage: string) => excerptMarkdown(passage, { text: 'Intro', slug: 'intro' }, '')

  it('makes the section at the end of the notes when there is none', () => {
    const added = appendExcerpt('# My notes\n\nThoughts.\n', 'Quoted.', excerpt('Quoted.'), '## Excerpts')
    expect(added.notes).toBe('# My notes\n\nThoughts.\n\n## Excerpts\n\n> Quoted.\n\n[Intro](#intro)\n')
    expect(added.notes.slice(added.at)).toMatch(/^> Quoted\./)
    expect(appendExcerpt('', 'Quoted.', excerpt('Quoted.'), '## 摘錄').notes).toBe('## 摘錄\n\n> Quoted.\n\n[Intro](#intro)')
  })

  it('goes at the end of the Excerpts section in either language', () => {
    const notes = '## 摘錄\n\n> Old.\n\n[Intro](#intro)\n\n## Later\n\nText.'
    const added = appendExcerpt(notes, 'New.', excerpt('New.'), '## Excerpts')
    expect(added.notes).toBe('## 摘錄\n\n> Old.\n\n[Intro](#intro)\n\n> New.\n\n[Intro](#intro)\n\n## Later\n\nText.')
    expect(added.notes.slice(added.at)).toMatch(/^> New\./)
  })

  it('never adds a passage twice, comparing whole text and not containment', () => {
    const notes = '## Excerpts\n\n> Some   words $x$.\n\n[Intro](#intro)'
    const again = appendExcerpt(notes, 'Some words $x$.', excerpt('Some words $x$.'), '## Excerpts')
    expect(again).toEqual({ notes, at: notes.indexOf('> Some'), duplicate: true })
    expect(appendExcerpt(notes, 'Some words', excerpt('Some words'), '## Excerpts').duplicate).toBe(false)
  })
})

describe('findPassage', () => {
  it('finds a passage across parts with spacing ignored', () => {
    const parts = ['We suspect that ', 'for large values of ', 'd_k', ', the dot products']
    expect(findPassage(parts, 'for large values of $d_k$'.replace(/\$/g, ''))).toEqual({
      start: { part: 1, offset: 0 },
      end: { part: 2, offset: 3 },
    })
    expect(findPassage(parts, 'nowhere')).toBeNull()
  })
})

describe('matchExcerpts', () => {
  const notes = [
    '## Excerpts',
    '',
    '> To counteract this effect, we scale.',
    '',
    '[4 Results](#4-results)',
    '',
    '> We suspect that for large values of $d_k$,',
    '',
    '[3.2.1 Scaled Dot-Product Atention](#321-scaled-dot-product-atention)',
    '',
    '> A passage no longer there.',
    '',
    '[Gone heading](#gone)',
  ].join('\n')

  it('finds each passage in its linked section first, and one whose heading was renamed anywhere', () => {
    const matches = matchExcerpts(notes, pages, outline)
    expect(matches.map((match) => match.found)).toEqual([
      { start: { page: 0, index: 5 }, end: { page: 0, index: 5 } },
      { start: { page: 0, index: 2 }, end: { page: 0, index: 2 } },
      null,
    ])
    expect(matches[1]!.heading).toBeNull()
    expect([...excerptMarkers(matches).keys()]).toEqual(['0-5', '0-2'])
  })

  it('says which links still lead somewhere', () => {
    expect(linkResolves(pages, outline, { label: 'Gone', anchor: 'gone', passage: null })).toBe(false)
    expect(linkResolves(pages, outline, { label: 'Gone', anchor: 'gone', passage: 'we scale.' })).toBe(true)
    expect(linkResolves(pages, outline, { label: 'Beginning of the document', anchor: '', passage: null })).toBe(true)
    expect(linkResolves(pages, outline, { label: '4 Results', anchor: 'x', passage: null })).toBe(true)
  })

  it('names the section a block sits in', () => {
    expect(sectionOf(outline, { page: 0, index: 0 })).toBeNull()
    expect(sectionOf(outline, { page: 0, index: 2 })?.slug).toBe('321-scaled-dot-product-attention')
    expect(sectionOf(outline, { page: 0, index: 4 })?.slug).toBe('4-results')
  })
})
