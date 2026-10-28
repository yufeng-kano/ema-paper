import { describe, expect, it } from 'vitest'
import {
  addedLines,
  countKinds,
  diffFromInput,
  firstCommand,
  groupRuns,
  parseDiff,
  readShell,
  readStep,
  resultText,
  shellWords,
  splitDuration,
} from './steps'

describe('shellWords', () => {
  it('splits on whitespace and drops the quotes', () => {
    expect(shellWords(`grep -n 'a b' "c \\"d\\"" e\\ f`)).toEqual(['grep', '-n', 'a b', 'c "d"', 'e f'])
  })

  it('keeps an empty quoted word', () => {
    expect(shellWords(`echo '' x`)).toEqual(['echo', '', 'x'])
  })
})

describe('firstCommand', () => {
  it('stops at the first unquoted pipe, list operator or newline', () => {
    expect(firstCommand('cat doc.md | head -5')).toBe('cat doc.md ')
    expect(firstCommand("grep 'a|b' doc.md && ls")).toBe("grep 'a|b' doc.md ")
    expect(firstCommand('ls\nwc -l x')).toBe('ls')
  })
})

describe('readShell', () => {
  it('drops the leading cd into the document and reads a sed print as a read', () => {
    expect(readShell('cd "/workspace/files/UniVLA_2505.06111.pdf"; sed -n 16,60p doc.md')).toEqual({
      kind: 'read',
      target: 'doc.md',
      lines: { from: 16, to: 60 },
      cwd: '/workspace/files/UniVLA_2505.06111.pdf',
    })
  })

  it('reads a quoted sed range, one line, and a range to the end', () => {
    expect(readShell("sed -n '225,410p' doc.md").lines).toEqual({ from: 225, to: 410 })
    expect(readShell('sed -n 7p doc.md').lines).toEqual({ from: 7, to: 7 })
    expect(readShell("cd x && sed -n '600,$p' doc.md").lines).toEqual({ from: 600, to: null })
  })

  it('reads sed -i as an edit and any other sed as a run', () => {
    expect(readShell("sed -i 's/a/b/' doc.md")).toMatchObject({ kind: 'edit', target: 'doc.md' })
    expect(readShell("sed 's/a/b/' doc.md")).toMatchObject({ kind: 'run', target: 'sed doc.md' })
  })

  it('reads head and tail with their counts', () => {
    expect(readShell('head -n 50 doc.md')).toMatchObject({ kind: 'read', target: 'doc.md', lines: { from: 1, to: 50 } })
    expect(readShell('head -20 doc.md').lines).toEqual({ from: 1, to: 20 })
    expect(readShell('tail -n 30 notes.md')).toMatchObject({ kind: 'read', target: 'notes.md', last: 30 })
    expect(readShell('tail -n +100 doc.md').lines).toEqual({ from: 100, to: null })
  })

  it('reads cat as a read of its first file', () => {
    expect(readShell('cat -n doc.md | head')).toMatchObject({ kind: 'read', target: 'doc.md' })
  })

  it('reads grep and rg as a search, the pattern with its alternation unescaped', () => {
    expect(readShell("grep -n 'METHODOLOGY\\|EXPERIMENTS' doc.md")).toMatchObject({
      kind: 'search',
      target: 'METHODOLOGY|EXPERIMENTS',
      file: 'doc.md',
    })
    expect(readShell('rg -n -g "*.md" figure')).toMatchObject({ kind: 'search', target: 'figure', file: undefined })
    expect(readShell('grep -e "-x" doc.md')).toMatchObject({ kind: 'search', target: '-x', file: 'doc.md' })
  })

  it('reads ls, find and wc', () => {
    expect(readShell('ls -la figures')).toMatchObject({ kind: 'list', target: 'figures' })
    expect(readShell('ls')).toMatchObject({ kind: 'list', target: '.' })
    expect(readShell('wc -l doc.md')).toMatchObject({ kind: 'count', target: 'doc.md' })
  })

  it('names the program of anything else, past assignments and redirections', () => {
    expect(readShell('LC_ALL=C python3 analyze.py > out.txt')).toMatchObject({ kind: 'run', target: 'python3 analyze.py' })
    expect(readShell("python3 -c 'print(1)'")).toMatchObject({ kind: 'run', target: 'python3' })
    expect(readShell("python3 - <<'EOF'\nprint(1)\nEOF")).toMatchObject({ kind: 'run', target: 'python3' })
    expect(readShell('/usr/bin/pdftotext source.pdf -')).toMatchObject({ kind: 'run', target: 'pdftotext source.pdf' })
  })
})

describe('readStep', () => {
  it('reads the editor by its command, the file by its name', () => {
    expect(readStep('str_replace_based_edit_tool', { command: 'view', path: '/workspace/files/a/doc.md', view_range: [10, -1] })).toEqual({
      kind: 'read',
      target: 'doc.md',
      lines: { from: 10, to: null },
    })
    expect(readStep('str_replace_based_edit_tool', { command: 'str_replace', path: '/workspace/files/a/doc.md' })).toEqual({
      kind: 'edit',
      target: 'doc.md',
    })
    expect(readStep('str_replace_based_edit_tool', { command: 'create', path: 'x/notes.md' })).toEqual({
      kind: 'create',
      target: 'notes.md',
    })
  })

  it('reads the page and figure views, skills, subagents, a restart and unknown tools', () => {
    expect(readStep('view_page', { page: 3 })).toEqual({ kind: 'page', target: '', page: 3 })
    expect(readStep('view_figure', { page: 5, bbox: [1, 2, 3, 4] })).toEqual({ kind: 'figure', target: '', page: 5 })
    expect(readStep('web_search', { query: 'attention is all you need' })).toEqual({
      kind: 'web',
      target: 'attention is all you need',
    })
    expect(readStep('search_papers', { query: 'transformers' })).toEqual({ kind: 'papers', target: 'transformers' })
    expect(readStep('read_url', { url: 'https://example.org/p' })).toEqual({ kind: 'fetch', target: 'https://example.org/p' })
    expect(readStep('download_file', { url: 'https://example.org/p.pdf', folder: 'Papers', name: 'p.pdf' })).toEqual({
      kind: 'download',
      target: 'p.pdf',
    })
    expect(readStep('download_file', { url: 'https://example.org/p.pdf', folder: '', name: '' })).toEqual({
      kind: 'download',
      target: 'https://example.org/p.pdf',
    })
    expect(readStep('convert_file', { path: 'Papers/2017/attention.pdf' })).toEqual({ kind: 'convert', target: 'attention.pdf' })
    expect(readStep('convert_file', { path: '/workspace/files/scan.png' })).toEqual({ kind: 'convert', target: 'scan.png' })
    expect(readStep('skill', { name: 'zh-summary', arguments: '' })).toEqual({ kind: 'skill', target: 'zh-summary' })
    expect(readStep('subagent', { name: 'verifier', task: 'x' })).toEqual({ kind: 'subagent', target: 'verifier' })
    expect(readStep('bash', { restart: true })).toEqual({ kind: 'restart', target: '' })
    expect(readStep('mystery', {})).toEqual({ kind: 'tool', target: 'mystery' })
  })
})

describe('groupRuns', () => {
  it('folds each run of steps into one group and leaves the rest in place', () => {
    const items = ['t1', 's1', 's2', 't2', 's3']
    const grouped = groupRuns(items, (item) => item.startsWith('s'), (run) => ({ run }))
    expect(grouped).toEqual(['t1', { run: ['s1', 's2'] }, 't2', { run: ['s3'] }])
  })

  it('returns nothing for nothing', () => {
    expect(groupRuns([], () => true, (run) => run)).toEqual([])
  })
})

describe('countKinds', () => {
  it('counts in the summary order and leaves out the kinds with none', () => {
    expect(countKinds(['edit', 'read', 'read', 'search', 'edit', 'read'])).toEqual([
      { kind: 'read', count: 3 },
      { kind: 'search', count: 1 },
      { kind: 'edit', count: 2 },
    ])
  })
})

describe('diffs', () => {
  it('drops the file header, keeps hunks, and counts added lines', () => {
    const lines = parseDiff('--- a/doc.md\n+++ b/doc.md\n@@ -1,2 +1,3 @@\n context\n-old\n+new\n+more\n')
    expect(lines).toEqual([
      { kind: 'hunk', text: '@@ -1,2 +1,3 @@' },
      { kind: 'context', text: 'context' },
      { kind: 'removed', text: 'old' },
      { kind: 'added', text: 'new' },
      { kind: 'added', text: 'more' },
    ])
    expect(addedLines(lines)).toBe(2)
  })

  it('implies a diff from a stored editor call', () => {
    expect(diffFromInput({ old_str: 'a', new_str: 'b\nc' })).toEqual([
      { kind: 'removed', text: 'a' },
      { kind: 'added', text: 'b' },
      { kind: 'added', text: 'c' },
    ])
    expect(diffFromInput({ file_text: 'x' })).toEqual([{ kind: 'added', text: 'x' }])
  })
})

describe('resultText', () => {
  it('reads a string, text blocks and an image marker', () => {
    expect(resultText('ok')).toBe('ok')
    expect(resultText([{ type: 'text', text: 'a' }, { type: 'image' }, { type: 'text', text: 'b' }])).toBe('a\n[image]\nb')
    expect(resultText(undefined)).toBe('')
  })
})

describe('splitDuration', () => {
  it('splits into whole minutes and the seconds left', () => {
    expect(splitDuration(108_400)).toEqual({ minutes: 1, seconds: 48 })
    expect(splitDuration(52_000)).toEqual({ minutes: 0, seconds: 52 })
    expect(splitDuration(-5)).toEqual({ minutes: 0, seconds: 0 })
  })
})
