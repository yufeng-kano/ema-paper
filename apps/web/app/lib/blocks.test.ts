import { describe, expect, it } from 'vitest'
import {
  UndoHistory,
  applySpans,
  blockName,
  currentTick,
  diffSpans,
  fitTicks,
  foldsOf,
  hashText,
  moveTarget,
  revertSpans,
  rowsOf,
  translatable,
  type NameText,
} from './blocks'
import { documentOutline, moveBlocks, parseResultMarkdown, placedBlocks } from './markdown'

const en: NameText = {
  formula: 'Formula',
  formulaTag: (tag) => `Formula (${tag})`,
  quoted: (words) => `“${words}”`,
  figure: (words) => `Figure “${words}”`,
  figureBare: 'Figure',
}

function blocks(markdown: string) {
  return parseResultMarkdown(markdown).flatMap((page) => page.blocks)
}

describe('blockName', () => {
  it('names a formula by its tag, anything else by its words in quotation marks', () => {
    const [tagged, plain, heading, prose, figure, bare] = blocks(
      [
        '$$x = 1 \\tag{1.6}$$',
        '',
        '$$y$$',
        '',
        '## 3.2 Attention',
        '',
        'An attention function can be described as mapping a query.',
        '',
        '![fig1](sightread://p1/0,0,1,1)',
        'Figure 1: *Learning* curves over training',
        '',
        '![fig2](sightread://p1/0,0,1,1)',
      ].join('\n'),
    )
    expect(blockName(tagged!, en)).toBe('Formula (1.6)')
    expect(blockName(plain!, en)).toBe('Formula')
    expect(blockName(heading!, en)).toBe('“3.2 Attention”')
    expect(blockName(prose!, en)).toBe('“An attention function…”')
    expect(blockName(figure!, en)).toBe('Figure “Figure 1: Learning…”')
    expect(blockName(bare!, en)).toBe('Figure')
  })

  it('cuts words without spaces by length', () => {
    const [block] = blocks('注意力函數可以描述為將一個查詢和一組鍵值對映射到輸出')
    expect(blockName(block!, { ...en, quoted: (words) => `「${words}」` })).toBe('「注意力函數可以描述為將一個查詢和一組鍵值對映射到…」')
  })
})

describe('translatable', () => {
  it('is prose and captions, never formulas, code, tables, contents or bare figures', () => {
    const kinds = blocks(
      ['Prose.', '', '- item', '', '> quoted', '', '$$x$$', '', '```\ncode\n```', '', '| a |\n| --- |\n| 1 |', '', '![f](sightread://p1/0,0,1,1)'].join('\n'),
    ).map(translatable)
    expect(kinds).toEqual([true, true, true, false, false, false, false])
  })
})

const doc = parseResultMarkdown(
  [
    '<!-- page: 1 -->',
    '## 1 Intro',
    '',
    'Para A.',
    '',
    '<!-- note -->',
    '',
    '### 1.1 Sub',
    '',
    'Para B.',
    '',
    '<!-- page: 2 -->',
    '## 2 Method',
    '',
    'Para C.',
  ].join('\n'),
)
const placed = placedBlocks(doc)
const outline = documentOutline(doc)

describe('folds', () => {
  it('hides a collapsed section to the next heading of its level, comments not counted', () => {
    const folds = foldsOf(placed, outline, new Set(['1-intro']))
    expect([...folds.hidden]).toEqual(['0-1', '0-2', '0-3', '0-4'])
    expect([...folds.counts]).toEqual([['0-0', 3]])
  })

  it('draws no fold for a collapsed heading inside another', () => {
    const folds = foldsOf(placed, outline, new Set(['1-intro', '11-sub']))
    expect([...folds.counts.keys()]).toEqual(['0-0'])
  })

  it('makes a collapsed section one row reaching to its last hidden block', () => {
    const rows = rowsOf(placed, foldsOf(placed, outline, new Set(['11-sub'])))
    expect(rows.map((row) => [row.key, row.first, row.last])).toEqual([
      ['0-0', 0, 0],
      ['0-1', 1, 1],
      ['0-3', 3, 4],
      ['1-0', 5, 5],
      ['1-1', 6, 6],
    ])
  })
})

describe('moveTarget', () => {
  const rows = rowsOf(placed, foldsOf(placed, outline, new Set(['11-sub'])))

  it('steps over one visible item, a collapsed section being one', () => {
    expect(moveTarget(rows, 1, 1, 'down')).toEqual({ target: 4, place: 'after' })
    expect(moveTarget(rows, 3, 3, 'up')).toEqual({ target: 3, place: 'before' })
    expect(moveTarget(rows, 0, 0, 'up')).toBeNull()
    expect(moveTarget(rows, 4, 4, 'down')).toBeNull()
  })

  it('lands a block moved down past a fold inside its section', () => {
    const markdown = [
      '<!-- page: 1 -->',
      'Lead.',
      '',
      '## Folded',
      '',
      'Inside.',
      '',
      '## Next',
    ].join('\n')
    const pages = parseResultMarkdown(markdown)
    const all = placedBlocks(pages)
    const lines = documentOutline(pages)
    const rows = rowsOf(all, foldsOf(all, lines, new Set(['folded'])))
    const move = moveTarget(rows, 0, 0, 'down')!
    const target = all[move.target]!
    const moved = moveBlocks(markdown, { from: { page: 0, index: 0 }, to: { page: 0, index: 0 } }, target, move.place)
    expect(moved!.markdown).toBe(['<!-- page: 1 -->', '## Folded', '', 'Inside.', '', 'Lead.', '', '## Next'].join('\n'))
  })
})

describe('fitTicks', () => {
  it('keeps every tick while they fit, closing the gap from 8px to 2px', () => {
    expect(fitTicks([1, 2, 3], 100)).toEqual({ gap: 8, ticks: [{ index: 0, long: true }, { index: 1, long: true }, { index: 2, long: false }] })
    expect(fitTicks([2, 3, 3, 3], 20)?.gap).toBe(4)
  })

  it('drops to the # and ## ticks, then to none', () => {
    expect(fitTicks([2, 3, 3, 3, 3, 2], 10)).toEqual({ gap: 6, ticks: [{ index: 0, long: true }, { index: 5, long: true }] })
    expect(fitTicks([2, 2, 2, 2, 2, 2], 10)).toBeNull()
  })

  it('marks the tick of the heading the current section falls under', () => {
    const fit = fitTicks([2, 3, 3, 3, 3, 2], 10)!
    expect(currentTick(fit, 3)).toBe(0)
    expect(currentTick(fit, 5)).toBe(5)
    expect(currentTick(fit, null)).toBeNull()
  })
})

describe('undo', () => {
  it('keeps one span for an edit and puts it back exactly', () => {
    const before = 'one\n\ntwo\n\nthree'
    const after = 'one\n\nthree'
    const spans = diffSpans(before, after)
    expect(spans).toHaveLength(1)
    expect(applySpans(before, spans)).toBe(after)
    expect(revertSpans(after, spans)).toBe(before)
  })

  it('keeps a move as two spans holding the run, not what it passed', () => {
    const passed = Array.from({ length: 40 }, (_, index) => `Paragraph ${index}.`).join('\n\n')
    const markdown = `<!-- page: 1 -->\nMoving block.\n\n${passed}\n\nEnd.`
    const moved = moveBlocks(markdown, { from: { page: 0, index: 0 }, to: { page: 0, index: 0 } }, { page: 0, index: 39 }, 'after')!
    const spans = diffSpans(markdown, moved.markdown)
    expect(spans).toHaveLength(2)
    expect(spans.every((span) => span.removed.length + span.inserted.length < 40)).toBe(true)
    expect(applySpans(markdown, spans)).toBe(moved.markdown)
    expect(revertSpans(moved.markdown, spans)).toBe(markdown)
    const up = moveBlocks(moved.markdown, { from: { page: 0, index: 39 }, to: { page: 0, index: 39 } }, { page: 0, index: 0 }, 'before')!
    const back = diffSpans(moved.markdown, up.markdown)
    expect(back.every((span) => span.removed.length + span.inserted.length < 40)).toBe(true)
    expect(applySpans(moved.markdown, back)).toBe(up.markdown)
  })

  it('undoes and redoes in order across files, removing a step whose file changed', () => {
    const files = { document: 'a\n\nb', notes: 'n' }
    const history = new UndoHistory()
    const act = (file: 'document' | 'notes', next: string) => {
      history.record(file, files[file], next)
      files[file] = next
    }
    act('document', 'b\n\na')
    act('notes', 'n\n\n> q')
    // Typed in the notes: the excerpt cannot be taken back, the move still can.
    files.notes = 'n\n\n> q typed'
    const undone = history.undo((file) => files[file])
    expect(undone?.file).toBe('document')
    expect(undone?.text).toBe('a\n\nb')
    files.document = undone!.text
    const redone = history.redo((file) => files[file])
    expect(redone?.text).toBe('b\n\na')
    files.document = redone!.text
    expect(history.undo((file) => files[file])?.file).toBe('document')
  })

  it('hashes by content and length', () => {
    expect(hashText('abc')).toBe(hashText('abc'))
    expect(hashText('abc')).not.toBe(hashText('abd'))
    expect(hashText('')).toMatch(/^0:/)
  })

  it('holds at most its limit of steps', () => {
    const history = new UndoHistory(2)
    let text = 'x'
    for (const next of ['xa', 'xab', 'xabc']) {
      history.record('document', text, next)
      text = next
    }
    expect(history.undo(() => text)?.text).toBe('xab')
    text = 'xab'
    expect(history.undo(() => text)?.text).toBe('xa')
    text = 'xa'
    expect(history.undo(() => text)).toBeNull()
  })
})
