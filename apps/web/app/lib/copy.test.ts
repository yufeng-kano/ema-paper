import { describe, expect, it } from 'vitest'
import { cellGroupsToMarkdown, nodesToMarkdown, type MarkdownNode } from './copy'

/** Minimal DOM stand-ins — the serializer reads a structural subset of `Node`. */
function el(
  tagName: string,
  attrs: Record<string, string> = {},
  childNodes: MarkdownNode[] = [],
): MarkdownNode {
  return {
    nodeType: 1,
    tagName: tagName.toUpperCase(),
    childNodes,
    getAttribute: (name: string) => attrs[name] ?? null,
    textContent: childNodes.map((child) => child.textContent ?? '').join(''),
  }
}

function text(content: string): MarkdownNode {
  return { nodeType: 3, textContent: content }
}

function mathSpan(tex: string, rendered: string): MarkdownNode {
  return el('span', { class: 'md-inline-math', 'data-tex': tex }, [text(rendered)])
}

describe('nodesToMarkdown', () => {
  it('turns a table back into a pipe table, math and escaped pipes included', () => {
    const table = el('table', { class: 'md-table' }, [
      el('thead', {}, [
        el('tr', {}, [
          el('th', {}, [text('Name')]),
          el('th', {}, [text('Value')]),
        ]),
      ]),
      el('tbody', {}, [
        el('tr', {}, [
          el('td', {}, [text('H'), mathSpan('$_2$', '2'), text('O')]),
          el('td', {}, [text('a|b')]),
        ]),
      ]),
    ])

    expect(nodesToMarkdown([el('div', { class: 'md-table-wrap' }, [table])])).toBe(
      ['| Name | Value |', '| --- | --- |', '| H$_2$O | a\\|b |'].join('\n'),
    )
  })

  it('gives a partial table selection a separator so it stays a table', () => {
    const partial = el('table', {}, [
      el('tbody', {}, [
        el('tr', {}, [el('td', {}, [text('EMEA')]), el('td', {}, [text('903')])]),
      ]),
    ])

    expect(nodesToMarkdown([partial])).toBe(['| EMEA | 903 |', '| --- | --- |'].join('\n'))
  })

  it('serializes headings, lists, code and display math by their viewer classes', () => {
    const nodes = [
      el('h3', { class: 'md-h2' }, [text('Results')]),
      el('ol', { start: '5', class: 'md-list' }, [
        el('li', {}, [text('Fifth')]),
        el('li', {}, [text('Sixth')]),
      ]),
      el('pre', { class: 'md-math' }, [text('E = mc^2')]),
      el('pre', { class: 'md-math' }, [
        el('code', { class: 'language-python' }, [text('print(1)')]),
      ]),
    ]

    expect(nodesToMarkdown(nodes)).toBe(
      [
        '## Results',
        '5. Fifth\n6. Sixth',
        '$$\nE = mc^2\n$$',
        '```python\nprint(1)\n```',
      ].join('\n\n'),
    )
  })

  it('keeps the source heading depth from data-level', () => {
    const nodes = [
      el('h3', { class: 'md-h2', 'data-level': '1' }, [text('Top')]),
      el('h4', { class: 'md-h3', 'data-level': '4' }, [text('Deep')]),
    ]

    expect(nodesToMarkdown(nodes)).toBe('# Top\n\n#### Deep')
  })

  it('hands a figure back as its stored placeholder source', () => {
    const figure = el(
      'figure',
      { class: 'md-figure', 'data-md': '![fig1](sightread://p3/10,20,30,40)\nFigure 1: chart' },
      [el('div', { class: 'figure-frame' }, [text('No stored crop for this figure')])],
    )

    expect(nodesToMarkdown([figure])).toBe('![fig1](sightread://p3/10,20,30,40)\nFigure 1: chart')
  })

  it('keeps a selection inside one paragraph as one line of text', () => {
    const nodes = [text('Qingwen Bu'), mathSpan('$^{1,2}$', '1,2'), text(' and others')]

    expect(nodesToMarkdown(nodes)).toBe('Qingwen Bu$^{1,2}$ and others')
  })

  it('separates whole blocks with blank lines', () => {
    const section = el('section', { class: 'page' }, [
      el('article', { class: 'prose' }, [
        el('h4', { class: 'md-h3' }, [text('Notes')]),
        el('p', { class: 'md-p' }, [text('Body text.')]),
      ]),
    ])

    expect(nodesToMarkdown([section])).toBe('### Notes\n\nBody text.')
  })
})

describe('nodesToMarkdown on selections that lost their structural root', () => {
  // Range.cloneContents() copies the parent chain only up to — exclusive of — the common
  // ancestor, so a drag across one table hands over bare thead/tbody/tr, one row hands
  // over bare cells, and a list hands over bare li.
  it('reassembles orphaned thead/tbody/tr runs into one table', () => {
    const nodes = [
      el('thead', {}, [
        el('tr', {}, [el('th', {}, [text('Name')]), el('th', {}, [text('Value')])]),
      ]),
      text('\n  '),
      el('tbody', {}, [
        el('tr', {}, [el('td', {}, [text('APAC')]), el('td', {}, [text('1,204')])]),
      ]),
      el('tr', {}, [el('td', {}, [text('EMEA')]), el('td', {}, [text('903')])]),
    ]

    expect(nodesToMarkdown(nodes)).toBe(
      [
        '| Name | Value |',
        '| --- | --- |',
        '| APAC | 1,204 |',
        '| EMEA | 903 |',
      ].join('\n'),
    )
  })

  it('reassembles orphaned cells into one row', () => {
    const nodes = [el('td', {}, [text('a')]), el('td', {}, [text('b')])]

    expect(nodesToMarkdown(nodes)).toBe(['| a | b |', '| --- | --- |'].join('\n'))
  })

  it('reassembles orphaned list items into one bulleted list', () => {
    const nodes = [el('li', {}, [text('First')]), el('li', {}, [text('Second')])]

    expect(nodesToMarkdown(nodes)).toBe('- First\n- Second')
  })

  it('keeps an ordered selection ordered when the live range supplies its list context', () => {
    const nodes = [el('li', {}, [text('Review')]), el('li', {}, [text('Submit')])]

    expect(nodesToMarkdown(nodes, { ordered: true, start: 5 })).toBe('5. Review\n6. Submit')
  })
})

describe('cellGroupsToMarkdown', () => {
  // Firefox reports a table-region selection as one range per cell; the copy handler
  // groups the live cells by row and this turns the groups into the one selected table.
  it('serializes row-grouped cells as one pipe table', () => {
    const rows = [
      [el('th', {}, [text('Name')]), el('th', {}, [text('Value')])],
      [el('td', {}, [text('APAC')]), el('td', {}, [text('1,204')])],
    ]

    expect(cellGroupsToMarkdown(rows)).toBe(
      ['| Name | Value |', '| --- | --- |', '| APAC | 1,204 |'].join('\n'),
    )
  })
})

describe('rendered inline markdown', () => {
  it('hands back emphasis, code and <br> from data-src', () => {
    const cell = el('td', {}, [
      el('strong', { 'data-src': '**96.5**' }, [text('96.5')]),
      text(' '),
      el('br', { 'data-src': '<br>' }),
      el('code', { class: 'md-inline-code', 'data-src': '`uv sync`' }, [text('uv sync')]),
    ])
    const row = el('tr', {}, [cell])

    expect(nodesToMarkdown([el('table', {}, [row])])).toBe(
      ['| **96.5** <br>`uv sync` |', '| --- |'].join('\n'),
    )
  })

  it('hands back <sup> and <sub> from data-src', () => {
    const paragraph = el('p', { class: 'md-p' }, [
      text('H'),
      el('sub', { class: 'md-sub', 'data-src': '<sub>2</sub>' }, [text('2')]),
      text('O and THYAGATURU'),
      el('sup', { class: 'md-sup', 'data-src': '<SUP>1</SUP>' }, [text('1')]),
    ])

    expect(nodesToMarkdown([paragraph])).toBe('H<sub>2</sub>O and THYAGATURU<SUP>1</SUP>')
  })

  it('hands back a KaTeX display block from its data-md', () => {
    const block = el('div', { class: 'md-math-block', 'data-md': '$$\nx = 1\n$$' }, [
      text('x=1'),
    ])

    expect(nodesToMarkdown([block])).toBe('$$\nx = 1\n$$')
  })
})

describe('partially selected styled elements', () => {
  it('re-wraps only the cloned children when a boundary cuts through', () => {
    const cut = el('strong', { 'data-src': '**96.5**' }, [text('6.5')])

    expect(nodesToMarkdown([el('p', { class: 'md-p' }, [cut, text(' result')])])).toBe(
      '**6.5** result',
    )
  })

  it('re-wraps a cut superscript in its own tags', () => {
    const cut = el('sup', { class: 'md-sup', 'data-src': '<sup>1,2</sup>' }, [text(',2')])

    expect(nodesToMarkdown([el('p', { class: 'md-p' }, [text('Bu'), cut])])).toBe('Bu<sup>,2</sup>')
  })

  it('drops an element cloned empty at the selection edge', () => {
    const empty = el('em', { 'data-src': '*note*' }, [])

    expect(nodesToMarkdown([el('p', { class: 'md-p' }, [empty, text('after')])])).toBe('after')
  })
})

describe('display math cut by a selection boundary', () => {
  const source = '$$\nx = 1\n$$'

  function mathBlock(id: string, glyphs: string) {
    return el('div', { class: 'md-math-block', 'data-md': source, 'data-md-id': id }, [
      el('span', { class: 'katex' }, [
        el('span', { class: 'katex-mathml' }, [text('x=1 mathml mirror')]),
        el('span', { class: 'katex-html' }, [text(glyphs)]),
      ]),
    ])
  }

  it('copies only the surviving glyph text when the block is named partial', () => {
    expect(nodesToMarkdown([mathBlock('m1', '= 1')], undefined, new Set(['m1']))).toBe('= 1')
  })

  it('still hands back the full source when the block was swallowed whole', () => {
    expect(nodesToMarkdown([mathBlock('m1', 'x = 1')])).toBe(source)
  })

  it('downgrades only the cut twin of a repeated formula', () => {
    const cut = mathBlock('m1', '= 1')
    const whole = mathBlock('m2', 'x = 1')

    expect(nodesToMarkdown([cut, whole], undefined, new Set(['m1']))).toBe(
      ['= 1', source].join('\n\n'),
    )
  })
})

describe('whitespace inside restored code spans', () => {
  it('keeps repeated spaces in an inline code span through block normalization', () => {
    const paragraph = el('p', { class: 'md-p' }, [
      text('run   '),
      el('code', { class: 'md-inline-code', 'data-src': '`a  b`' }, [text('a  b')]),
      text('   now'),
    ])

    expect(nodesToMarkdown([paragraph])).toBe('run `a  b` now')
  })
})

describe('a table of contents', () => {
  it('copies one source line per row, leader dots included', () => {
    const toc = el('div', { class: 'md-toc' }, [
      el('p', { class: 'md-toc-row' }, [
        el('span', { class: 'md-toc-title' }, [
          el('strong', { 'data-src': '**1 Introduction**' }, [text('1 Introduction')]),
        ]),
        el('span', { class: 'md-toc-leader', 'data-src': ' ' }),
        el('span', { class: 'md-toc-page' }, [el('strong', { 'data-src': '**13**' }, [text('13')])]),
      ]),
      el('p', { class: 'md-toc-row' }, [
        el('span', { class: 'md-toc-title' }, [text('1.1 Sequential decision making')]),
        el('span', { class: 'md-toc-leader dotted', 'data-src': ' . . . . . ' }),
        el('span', { class: 'md-toc-page' }, [text('13')]),
      ]),
    ])

    expect(nodesToMarkdown([toc])).toBe(
      ['**1 Introduction** **13**', '1.1 Sequential decision making . . . . . 13'].join('\n'),
    )
  })

  it('hands an answer’s section link back as its markdown, chip or plain text', () => {
    const source = '[3.2 Attention](#32-attention)'
    const chip = el('button', { class: 'md-cite', type: 'button', 'data-src': source }, [
      el('svg'),
      text('3.2 Attention'),
    ])
    const plain = el('span', { class: 'md-cite-text', 'data-src': source }, [text('3.2 Attention')])

    expect(nodesToMarkdown([el('p', { class: 'md-p' }, [text('See '), chip, text('.')])])).toBe(`See ${source}.`)
    // A selection inside the paragraph arrives without it.
    expect(nodesToMarkdown([text('See '), chip, text(' and '), plain])).toBe(`See ${source} and ${source}`)
  })
})
