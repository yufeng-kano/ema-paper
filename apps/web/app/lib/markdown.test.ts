import { describe, expect, it } from 'vitest'
import {
  appendBlockSource,
  blockAnchor,
  blockPlain,
  decodeBlockAnchor,
  documentOutline,
  findHeading,
  findPlace,
  firstWords,
  headingSlug,
  imageLine,
  inlinePlain,
  insertBlocks,
  moveBlocks,
  parseInline,
  parseResultMarkdown,
  placeNear,
  replaceBlockSource,
  resolveAnchor,
  sectionSource,
  serializeDocument,
  type ResultBlock,
} from './markdown'

/** The parsed shape without each block's `src` — what the structural tests below are about;
 *  the source round trip has tests of its own at the end of this file. */
function bare(blocks: ResultBlock[] | undefined): Omit<ResultBlock, 'src'>[] | undefined {
  return blocks?.map((block) => {
    const { src: _src, ...rest } = block
    return rest
  })
}

describe('parseResultMarkdown', () => {
  it('splits on page markers and keeps block order', () => {
    const pages = parseResultMarkdown(
      [
        '<!-- page: 1 -->',
        '## Quarterly results',
        '',
        'Revenue grew across every region,',
        'led by APAC.',
        '',
        '<!-- page: 2 -->',
        '### Notes',
        '| Region | Revenue |',
        '| --- | --- |',
        '| APAC | 1,204 |',
        '| EMEA | 903 |',
      ].join('\n'),
    )

    expect(pages.map((page) => page.page)).toEqual([1, 2])
    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'h2', text: 'Quarterly results', level: 2 },
      { kind: 'p', text: 'Revenue grew across every region, led by APAC.' },
    ])
    expect(bare(pages[1]!.blocks)).toEqual([
      { kind: 'h3', text: 'Notes', level: 3 },
      {
        kind: 'table',
        header: ['Region', 'Revenue'],
        rows: [
          ['APAC', '1,204'],
          ['EMEA', '903'],
        ],
      },
    ])
  })

  it('reads a figure placeholder and the caption on the next line', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 3 -->', '![fig1](sightread://p3/120,60,480,940)', 'Figure 1: revenue by region'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      {
        kind: 'fig',
        id: 'fig1',
        page: 3,
        bbox: [120, 60, 480, 940],
        caption: 'Figure 1: revenue by region',
      },
    ])
  })

  it('leaves a figure uncaptioned when the next line starts another block', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '![fig1](sightread://p1/0,0,10,10)', '## Next section'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'fig', id: 'fig1', page: 1, bbox: [0, 0, 10, 10], caption: null },
      { kind: 'h2', text: 'Next section', level: 2 },
    ])
  })

  it('keeps content that precedes the first marker as page 1', () => {
    const pages = parseResultMarkdown(['# Title', '<!-- page: 1 -->', 'Body.'].join('\n'))

    expect(pages).toHaveLength(1)
    expect(pages[0]!.page).toBe(1)
    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'h2', text: 'Title', level: 1 },
      { kind: 'p', text: 'Body.' },
    ])
  })

  it('keeps the source heading depth on the two rendered kinds', () => {
    const pages = parseResultMarkdown(['<!-- page: 1 -->', '# Top', '#### Deep'].join('\n'))

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'h2', text: 'Top', level: 1 },
      { kind: 'h3', text: 'Deep', level: 4 },
    ])
  })

  it('returns nothing for an empty document', () => {
    expect(parseResultMarkdown('')).toEqual([])
  })
})

describe('parseResultMarkdown lists', () => {
  it('keeps an unordered list as its own block instead of merging it into a paragraph', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'Findings:', '', '- First', '- Second', '- Third'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'Findings:' },
      { kind: 'list', ordered: false, items: ['First', 'Second', 'Third'] },
    ])
  })

  it('reads an ordered list and its markers', () => {
    const pages = parseResultMarkdown(['<!-- page: 1 -->', '1. Alpha', '2) Beta'].join('\n'))

    expect(bare(pages[0]!.blocks)).toEqual([{ kind: 'list', ordered: true, items: ['Alpha', 'Beta'] }])
  })

  it('splits a bulleted list that follows a numbered one into two blocks', () => {
    const pages = parseResultMarkdown(['<!-- page: 1 -->', '1. Alpha', '- Beta'].join('\n'))

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'list', ordered: true, items: ['Alpha'] },
      { kind: 'list', ordered: false, items: ['Beta'] },
    ])
  })

  it('does not let a paragraph swallow the list that follows it', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'The regions were:', '- APAC', 'Totals follow.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'The regions were:' },
      { kind: 'list', ordered: false, items: ['APAC'] },
      { kind: 'p', text: 'Totals follow.' },
    ])
  })
})

describe('parseResultMarkdown tables', () => {
  it('reads a GFM table whose rows omit the outer pipes', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'Region | Revenue', '--- | ---', 'APAC | 1,204'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'table', header: ['Region', 'Revenue'], rows: [['APAC', '1,204']] },
    ])
  })

  it('does not let a paragraph swallow an unpiped table that follows it', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'By region:', 'Region | Revenue', '--- | ---', 'APAC | 1,204'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'By region:' },
      { kind: 'table', header: ['Region', 'Revenue'], rows: [['APAC', '1,204']] },
    ])
  })

  it('leaves a sentence containing a pipe as prose', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'Run `a | b` to pipe the output.', 'It prints nothing.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'Run `a | b` to pipe the output. It prints nothing.' },
    ])
  })

  it('ends a table at the prose under it', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '| A | B |', '| --- | --- |', '| 1 | 2 |', 'Totals are provisional.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'table', header: ['A', 'B'], rows: [['1', '2']] },
      { kind: 'p', text: 'Totals are provisional.' },
    ])
  })
})

describe('parseResultMarkdown table edge cases', () => {
  it('leaves a formula that starts with a pipe as prose', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '|x| = 3', '|y| = 4', 'Both hold for every n.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([{ kind: 'p', text: '|x| = 3 |y| = 4 Both hold for every n.' }])
  })

  it('keeps an escaped pipe inside a cell instead of splitting on it', () => {
    const pages = parseResultMarkdown(
      [
        '<!-- page: 1 -->',
        '| Vendor | Region |',
        '| --- | --- |',
        '| ACME \\| Europe | EMEA |',
      ].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      {
        kind: 'table',
        header: ['Vendor', 'Region'],
        rows: [['ACME | Europe', 'EMEA']],
      },
    ])
  })
})

describe('parseResultMarkdown formulas and wrapped items', () => {
  it('keeps the line breaks inside a display-math fence', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'Then:', '$$', '\\begin{aligned}', 'E &= mc^2', '\\end{aligned}', '$$', 'as shown.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'Then:' },
      { kind: 'math', text: '\\begin{aligned}\nE &= mc^2\n\\end{aligned}' },
      { kind: 'p', text: 'as shown.' },
    ])
  })

  it('does not run an unclosed fence past its page', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '$$', 'E = mc^2', '<!-- page: 2 -->', 'Next page.'].join('\n'),
    )

    expect(pages).toHaveLength(2)
    expect(bare(pages[1]!.blocks)).toEqual([{ kind: 'p', text: 'Next page.' }])
  })

  it('folds an indented continuation into the item above it', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '- First line', '  wrapped onto a second', '- Second'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'list', ordered: false, items: ['First line wrapped onto a second', 'Second'] },
    ])
  })

  it('ends the list at an unindented line', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '- Only item', 'A new paragraph.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'list', ordered: false, items: ['Only item'] },
      { kind: 'p', text: 'A new paragraph.' },
    ])
  })
})

describe('parseResultMarkdown code and numbering', () => {
  it('keeps a fenced code block whole, with its language', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', 'Example:', '```python', 'def f(x):', '    return x + 1', '```', 'Done.'].join('\n'),
    )

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'p', text: 'Example:' },
      { kind: 'code', text: 'def f(x):\n    return x + 1', lang: 'python' },
      { kind: 'p', text: 'Done.' },
    ])
  })

  it('does not run an unclosed code fence past its page', () => {
    const pages = parseResultMarkdown(
      ['<!-- page: 1 -->', '```', 'x = 1', '<!-- page: 2 -->', 'Next page.'].join('\n'),
    )

    expect(pages).toHaveLength(2)
    expect(bare(pages[1]!.blocks)).toEqual([{ kind: 'p', text: 'Next page.' }])
  })

  it('keeps the number an ordered list starts at', () => {
    const pages = parseResultMarkdown(['<!-- page: 2 -->', '5. Fifth', '6. Sixth'].join('\n'))

    expect(bare(pages[0]!.blocks)).toEqual([
      { kind: 'list', ordered: true, items: ['Fifth', 'Sixth'], start: 5 },
    ])
  })

  it('leaves a list starting at one without a start attribute', () => {
    const pages = parseResultMarkdown(['<!-- page: 1 -->', '1. First'].join('\n'))

    expect(bare(pages[0]!.blocks)).toEqual([{ kind: 'list', ordered: true, items: ['First'] }])
  })
})

describe('parseInline', () => {
  it('splits affiliation superscripts into math spans', () => {
    const segments = parseInline('Qingwen Bu$^{1,2}$, Yanting Yang$^2$')

    expect(segments).toEqual([
      { kind: 'text', text: 'Qingwen Bu' },
      { kind: 'math', tex: '^{1,2}' },
      { kind: 'text', text: ', Yanting Yang' },
      { kind: 'math', tex: '^2' },
    ])
  })

  it('does not mistake prices for a formula', () => {
    expect(parseInline('costs $5 and $6 total')).toEqual([
      { kind: 'text', text: 'costs $5 and $6 total' },
    ])
  })

  it('finds a formula after a rejected price on the same line', () => {
    const segments = parseInline('cost $5 and variable $x$')

    expect(segments).toEqual([
      { kind: 'text', text: 'cost $5 and variable ' },
      { kind: 'math', tex: 'x' },
    ])
  })

  it('hands plain text through as one segment', () => {
    expect(parseInline('no math here')).toEqual([{ kind: 'text', text: 'no math here' }])
    expect(parseInline('')).toEqual([{ kind: 'text', text: '' }])
  })

  it('renders strong emphasis with its source kept for copy', () => {
    expect(parseInline('**96.5**')).toEqual([
      { kind: 'strong', segments: [{ kind: 'text', text: '96.5' }], src: '**96.5**' },
    ])
  })

  it('renders <br> as a line break inside a table cell', () => {
    expect(parseInline('UniVLA<br>(Ours)')).toEqual([
      { kind: 'text', text: 'UniVLA' },
      { kind: 'br', src: '<br>' },
      { kind: 'text', text: '(Ours)' },
    ])
  })

  it('renders <sup> and <sub> with their source kept for copy', () => {
    expect(parseInline('THYAGATURU<sup>1</sup>, H<sub>2</sub>O')).toEqual([
      { kind: 'text', text: 'THYAGATURU' },
      { kind: 'sup', segments: [{ kind: 'text', text: '1' }], src: '<sup>1</sup>' },
      { kind: 'text', text: ', H' },
      { kind: 'sub', segments: [{ kind: 'text', text: '2' }], src: '<sub>2</sub>' },
      { kind: 'text', text: 'O' },
    ])
    expect(parseInline('<SUP>a</SUP>')).toEqual([
      { kind: 'sup', segments: [{ kind: 'text', text: 'a' }], src: '<SUP>a</SUP>' },
    ])
  })

  it('renders the superscripts of an author line inside its strong emphasis', () => {
    expect(parseInline('**THYAGATURU<sup>1</sup>, (Senior Member, IEEE)** <sup>1</sup>Intel')).toEqual([
      {
        kind: 'strong',
        segments: [
          { kind: 'text', text: 'THYAGATURU' },
          { kind: 'sup', segments: [{ kind: 'text', text: '1' }], src: '<sup>1</sup>' },
          { kind: 'text', text: ', (Senior Member, IEEE)' },
        ],
        src: '**THYAGATURU<sup>1</sup>, (Senior Member, IEEE)**',
      },
      { kind: 'text', text: ' ' },
      { kind: 'sup', segments: [{ kind: 'text', text: '1' }], src: '<sup>1</sup>' },
      { kind: 'text', text: 'Intel' },
    ])
  })

  it('keeps any other tag, and an escaped or unclosed <sup>, as text', () => {
    expect(parseInline('<span>x</span> \\<sup>1</sup> <sup>open')).toEqual([
      { kind: 'text', text: '<span>x</span> <sup>1</sup> <sup>open' },
    ])
  })

  it('nests emphasis one inside the other', () => {
    expect(parseInline('_**Abstract**_')).toEqual([
      {
        kind: 'em',
        segments: [
          { kind: 'strong', segments: [{ kind: 'text', text: 'Abstract' }], src: '**Abstract**' },
        ],
        src: '_**Abstract**_',
      },
    ])
  })

  it('keeps mid-word underscores as text', () => {
    expect(parseInline('usage_log and a_b_c stay text')).toEqual([
      { kind: 'text', text: 'usage_log and a_b_c stay text' },
    ])
  })

  it('renders single-asterisk emphasis', () => {
    expect(parseInline('*A. Main Results*')).toEqual([
      { kind: 'em', segments: [{ kind: 'text', text: 'A. Main Results' }], src: '*A. Main Results*' },
    ])
  })

  it('closes each emphasis span at its own marker', () => {
    expect(parseInline('**a** and **b**')).toEqual([
      { kind: 'strong', segments: [{ kind: 'text', text: 'a' }], src: '**a**' },
      { kind: 'text', text: ' and ' },
      { kind: 'strong', segments: [{ kind: 'text', text: 'b' }], src: '**b**' },
    ])
  })

  it('renders an inline code span verbatim', () => {
    expect(parseInline('run `uv sync` first')).toEqual([
      { kind: 'text', text: 'run ' },
      { kind: 'code', text: 'uv sync', src: '`uv sync`' },
      { kind: 'text', text: ' first' },
    ])
  })

  it('leaves unpaired markers as literal text', () => {
    expect(parseInline('5 * 3 and a ** that never closes')).toEqual([
      { kind: 'text', text: '5 * 3 and a ** that never closes' },
    ])
  })

  it('treats non-ASCII letters as word characters around underscores', () => {
    expect(parseInline('caf\u00e9_value_name and \u6e2c\u8a66_\u8b8a\u6578_\u540d\u7a31 stay text')).toEqual([
      { kind: 'text', text: 'caf\u00e9_value_name and \u6e2c\u8a66_\u8b8a\u6578_\u540d\u7a31 stay text' },
    ])
  })

  it('still opens underscore emphasis off punctuation and spaces', () => {
    expect(parseInline('a _b_ c')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'em', segments: [{ kind: 'text', text: 'b' }], src: '_b_' },
      { kind: 'text', text: ' c' },
    ])
  })

  it('renders a backslash-escaped marker as the character, without the backslash', () => {
    expect(parseInline('\\*literal\\*')).toEqual([{ kind: 'text', text: '*literal*' }])
    expect(parseInline('*a\\*')).toEqual([{ kind: 'text', text: '*a*' }])
  })

  it('unescapes the brackets a reference list arrives with', () => {
    expect(parseInline('\\[87] Seonghyeon Ye, Joel Jang')).toEqual([
      { kind: 'text', text: '[87] Seonghyeon Ye, Joel Jang' },
    ])
  })

  it('keeps a backslash that escapes nothing', () => {
    expect(parseInline('C:\\path and 5\\n')).toEqual([
      { kind: 'text', text: 'C:\\path and 5\\n' },
    ])
  })

  it('reads a triple-asterisk run as em holding strong', () => {
    expect(parseInline('***text***')).toEqual([
      {
        kind: 'em',
        segments: [
          { kind: 'strong', segments: [{ kind: 'text', text: 'text' }], src: '**text**' },
        ],
        src: '***text***',
      },
    ])
  })

  it('nests strong inside single-asterisk emphasis', () => {
    expect(parseInline('*outer **inner** text*')).toEqual([
      {
        kind: 'em',
        segments: [
          { kind: 'text', text: 'outer ' },
          { kind: 'strong', segments: [{ kind: 'text', text: 'inner' }], src: '**inner**' },
          { kind: 'text', text: ' text' },
        ],
        src: '*outer **inner** text*',
      },
    ])
  })

  it('nests em inside strong', () => {
    expect(parseInline('**a *b* c**')).toEqual([
      {
        kind: 'strong',
        segments: [
          { kind: 'text', text: 'a ' },
          { kind: 'em', segments: [{ kind: 'text', text: 'b' }], src: '*b*' },
          { kind: 'text', text: ' c' },
        ],
        src: '**a *b* c**',
      },
    ])
  })

  it('does not let a marker inside nested code close the emphasis', () => {
    expect(parseInline('*use `a*b` now*')).toEqual([
      {
        kind: 'em',
        segments: [
          { kind: 'text', text: 'use ' },
          { kind: 'code', text: 'a*b', src: '`a*b`' },
          { kind: 'text', text: ' now' },
        ],
        src: '*use `a*b` now*',
      },
    ])
    expect(parseInline('**use `a**b` now**')).toEqual([
      {
        kind: 'strong',
        segments: [
          { kind: 'text', text: 'use ' },
          { kind: 'code', text: 'a**b', src: '`a**b`' },
          { kind: 'text', text: ' now' },
        ],
        src: '**use `a**b` now**',
      },
    ])
  })

  it('treats an even backslash run as escaping itself, not the marker', () => {
    expect(parseInline('\\\\*word*')).toEqual([
      { kind: 'text', text: '\\' },
      { kind: 'em', segments: [{ kind: 'text', text: 'word' }], src: '*word*' },
    ])
    expect(parseInline('\\\\`$x$`')).toEqual([
      { kind: 'text', text: '\\' },
      { kind: 'code', text: '$x$', src: '`$x$`' },
    ])
  })

  it('lets a self-escaped backslash precede a live closer', () => {
    expect(parseInline('*path\\\\*')).toEqual([
      { kind: 'em', segments: [{ kind: 'text', text: 'path\\' }], src: '*path\\\\*' },
    ])
  })

  it('closes a span on the dollar at brace depth zero, not the first one', () => {
    const tex = String.raw`\text{\sout{$E'_v = 1$}}`

    expect(parseInline(`w/o Visual: $${tex}$`)).toEqual([
      { kind: 'text', text: 'w/o Visual: ' },
      { kind: 'math', tex },
    ])
  })

  it('keeps a whole cases block with nested text-mode math as one formula', () => {
    const tex = String.raw`\begin{cases} \text{\sout{$a = 1,$}} \\ b = \bcancel{2} \end{cases}`

    expect(parseInline(`$${tex}$ ends`)).toEqual([
      { kind: 'math', tex },
      { kind: 'text', text: ' ends' },
    ])
  })

  it('does not let an escaped dollar open a span', () => {
    expect(parseInline('costs \\$5 and \\$6')).toEqual([{ kind: 'text', text: 'costs $5 and $6' }])
  })

  it('keeps dollars inside a code span as code, not math', () => {
    expect(parseInline('write `$x$` literally')).toEqual([
      { kind: 'text', text: 'write ' },
      { kind: 'code', text: '$x$', src: '`$x$`' },
      { kind: 'text', text: ' literally' },
    ])
  })

  it('lets an escaped backtick protect nothing', () => {
    expect(parseInline('show \\` then $x$ and `code`')).toEqual([
      { kind: 'text', text: 'show ` then ' },
      { kind: 'math', tex: 'x' },
      { kind: 'text', text: ' and ' },
      { kind: 'code', text: 'code', src: '`code`' },
    ])
  })

  it('does not let a rejected price steal a closer from inside a later code span', () => {
    expect(parseInline('cost $5 and `$x$`')).toEqual([
      { kind: 'text', text: 'cost $5 and ' },
      { kind: 'code', text: '$x$', src: '`$x$`' },
    ])
  })

  it('lets an unclosed backtick protect nothing', () => {
    expect(parseInline('a ` b $x$')).toEqual([
      { kind: 'text', text: 'a ` b ' },
      { kind: 'math', tex: 'x' },
    ])
  })

  it('keeps underscores inside a math span out of emphasis', () => {
    expect(parseInline('$a_{1}$ and $b_{2}$')).toEqual([
      { kind: 'math', tex: 'a_{1}' },
      { kind: 'text', text: ' and ' },
      { kind: 'math', tex: 'b_{2}' },
    ])
  })
})

describe('display math with the dollars on the formula’s own line', () => {
  it('reads a one-line $$…$$ as a display block, not a paragraph', () => {
    const [page] = parseResultMarkdown(
      ['Before:', '', '$$G_t \\triangleq r_t + \\gamma r_{t+1} \\tag{1.6}$$', '', 'After.'].join('\n'),
    )
    expect(bare(page?.blocks)).toEqual([
      { kind: 'p', text: 'Before:' },
      { kind: 'math', text: 'G_t \\triangleq r_t + \\gamma r_{t+1} \\tag{1.6}' },
      { kind: 'p', text: 'After.' },
    ])
  })

  it('ends the paragraph above it without a blank line between', () => {
    const [page] = parseResultMarkdown(['where:', '$$x = 1$$'].join('\n'))
    expect(bare(page?.blocks)).toEqual([
      { kind: 'p', text: 'where:' },
      { kind: 'math', text: 'x = 1' },
    ])
  })

  it('reads dollars that open and close on content lines', () => {
    const [page] = parseResultMarkdown(['$$\\begin{aligned}', 'a &= b \\\\', 'c &= d', '\\end{aligned}$$', 'Next.'].join('\n'))
    expect(bare(page?.blocks)).toEqual([
      { kind: 'math', text: '\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}' },
      { kind: 'p', text: 'Next.' },
    ])
  })

  it('leaves a line with trailing prose after the formula as prose', () => {
    const [page] = parseResultMarkdown(['$$x$$ where x is small', '', 'Next.'].join('\n'))
    expect(page?.blocks.map((block) => block.kind)).toEqual(['p', 'p'])
  })
})

describe('a table of contents', () => {
  it('keeps one row per line and splits the leader from the page number', () => {
    const [page] = parseResultMarkdown(
      [
        '**1 Introduction** **13**',
        '1.1 Sequential decision making . . . . . . . 13',
        '1.2 Canonical models.....17',
        'Preface . . . . . iv',
      ].join('\n'),
    )
    expect(bare(page?.blocks)).toEqual([
      {
        kind: 'toc',
        entries: [
          { title: '**1 Introduction**', leader: ' ', page: '**13**' },
          { title: '1.1 Sequential decision making', leader: ' . . . . . . . ', page: '13' },
          { title: '1.2 Canonical models', leader: '.....', page: '17' },
          { title: 'Preface', leader: ' . . . . . ', page: 'iv' },
        ],
      },
    ])
  })

  it('rebuilds every source line from its entry', () => {
    const lines = ['**2 Value-based RL** **31**', '2.1 Basic concepts . . . . . . 31', 'Part one']
    const [page] = parseResultMarkdown(lines.join('\n'))
    const block = page?.blocks[0]
    expect(block?.kind).toBe('toc')
    if (block?.kind === 'toc') {
      expect(block.entries.map((entry) => entry.title + entry.leader + (entry.page ?? ''))).toEqual(lines)
      expect(block.entries[2]).toEqual({ title: 'Part one', leader: '', page: null })
    }
  })

  it('leaves wrapped prose a paragraph, trailing numbers and ellipses included', () => {
    const [page] = parseResultMarkdown(['The year was', '1984 and so on... and then', 'chapter 7'].join('\n'))
    expect(bare(page?.blocks)).toEqual([{ kind: 'p', text: 'The year was 1984 and so on... and then chapter 7' }])
  })
})


describe('serializeDocument', () => {
  /** One of every shape the parser knows, in the layout the pipeline assembles. */
  const document = [
    '<!-- page: 1 -->',
    '## 1.1 Sequential decision making',
    '',
    'An agent interacts with an environment over a sequence of discrete',
    'time steps, observing a **state** and a reward $r_t$.',
    '',
    '- First item',
    '  wrapped onto a second line',
    '- Second item',
    '',
    '1. Alpha',
    '2) Beta',
    '',
    '| Policy | Return |',
    '| --- | --- |',
    '| Random \\| noise | 1.2 |',
    '',
    'Region | Revenue',
    '--- | ---',
    'APAC | 1,204',
    '',
    '$$',
    '\\begin{aligned}',
    'E &= mc^2',
    '\\end{aligned}',
    '$$',
    '',
    '$$G_t = r_t + \\gamma r_{t+1} \\tag{1.6}$$',
    '',
    '```python',
    'def f(x):',
    '    return x + 1',
    '```',
    '',
    '![fig1](sightread://p1/120,60,480,940)',
    'Figure 1. The agent-environment loop.',
    '',
    '<!-- page: 2 -->',
    '### Contents',
    '',
    '**1 Introduction** **13**',
    '1.1 Sequential decision making . . . . . . . 13',
    '1.2 Canonical models.....17',
    '',
    '<!-- page: 3 -->',
  ].join('\n')

  it('keeps every block source byte for byte', () => {
    const pages = parseResultMarkdown(document)
    expect(pages.map((page) => page.blocks.map((block) => block.kind))).toEqual([
      ['h2', 'p', 'list', 'list', 'table', 'table', 'math', 'math', 'code', 'fig'],
      ['h3', 'toc'],
      [],
    ])
    expect(pages[0]!.blocks[2]!.src).toBe('- First item\n  wrapped onto a second line\n- Second item')
    expect(pages[0]!.blocks[6]!.src).toBe('$$\n\\begin{aligned}\nE &= mc^2\n\\end{aligned}\n$$')
    expect(pages[0]!.blocks[8]!.src).toBe('```python\ndef f(x):\n    return x + 1\n```')
    expect(pages[0]!.blocks[9]!.src).toBe(
      '![fig1](sightread://p1/120,60,480,940)\nFigure 1. The agent-environment loop.',
    )
  })

  it('is byte-stable through parse and serialize', () => {
    expect(serializeDocument(parseResultMarkdown(document))).toBe(document)
  })

  it('is stable again on a second round trip', () => {
    const once = serializeDocument(parseResultMarkdown(document))
    expect(serializeDocument(parseResultMarkdown(once))).toBe(once)
  })

  it('writes an empty page as its marker alone', () => {
    expect(serializeDocument([{ page: 4, blocks: [] }])).toBe('<!-- page: 4 -->')
  })

  it('leaves a document with no marker without one', () => {
    const written = ['# Title', '', 'Body.'].join('\n')
    expect(serializeDocument(parseResultMarkdown(written))).toBe(written)
  })

  it('folds content ahead of a page 1 marker under that marker', () => {
    const pages = parseResultMarkdown(['# Title', '<!-- page: 1 -->', 'Body.'].join('\n'))
    expect(serializeDocument(pages)).toBe('<!-- page: 1 -->\n# Title\n\nBody.')
  })

  it('keeps content ahead of a later page unmarked, and drops it once it is empty', () => {
    const doc = ['Intro.', '', '<!-- page: 2 -->', 'Body.'].join('\n')
    const pages = parseResultMarkdown(doc)
    expect(serializeDocument(pages)).toBe(doc)
    expect(serializeDocument([{ ...pages[0]!, blocks: [] }, pages[1]!])).toBe('<!-- page: 2 -->\nBody.')
  })
})

describe('appendBlockSource', () => {
  it('gives an emptied page its text back as a new block', () => {
    const emptied = ['<!-- page: 1 -->', '', '<!-- page: 2 -->', 'Second page.'].join('\n')
    expect(appendBlockSource(emptied, 0, 'Back again.')).toBe(
      ['<!-- page: 1 -->', 'Back again.', '', '<!-- page: 2 -->', 'Second page.'].join('\n'),
    )
  })

  it('starts a document with no page at all without a marker', () => {
    expect(appendBlockSource('', 0, 'First words.')).toBe('First words.')
    expect(appendBlockSource('First words.', 0, 'More.')).toBe('First words.\n\nMore.')
    expect(parseResultMarkdown(appendBlockSource('', 0, 'First words.'))[0]?.blocks[0]?.kind).toBe('p')
  })
})

describe('replaceBlockSource', () => {
  const document = [
    '<!-- page: 1 -->',
    '## Heading',
    '',
    'First paragraph.',
    '',
    '| A | B |',
    '| --- | --- |',
    '| 1 | 2 |',
    '',
    '<!-- page: 2 -->',
    'Second page.',
  ].join('\n')

  it('changes only the block that was edited', () => {
    const edited = replaceBlockSource(document, 0, 1, 'First paragraph, revised.')
    expect(edited).toBe(document.replace('First paragraph.', 'First paragraph, revised.'))
  })

  it('lets one block become two', () => {
    const edited = replaceBlockSource(document, 1, 0, 'Second page.\n\nAnd a third paragraph.')
    const pages = parseResultMarkdown(edited)
    expect(bare(pages[1]!.blocks)).toEqual([
      { kind: 'p', text: 'Second page.' },
      { kind: 'p', text: 'And a third paragraph.' },
    ])
    expect(serializeDocument(pages)).toBe(edited)
  })

  it('drops a block whose source was emptied', () => {
    const edited = replaceBlockSource(document, 0, 2, '   ')
    expect(edited).toBe(
      ['<!-- page: 1 -->', '## Heading', '', 'First paragraph.', '', '<!-- page: 2 -->', 'Second page.'].join('\n'),
    )
  })

  it('leaves the document alone for a block that does not exist', () => {
    expect(replaceBlockSource(document, 5, 0, 'x')).toBe(document)
  })
})

describe('comments', () => {
  it('keeps an HTML comment that is not a page marker through a block edit', () => {
    const source = '<!-- page: 1 -->\n\nFirst.\n\n<!-- reviewer: check this -->\n\nSecond.'
    const pages = parseResultMarkdown(source)
    expect(pages[0]?.blocks.map((block) => block.kind)).toEqual(['p', 'comment', 'p'])
    const edited = replaceBlockSource(source, 0, 0, 'First, edited.')
    expect(edited).toBe('<!-- page: 1 -->\nFirst, edited.\n\n<!-- reviewer: check this -->\n\nSecond.')
  })
})

describe('section links', () => {
  const link = '[3.2 Attention](#32-attention)'

  it('reads a link to a section only when asked to', () => {
    expect(parseInline(`See ${link}.`)).toEqual([{ kind: 'text', text: `See ${link}.` }])
    expect(parseInline(`See ${link}.`, { cites: true })).toEqual([
      { kind: 'text', text: 'See ' },
      {
        kind: 'cite',
        segments: [{ kind: 'text', text: '3.2 Attention' }],
        label: '3.2 Attention',
        anchor: '32-attention',
        src: link,
      },
      { kind: 'text', text: '.' },
    ])
  })

  it('reads the document’s own file name before the anchor', () => {
    expect(parseInline('[Results](doc.md#4-results)', { cites: true })).toMatchObject([
      { kind: 'cite', label: 'Results', anchor: '4-results', src: '[Results](doc.md#4-results)' },
    ])
  })

  it('leaves every other link the text it was', () => {
    for (const text of [
      '[site](https://example.com)',
      '[notes](notes.md#x)',
      '[spaced](#a b)',
      '[ ](#blank)',
      '[paren](#a(b))',
    ]) {
      expect(parseInline(text, { cites: true }).some((segment) => segment.kind === 'cite')).toBe(false)
    }
  })

  it('draws emphasis and math inside the link text, and around the link', () => {
    const [cite] = parseInline('[**3.1** The $\\epsilon$-greedy policy](#31-the-epsilon-greedy-policy)', { cites: true })
    expect(cite).toMatchObject({
      kind: 'cite',
      label: '3.1 The \\epsilon-greedy policy',
      segments: [
        { kind: 'strong', segments: [{ kind: 'text', text: '3.1' }], src: '**3.1**' },
        { kind: 'text', text: ' The ' },
        { kind: 'math', tex: '\\epsilon' },
        { kind: 'text', text: '-greedy policy' },
      ],
    })
    expect(parseInline(`**${link}**`, { cites: true })).toMatchObject([
      { kind: 'strong', segments: [{ kind: 'cite', label: '3.2 Attention' }] },
    ])
  })

  it('keeps an escaped bracket, and a link inside code, literal', () => {
    expect(parseInline(`\\${link}`, { cites: true })).toEqual([{ kind: 'text', text: link }])
    expect(parseInline(`\`${link}\` costs $5 and $x$`, { cites: true })).toMatchObject([
      { kind: 'code', text: link },
      { kind: 'text', text: ' costs $5 and ' },
      { kind: 'math', tex: 'x' },
    ])
    expect(parseInline('[a \\] b](#a-b)', { cites: true })).toMatchObject([{ kind: 'cite', label: 'a ] b' }])
  })
})

describe('documentOutline and findHeading', () => {
  const pages = parseResultMarkdown(
    [
      '<!-- page: 1 -->',
      '# Attention Is All You Need',
      '## 3.2 Attention',
      'Text.',
      '### 3.2.1 Scaled **Dot-Product** Attention',
      '<!-- page: 2 -->',
      '## Notes',
      '## Notes',
    ].join('\n'),
  )
  const outline = documentOutline(pages)

  it('lists every heading with its place, its text and its GitHub anchor', () => {
    expect(outline.map((entry) => [entry.page, entry.index, entry.level, entry.plain, entry.slug])).toEqual([
      [0, 0, 1, 'Attention Is All You Need', 'attention-is-all-you-need'],
      [0, 1, 2, '3.2 Attention', '32-attention'],
      [0, 3, 3, '3.2.1 Scaled Dot-Product Attention', '321-scaled-dot-product-attention'],
      [1, 0, 2, 'Notes', 'notes'],
      [1, 1, 2, 'Notes', 'notes-1'],
    ])
    expect(headingSlug('3.2.2 Multi-Head Attention')).toBe('322-multi-head-attention')
    expect(headingSlug('2.1 注意力（機制）')).toBe('21-注意力機制')
  })

  it('matches a link by its text first, markdown, spacing and case ignored', () => {
    expect(findHeading(outline, '3.2.1  scaled dot-product ATTENTION', 'nothing')).toMatchObject({ index: 3 })
    expect(findHeading(outline, inlinePlain('3.2 *Attention*'), '')).toMatchObject({ index: 1 })
  })

  it('falls back to the anchor, and finds nothing for a heading that is gone', () => {
    expect(findHeading(outline, 'Attention (renamed)', '32-attention')).toMatchObject({ index: 1 })
    expect(findHeading(outline, 'Notes again', 'notes-1')).toMatchObject({ page: 1, index: 1 })
    expect(findHeading(outline, 'Second notes', 'Notes')).toMatchObject({ page: 1, index: 0 })
    expect(findHeading(outline, 'Results', '4-results')).toBeNull()
    expect(findHeading(outline, 'Results', '')).toBeNull()
  })
})

describe('sectionSource', () => {
  const pages = parseResultMarkdown(
    [
      '<!-- page: 1 -->',
      '## 3 Model',
      'Intro.',
      '### 3.1 Encoder',
      'Six layers.',
      '<!-- page: 2 -->',
      'Continued.',
      '<!-- a reviewer note -->',
      '### 3.2 Attention',
      'Heads.',
      '# Appendix',
      'More.',
    ].join('\n'),
  )

  it('runs to the next heading of the same level, across a page marker', () => {
    expect(sectionSource(pages, 0, 2)).toBe('### 3.1 Encoder\n\nSix layers.\n\nContinued.')
  })

  it('takes its subsections with it and stops at a higher level', () => {
    expect(sectionSource(pages, 0, 0)).toBe(
      ['## 3 Model', 'Intro.', '### 3.1 Encoder', 'Six layers.', 'Continued.', '### 3.2 Attention', 'Heads.'].join('\n\n'),
    )
  })

  it('runs to the end of the document after the last heading, and is null off a heading', () => {
    expect(sectionSource(pages, 1, 4)).toBe('# Appendix\n\nMore.')
    expect(sectionSource(pages, 0, 1)).toBeNull()
    expect(sectionSource(pages, 5, 0)).toBeNull()
  })
})

describe('quotes', () => {
  it('reads a run of > lines as one quote whose content is blocks of its own', () => {
    const [page] = parseResultMarkdown(
      ['<!-- page: 1 -->', '> ## Said', '> First line', '>second line', '>', '> - a', '> - b', 'After.'].join('\n'),
    )
    const [quote, after] = page!.blocks
    expect(quote?.kind).toBe('quote')
    expect(quote?.src).toBe('> ## Said\n> First line\n>second line\n>\n> - a\n> - b')
    expect(quote?.kind === 'quote' && bare(quote.blocks)).toEqual([
      { kind: 'h2', text: 'Said', level: 2 },
      { kind: 'p', text: 'First line second line' },
      { kind: 'list', ordered: false, items: ['a', 'b'] },
    ])
    expect(bare([after!])).toEqual([{ kind: 'p', text: 'After.' }])
  })

  it('is one level deep: a > inside a quote, a placeholder and a contents row are its text', () => {
    const [page] = parseResultMarkdown(
      ['> > nested', '>', '> ![fig1](sightread://p2/1,2,3,4)', '>', '> Intro . . . . 13'].join('\n'),
    )
    const quote = page!.blocks[0]
    expect(quote?.kind === 'quote' && bare(quote.blocks)).toEqual([
      { kind: 'p', text: '> nested' },
      { kind: 'p', text: '![fig1](sightread://p2/1,2,3,4)' },
      { kind: 'p', text: 'Intro . . . . 13' },
    ])
  })

  it('ends a paragraph, and two quotes a blank line apart stay two', () => {
    const [page] = parseResultMarkdown(['Prose', '> quoted', '', '> another'].join('\n'))
    expect(page!.blocks.map((block) => block.kind)).toEqual(['p', 'quote', 'quote'])
  })

  it('round-trips byte for byte, and its headings are not the document\'s sections', () => {
    const markdown = ['<!-- page: 1 -->', '## Real', '', '> ## Quoted heading', '> text', '', 'Body.'].join('\n')
    const pages = parseResultMarkdown(markdown)
    expect(serializeDocument(pages)).toBe(markdown)
    expect(documentOutline(pages).map((entry) => entry.plain)).toEqual(['Real'])
    expect(sectionSource(pages, 0, 0)).toBe('## Real\n\n> ## Quoted heading\n> text\n\nBody.')
  })
})

describe('a figure keeps its own page', () => {
  it('reads the page its placeholder names, wherever the block sits', () => {
    const [page] = parseResultMarkdown(['<!-- page: 4 -->', '![fig2](sightread://p7/1,2,3,4)'].join('\n'))
    expect(page!.page).toBe(4)
    expect(page!.blocks[0]).toMatchObject({ kind: 'fig', page: 7 })
  })
})

describe('blockPlain and firstWords', () => {
  it('reads a block as the text it shows, formulas as their TeX', () => {
    const blocks = parseResultMarkdown(
      [
        '**Bold** and $x_1$ text',
        '',
        '- one',
        '- *two*',
        '',
        '| A | B |',
        '| --- | --- |',
        '| 1 | **2** |',
        '',
        '![fig1](sightread://p1/0,0,1,1)',
        'Figure 1: *curves*',
        '',
        '> quoted $y$',
      ].join('\n'),
    ).flatMap((page) => page.blocks)
    expect(blocks.map(blockPlain)).toEqual(['Bold and x_1 text', 'one two', 'A B 1 2', 'Figure 1: curves', 'quoted y'])
  })

  it('cuts to eight words, then to sixty characters', () => {
    expect(firstWords('one two three four five six seven eight nine ten')).toBe('one two three four five six seven eight')
    expect(firstWords('注意力機制'.repeat(20))).toBe('注意力機制'.repeat(12))
  })
})

describe('moveBlocks', () => {
  it('moves blocks in a document with no marker without adding one', () => {
    const moved = moveBlocks(['one', '', 'two'].join('\n'), { from: { page: 0, index: 1 }, to: { page: 0, index: 1 } }, { page: 0, index: 0 }, 'before')
    expect(moved?.markdown).toBe(['two', '', 'one'].join('\n'))
  })

  const doc = [
    '<!-- page: 1 -->',
    '## A',
    '',
    'one',
    '',
    '<!-- note on one -->',
    '',
    'two',
    '',
    '<!-- page: 2 -->',
    'three',
    '',
    '![fig1](sightread://p2/1,2,3,4)',
  ].join('\n')

  it('moves a run before another block, its comment with it, everything else byte for byte', () => {
    const moved = moveBlocks(doc, { from: { page: 0, index: 1 }, to: { page: 0, index: 1 } }, { page: 1, index: 1 }, 'before')
    expect(moved?.markdown).toBe(
      ['<!-- page: 1 -->', '## A', '', 'two', '', '<!-- page: 2 -->', 'three', '', 'one', '', '<!-- note on one -->', '', '![fig1](sightread://p2/1,2,3,4)'].join('\n'),
    )
    expect(moved?.first).toEqual({ page: 1, index: 1 })
    expect(moved?.last).toEqual({ page: 1, index: 2 })
  })

  it('crosses page markers, which stay where they were, and a figure keeps its page', () => {
    const moved = moveBlocks(doc, { from: { page: 1, index: 0 }, to: { page: 1, index: 1 } }, { page: 0, index: 0 }, 'before')
    const pages = parseResultMarkdown(moved!.markdown)
    expect(pages.map((page) => page.page)).toEqual([1, 2])
    expect(pages[0]!.blocks.map((block) => block.kind)).toEqual(['p', 'fig', 'h2', 'p', 'comment', 'p'])
    expect(pages[0]!.blocks[1]).toMatchObject({ kind: 'fig', page: 2 })
    expect(pages[1]!.blocks).toEqual([])
    expect(moved!.markdown.endsWith('<!-- page: 2 -->')).toBe(true)
  })

  it('lands after a target past the comments it carries', () => {
    const moved = moveBlocks(doc, { from: { page: 0, index: 0 }, to: { page: 0, index: 0 } }, { page: 0, index: 1 }, 'after')
    expect(parseResultMarkdown(moved!.markdown)[0]!.blocks.map((block) => block.src)).toEqual([
      'one',
      '<!-- note on one -->',
      '## A',
      'two',
    ])
  })

  it('does nothing for a target inside the run or a move that changes nothing', () => {
    expect(moveBlocks(doc, { from: { page: 0, index: 0 }, to: { page: 0, index: 1 } }, { page: 0, index: 1 }, 'after')).toBeNull()
    expect(moveBlocks(doc, { from: { page: 0, index: 0 }, to: { page: 0, index: 0 } }, { page: 0, index: 1 }, 'before')).toBeNull()
    expect(moveBlocks(doc, { from: { page: 0, index: 9 }, to: { page: 0, index: 9 } }, { page: 0, index: 1 }, 'before')).toBeNull()
  })
})

describe('block anchors', () => {
  const markdown = [
    '<!-- page: 1 -->',
    'Before any heading, a preface.',
    '',
    '## 3.2 Attention',
    '',
    'An attention function can be described as mapping a query and a set of pairs.',
    '',
    '![fig1](sightread://p1/0,0,1,1)',
    '',
    '<!-- page: 2 -->',
    '## Exercises',
    '',
    'Same words here.',
    '',
    '## Exercises',
    '',
    'Same words here.',
  ].join('\n')
  const pages = parseResultMarkdown(markdown)

  it('names a heading by its slug and any other block by its section and first words', () => {
    expect(blockAnchor(pages, { page: 0, index: 1 })).toBe('32-attention')
    expect(blockAnchor(pages, { page: 0, index: 2 })).toBe('32-attention~An attention function can be described as mapping')
    expect(blockAnchor(pages, { page: 0, index: 0 })).toBe('~Before any heading, a preface.')
    expect(blockAnchor(pages, { page: 0, index: 3 })).toBe('32-attention')
    expect(blockAnchor(pages, { page: 1, index: 3 })).toBe('exercises-1~Same words here.')
  })

  it('resolves in its section first, then anywhere, then its heading, then the top', () => {
    expect(resolveAnchor(pages, 'exercises-1~Same words here.')).toEqual({ page: 1, index: 3 })
    expect(resolveAnchor(pages, 'exercises~Same   words here.')).toEqual({ page: 1, index: 1 })
    expect(resolveAnchor(pages, 'missing~An attention function')).toEqual({ page: 0, index: 2 })
    expect(resolveAnchor(pages, '32-attention~Words no block has')).toEqual({ page: 0, index: 1 })
    expect(resolveAnchor(pages, '~Before any')).toEqual({ page: 0, index: 0 })
    expect(resolveAnchor(pages, 'gone~Nothing')).toBeNull()
    expect(resolveAnchor(pages, '')).toBeNull()
  })

  it('reads an anchor back from the escapes an address gave it', () => {
    expect(decodeBlockAnchor('32-attention~An+attention+function')).toBe('32-attention~An attention function')
    expect(decodeBlockAnchor('a~Let%20%28x%29%20be')).toBe('a~Let (x) be')
    expect(decodeBlockAnchor('a~100%')).toBe('a~100%')
    expect(resolveAnchor(pages, decodeBlockAnchor('32-attention~An+attention+function'))).toEqual({ page: 0, index: 2 })
  })

  it('survives the block moving to another section', () => {
    const moved = moveBlocks(markdown, { from: { page: 0, index: 2 }, to: { page: 0, index: 2 } }, { page: 1, index: 3 }, 'after')
    const after = parseResultMarkdown(moved!.markdown)
    expect(resolveAnchor(after, '32-attention~An attention function can be described as mapping')).toEqual({ page: 1, index: 4 })
  })
})

describe('inserted images', () => {
  it('reads only a stored image name as an image, with its caption line', () => {
    const blocks = parseResultMarkdown(
      [
        '![diagram](figures/img-3f9a0c1d2e4b5a6c.png)',
        'The pipeline, end to end',
        '',
        '![photo](figures/img-3f9a0c1d2e4b5a6c.jpg)',
        '',
        '![remote](https://example.com/a.png)',
        '',
        '![short](figures/img-3f9a.png)',
        '',
        '![other](figures/p1_0_0_1_1.png)',
      ].join('\n'),
    )[0]!.blocks
    expect(bare(blocks)).toEqual([
      { kind: 'img', alt: 'diagram', name: 'img-3f9a0c1d2e4b5a6c.png', caption: 'The pipeline, end to end' },
      { kind: 'img', alt: 'photo', name: 'img-3f9a0c1d2e4b5a6c.jpg', caption: null },
      { kind: 'p', text: '![remote](https://example.com/a.png)' },
      { kind: 'p', text: '![short](figures/img-3f9a.png)' },
      { kind: 'p', text: '![other](figures/p1_0_0_1_1.png)' },
    ])
    expect(blocks[0]!.src).toBe('![diagram](figures/img-3f9a0c1d2e4b5a6c.png)\nThe pipeline, end to end')
    expect(blockPlain(blocks[0]!)).toBe('The pipeline, end to end')
  })

  it('is text inside a quote, as a figure is', () => {
    const [quote] = parseResultMarkdown('> ![a](figures/img-3f9a0c1d2e4b5a6c.png)')[0]!.blocks
    expect(quote?.kind === 'quote' && quote.blocks[0]?.kind).toBe('p')
  })

  it('writes the line from the file name, nothing in it able to end the brackets', () => {
    expect(imageLine('Screen Shot 2026-10-21.png', 'figures/img-0123456789abcdef.png')).toBe(
      '![Screen Shot 2026-10-21](figures/img-0123456789abcdef.png)',
    )
    expect(imageLine('a]b[c\nd.jpeg', 'figures/img-0123456789abcdef.jpg')).toBe('![a b c d](figures/img-0123456789abcdef.jpg)')
    expect(imageLine('.png', 'figures/img-0123456789abcdef.png')).toBe('![image](figures/img-0123456789abcdef.png)')
  })
})

describe('insertBlocks', () => {
  const line = '![shot](figures/img-0123456789abcdef.png)'

  it('lands before or after a block, past the comments it carries', () => {
    const doc = ['<!-- page: 1 -->', 'one', '', '<!-- note -->', '', 'two'].join('\n')
    const after = insertBlocks(doc, { page: 0, index: 0 }, 'after', line)
    expect(after?.markdown).toBe(['<!-- page: 1 -->', 'one', '', '<!-- note -->', '', line, '', 'two'].join('\n'))
    expect(after?.first).toEqual({ page: 0, index: 2 })
    const before = insertBlocks(doc, { page: 0, index: 0 }, 'before', `${line}\n\nA caption-less paragraph.`)
    expect(before?.markdown).toBe(['<!-- page: 1 -->', line, '', 'A caption-less paragraph.', '', 'one', '', '<!-- note -->', '', 'two'].join('\n'))
    expect([before?.first, before?.last]).toEqual([
      { page: 0, index: 0 },
      { page: 0, index: 1 },
    ])
  })

  it('goes at the end without a target, and makes an empty document its first block', () => {
    expect(insertBlocks('', null, 'after', line)).toEqual({ markdown: line, first: { page: 0, index: 0 }, last: { page: 0, index: 0 } })
    expect(insertBlocks('one', null, 'after', line)?.markdown).toBe(`one\n\n${line}`)
  })

  it('leaves page markers out of what it inserts, and inserts nothing for no block', () => {
    expect(insertBlocks('one', { page: 0, index: 0 }, 'after', '<!-- page: 4 -->\ntwo')?.markdown).toBe('one\n\ntwo')
    expect(insertBlocks('one', null, 'after', '\n\n')).toBeNull()
  })

  it("goes at the end of the target's page when the target is past its last block", () => {
    const doc = ['<!-- page: 1 -->', 'one', '', '<!-- page: 2 -->', 'two'].join('\n')
    expect(insertBlocks(doc, { page: 0, index: 1 }, 'before', line)?.markdown).toBe(
      ['<!-- page: 1 -->', 'one', '', line, '', '<!-- page: 2 -->', 'two'].join('\n'),
    )
    const emptied = ['<!-- page: 1 -->', 'one', '', '<!-- page: 2 -->', '', '<!-- page: 3 -->', 'three'].join('\n')
    expect(insertBlocks(emptied, { page: 1, index: 0 }, 'before', line)?.markdown).toBe(
      ['<!-- page: 1 -->', 'one', '', '<!-- page: 2 -->', line, '', '<!-- page: 3 -->', 'three'].join('\n'),
    )
  })
})

describe('placeNear and findPlace', () => {
  const doc = ['<!-- page: 1 -->', 'One.', '', 'Two.', '', '<!-- page: 2 -->', 'Three.'].join('\n')

  it('holds a place by its neighbours and finds it again after edits', () => {
    const near = placeNear(doc, { page: 0, index: 1 }, 'after')
    expect(near).toEqual({ after: 'Two.', before: 'Three.', gap: 2, prefer: 'after' })
    // A block added above moves the place: its text finds it again.
    const edited = doc.replace('One.', 'One.\n\nOne and a half.')
    expect(findPlace(edited, near)).toEqual({ target: { page: 0, index: 2 }, place: 'after' })
    // The block it followed is gone: the one it preceded holds it.
    expect(findPlace(doc.replace('Two.', ''), near)).toEqual({ target: { page: 1, index: 0 }, place: 'before' })
    // Neither is there: the end of the document.
    expect(findPlace('Else.', near)).toEqual({ target: null, place: 'after' })
  })

  it('takes the nearest of repeated text, and the end of the document without a target', () => {
    const repeated = ['Same.', 'Other.', 'Same.', 'Last.'].join('\n\n')
    const near = placeNear(repeated, { page: 0, index: 2 }, 'after')
    expect(near).toEqual({ after: 'Same.', before: 'Last.', gap: 3, prefer: 'after' })
    expect(findPlace(repeated, near)).toEqual({ target: { page: 0, index: 2 }, place: 'after' })
    expect(findPlace(repeated, { after: null, before: 'Same.', gap: 0 })).toEqual({ target: { page: 0, index: 0 }, place: 'before' })
    expect(placeNear('One.', null, 'after')).toEqual({ after: 'One.', before: null, gap: 1, prefer: 'after' })
    expect(placeNear('', null, 'after')).toEqual({ after: null, before: null, gap: 0, prefer: 'after' })
  })

  it('keeps a place at a page edge on its page', () => {
    // Before the first block of page 2: not after the last block of page 1.
    const opening = placeNear(doc, { page: 1, index: 0 }, 'before')
    expect(opening).toEqual({ after: 'Two.', before: 'Three.', gap: 2, prefer: 'before' })
    expect(findPlace(doc, opening)).toEqual({ target: { page: 1, index: 0 }, place: 'before' })
    // Past a page's last block, as a draft emptied away leaves it: the end of that page.
    const closing = placeNear(doc, { page: 0, index: 2 }, 'before')
    expect(closing).toEqual({ after: 'Two.', before: 'Three.', gap: 2, prefer: 'after' })
    expect(findPlace(doc, closing)).toEqual({ target: { page: 0, index: 1 }, place: 'after' })
  })

  it('compares blocks without the spacing at their ends', () => {
    expect(findPlace('Text.  \n\nNext.', { after: 'Text.', before: null, gap: 1 })).toEqual({ target: { page: 0, index: 0 }, place: 'after' })
  })
})
