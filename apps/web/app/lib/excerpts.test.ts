import { describe, expect, it } from 'vitest'
import {
  appendExcerpt,
  droppedBlocksMarkdown,
  excerptMarkdown,
  excerptMarkers,
  excerptPassage,
  findPassage,
  linkResolves,
  matchExcerpts,
  rewriteBlockLinks,
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

describe('block references in the notes', () => {
  const address = (block: string) => `https://app.example/files?document=7&block=${block}`
  const anchorOf = (href: string) => {
    const url = new URL(href)
    return url.searchParams.get('document') === '7' ? /[?&]block=([^&#]*)/.exec(url.search)?.[1] ?? null : null
  }

  it('rewrites a link to this document as a section link, the anchor escaped', () => {
    const pasted = `See [“We suspect that…”](${address('321-scaled-dot-product-attention~We+suspect+that+for+large')}) and [other](https://app.example/files?document=8&block=x).`
    expect(rewriteBlockLinks(pasted, anchorOf)).toBe(
      'See [“We suspect that…”](#321-scaled-dot-product-attention~We%20suspect%20that%20for%20large) and [other](https://app.example/files?document=8&block=x).',
    )
    expect(rewriteBlockLinks(`[x](${address('a~Let+(x)+be')})`, anchorOf)).toBe('[x](#a~Let%20%28x%29%20be)')
    expect(rewriteBlockLinks('[a plain link](https://example.com/page)', anchorOf)).toBe('[a plain link](https://example.com/page)')
  })

  it('leaves code and escaped links as the text they show', () => {
    const link = `[x](${address('a~x')})`
    expect(rewriteBlockLinks(`Write \`${link}\` to cite.`, anchorOf)).toBe(`Write \`${link}\` to cite.`)
    expect(rewriteBlockLinks(`\`\`\`md\n${link}\n\`\`\`\n\n${link}`, anchorOf)).toBe(`\`\`\`md\n${link}\n\`\`\`\n\n[x](#a~x)`)
    expect(rewriteBlockLinks(`\\${link}`, anchorOf)).toBe(`\\${link}`)
    expect(rewriteBlockLinks(`\\\\${link}`, anchorOf)).toBe('\\\\[x](#a~x)')
  })

  it('resolves a block reference by its anchor, read back from its escapes', () => {
    expect(linkResolves(pages, outline, { label: 'x', anchor: '321-scaled-dot-product-attention~We%20suspect%20that', passage: null })).toBe(true)
    expect(linkResolves(pages, outline, { label: 'x', anchor: '~A+preface+before', passage: null })).toBe(true)
    expect(linkResolves(pages, outline, { label: 'x', anchor: 'gone~Words+no+block+has', passage: null })).toBe(false)
  })
})

describe('droppedBlocksMarkdown', () => {
  it('keeps blocks and quotes, takes a figure as its caption and leaves page markers out', () => {
    const dragged = [
      '<!-- page: 2 -->',
      '![fig1](sightread://p2/0,0,1,1)',
      'Figure 1: The architecture.',
      '',
      '> A quoted line.',
      '',
      '![fig2](sightread://p2/0,0,1,1)',
      '',
      '- one',
      '- two',
    ].join('\n')
    expect(droppedBlocksMarkdown(dragged)).toBe('Figure 1: The architecture.\n\n> A quoted line.\n\n- one\n- two')
    expect(droppedBlocksMarkdown('<!-- page: 3 -->\n![fig3](sightread://p3/0,0,1,1)')).toBeNull()
  })
})
