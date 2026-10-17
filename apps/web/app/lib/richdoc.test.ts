import { getSchema, type JSONContent } from '@tiptap/core'
import { describe, expect, it } from 'vitest'
import { parseResultMarkdown } from './markdown'
import { richExtensions } from './rich-extensions'
import { docToMarkdown, inlineMathAtEnd, markdownToDoc, serializeInline, sliceToMarkdown } from './richdoc'

const schema = getSchema(richExtensions())

/** The document as the editor would hold it: validated and normalised by the real schema. */
function load(markdown: string): JSONContent {
  return schema.nodeFromJSON(markdownToDoc(markdown)).toJSON() as JSONContent
}

/** What the viewer renders for `markdown`, with sources stripped — the reading, not the bytes. */
function reading(markdown: string) {
  return parseResultMarkdown(markdown).flatMap((page) =>
    page.blocks.map(({ src: _src, ...block }) => block),
  )
}

describe('an unchanged document', () => {
  it.each([
    ['a paragraph wrapped over lines', 'First line of prose\ncontinues here.'],
    ['a heading with inline math', '## 2.1 The $\\pi_\\theta$ gradient'],
    ['emphasis in either form', 'Some _italic_ and __strong__ and `code` text.'],
    ['an escaped reference', 'As shown in \\[87] and \\*not emphasis\\*.'],
    ['a list starting at 5', '5. five\n6. six'],
    ['a table with alignment and br', '| A | B |\n|:--|--:|\n| 1<br>2 | **3** |'],
    ['fenced display math', '$$\n\\nabla J(\\theta) = 1 \\tag{2.3}\n$$'],
    ['one-line display math', '$$x = 1 \\tag{1.6}$$'],
    ['a code block', '```py\nprint(1)\n  indented\n```'],
    ['a contents page', '1.1 Intro . . . . 13\n1.2 More . . . . 15'],
    ['a figure with caption', '![fig1](sightread://p3/1,2,3,4)\nFigure 1: Learning curves.'],
    ['prices', 'It cost $5 and $6 in total.'],
    ['several blocks', '# Title\n\nPara one.\n\n- a\n- b\n\n$$\nx\n$$'],
  ])('%s comes back byte for byte', (_name, markdown) => {
    expect(docToMarkdown(load(markdown))).toBe(markdown)
  })
})

describe('an edited document', () => {
  it('rewrites only the block that changed', () => {
    const doc = load('Keep _this_ exactly.\n\nChange me.')
    const changed = structuredClone(doc)
    changed.content![1]!.content = [{ type: 'text', text: 'Changed.' }]
    expect(docToMarkdown(changed)).toBe('Keep _this_ exactly.\n\nChanged.')
  })

  it('drops an emptied block', () => {
    const doc = load('One.\n\nTwo.')
    doc.content![1]!.content = []
    expect(docToMarkdown(doc)).toBe('One.')
  })

  it('writes both halves of a split paragraph fresh', () => {
    const doc = load('Alpha beta.')
    const [first] = doc.content!
    doc.content = [
      { ...first, content: [{ type: 'text', text: 'Alpha' }] },
      { ...first, content: [{ type: 'text', text: 'beta.' }] },
    ]
    expect(docToMarkdown(doc)).toBe('Alpha\n\nbeta.')
  })

  it('reads back the way it was drawn', () => {
    const edited: JSONContent = {
      type: 'doc',
      content: [
        { type: 'heading', attrs: { level: 1 }, content: [{ type: 'text', text: 'Title' }] },
        {
          type: 'paragraph',
          content: [
            { type: 'text', text: 'Costs $5 and ' },
            { type: 'text', text: 'bold', marks: [{ type: 'bold' }] },
            { type: 'text', text: ' a*b_c usage_log ' },
            { type: 'inlineMath', attrs: { latex: 'G_t' } },
            { type: 'hardBreak' },
            { type: 'text', text: 'x', marks: [{ type: 'italic' }, { type: 'bold' }] },
          ],
        },
        { type: 'paragraph', content: [{ type: 'text', text: '# not a heading' }] },
        { type: 'paragraph', content: [{ type: 'text', text: '1. not a list' }] },
        {
          type: 'bulletList',
          content: [
            { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'one' }] }] },
            { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'two' }] }] },
          ],
        },
      ],
    }
    const markdown = docToMarkdown(edited)
    expect(reading(markdown)).toEqual([
      { kind: 'h2', text: 'Title', level: 1 },
      { kind: 'p', text: 'Costs $5 and **bold** a\\*b_c usage_log $G_t$<br>***x***' },
      { kind: 'p', text: '\\# not a heading' },
      { kind: 'p', text: '1\\. not a list' },
      { kind: 'list', ordered: false, items: ['one', 'two'] },
    ])
    // And loaded again, the editor holds what it was given.
    const again = load(markdown)
    expect(again.content![1]!.content!.map((node) => node.type)).toEqual([
      'text', 'text', 'text', 'inlineMath', 'hardBreak', 'text',
    ])
    expect(again.content![2]!.content![0]!.text).toBe('# not a heading')
  })

  it('writes a table with an escaped pipe, keeping its alignment row', () => {
    const doc = load('| a | b |\n|:--|--:|\n| 1 | 2 |')
    doc.content![0]!.content![1]!.content![0]!.content = [
      { type: 'paragraph', content: [{ type: 'text', text: 'x|y' }] },
    ]
    const markdown = docToMarkdown(doc)
    expect(markdown).toBe('| a | b |\n|:--|--:|\n| x\\|y | 2 |')
    expect(reading(markdown)).toEqual([{ kind: 'table', header: ['a', 'b'], rows: [['x|y', '2']] }])
  })

  it('flattens a pasted nested list into one level', () => {
    const doc: JSONContent = {
      type: 'doc',
      content: [
        {
          type: 'orderedList',
          attrs: { start: 3 },
          content: [
            {
              type: 'listItem',
              content: [
                { type: 'paragraph', content: [{ type: 'text', text: 'outer' }] },
                {
                  type: 'bulletList',
                  content: [{ type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'inner' }] }] }],
                },
              ],
            },
          ],
        },
      ],
    }
    expect(docToMarkdown(doc)).toBe('3. outer\n4. inner')
  })
})

describe('inline math as it is typed', () => {
  it.each([
    ['the formula $x$', { index: 12, tex: 'x' }],
    ['$\\text{\\sout{$E$}}$', { index: 0, tex: '\\text{\\sout{$E$}}' }],
    ['costs $5 and $', null],
    ['an escaped \\$x$', null],
    ['display $$x$', null],
    ['no closer', null],
  ])('%s', (text, expected) => {
    expect(inlineMathAtEnd(text)).toEqual(expected)
  })
})

describe('the clipboard', () => {
  it('copies an inline selection as one line', () => {
    expect(
      sliceToMarkdown([
        { type: 'text', text: 'a ' },
        { type: 'inlineMath', attrs: { latex: 'x' } },
      ]),
    ).toBe('a $x$')
  })

  it('keeps whitespace outside emphasis markers', () => {
    expect(serializeInline([{ type: 'text', text: ' bold ', marks: [{ type: 'bold' }] }])).toBe(' **bold** ')
  })
})
