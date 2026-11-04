import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'
import {
  SUMMARY_ORDER,
  addedLines,
  countKinds,
  diffFromInput,
  filesPath,
  firstCommand,
  groupRuns,
  ownFileDocument,
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
      document: 'a',
    })
    expect(readStep('str_replace_based_edit_tool', { command: 'str_replace', path: '/workspace/files/a/doc.md' })).toEqual({
      kind: 'edit',
      target: 'doc.md',
      document: 'a',
    })
    expect(readStep('str_replace_based_edit_tool', { command: 'create', path: 'x/notes.md' })).toEqual({
      kind: 'create',
      target: 'notes.md',
    })
  })

  it('says which document a doc.md or notes.md is, when the path says', () => {
    expect(ownFileDocument('/workspace/files/DL/iBOT.pdf/doc.md')).toBe('DL/iBOT.pdf')
    expect(ownFileDocument('/workspace/files/ML/MoCo.pdf/notes.md')).toBe('ML/MoCo.pdf')
    // Not a document's own file, or a path that does not say whose.
    expect(ownFileDocument('/workspace/files/DL/iBOT.pdf/figures/fig1.png')).toBeUndefined()
    expect(ownFileDocument('/workspace/files/doc.md')).toBeUndefined()
    expect(ownFileDocument('doc.md')).toBeUndefined()
    // A shell step's file, whole or relative to where it `cd`'d; a run names a program.
    expect(readStep('bash', { command: 'cat /workspace/files/DL/iBOT.pdf/doc.md' }).document).toBe('DL/iBOT.pdf')
    expect(readStep('bash', { command: 'cd "/workspace/files/ML/MoCo.pdf" && sed -n 1,40p notes.md' }).document).toBe(
      'ML/MoCo.pdf',
    )
    expect(readStep('bash', { command: 'grep -n loss /workspace/files/DL/iBOT.pdf/doc.md' }).document).toBe('DL/iBOT.pdf')
    expect(readStep('bash', { command: 'sed -n 1,40p doc.md' }).document).toBeUndefined()
    expect(readStep('bash', { command: 'cd /workspace/files/a && python3 /workspace/files/b/doc.md' }).document).toBeUndefined()
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
    // A retry sends the failed pages again: its own step, and only on a literal true.
    expect(readStep('convert_file', { path: 'Papers/paper.pdf', retry: true })).toEqual({ kind: 'retry', target: 'paper.pdf' })
    expect(readStep('convert_file', { path: 'Papers/paper.pdf', retry: 'yes' })).toEqual({ kind: 'convert', target: 'paper.pdf' })
    expect(
      readStep('insert_image', { image: '/workspace/.cache/loss.png', document: '/workspace/files/Papers/attention/notes.md' }),
    ).toEqual({ kind: 'image', target: 'loss.png', file: 'notes.md', document: 'Papers/attention' })
    expect(readStep('skill', { name: 'zh-summary', arguments: '' })).toEqual({ kind: 'skill', target: 'zh-summary' })
    expect(readStep('subagent', { name: 'verifier', task: 'x' })).toEqual({ kind: 'subagent', target: 'verifier' })
    expect(readStep('bash', { restart: true })).toEqual({ kind: 'restart', target: '' })
    expect(readStep('mystery', {})).toEqual({ kind: 'tool', target: 'mystery' })
  })
})

describe('the library tools', () => {
  it('names an item by its Files path, whichever form the call used', () => {
    expect(filesPath('DL/DINO/iBOT.pdf')).toBe('DL/DINO/iBOT.pdf')
    expect(filesPath('/DL/DINO/')).toBe('DL/DINO')
    // A document's directory and its doc.md both name the document.
    expect(filesPath('/workspace/files/DL/DINO/iBOT.pdf/doc.md')).toBe('DL/DINO/iBOT.pdf')
    expect(filesPath('/workspace/files/DL/DINO/iBOT.pdf/')).toBe('DL/DINO/iBOT.pdf')
    // Only the workspace form drops a doc.md: a Files path ending in one is a file of that name.
    expect(filesPath('DL/doc.md')).toBe('DL/doc.md')
    expect(filesPath('/workspace/files/doc.md')).toBe('doc.md')
    expect(filesPath('/workspace/filesystem/a')).toBe('workspace/filesystem/a')
    for (const top of ['', '/', '/workspace/files', '/workspace/files/', undefined, 7]) {
      expect(filesPath(top)).toBe('')
    }
  })

  it('reads each tool as its own kind, never the tool’s name', () => {
    expect(readStep('list_files', { path: 'DL' })).toEqual({ kind: 'browse', target: 'DL' })
    expect(readStep('list_files', {})).toEqual({ kind: 'browse', target: '' })
    expect(readStep('list_files', { path: 'DL', recursive: true, trash: true })).toEqual({
      kind: 'browse',
      target: '',
      trash: true,
    })
    expect(readStep('new_folder', { path: 'DL/Transformer/' })).toEqual({ kind: 'folder', target: 'DL/Transformer' })
    expect(readStep('new_document', { path: '/workspace/files/DL/Reading list', text: '# x' })).toEqual({
      kind: 'newDocument',
      target: 'DL/Reading list',
    })
    expect(readStep('rename', { path: 'DL/scan_0412.pdf', name: 'ViT.pdf' })).toEqual({
      kind: 'rename',
      target: 'DL/scan_0412.pdf',
      name: 'ViT.pdf',
    })
  })

  it('names a batch of one by its path and a larger one by its count, with the destination', () => {
    expect(readStep('move', { paths: ['DL/iBOT.pdf'], to: 'DL/DINO' })).toEqual({
      kind: 'move',
      target: 'DL/iBOT.pdf',
      place: 'DL/DINO',
    })
    expect(readStep('move', { paths: ['a.pdf', '/workspace/files/b.pdf/doc.md'], to: '/' })).toEqual({
      kind: 'move',
      target: '',
      items: 2,
      place: '',
    })
    expect(readStep('copy', { paths: ['a.pdf', 'b.pdf', 'c.pdf'], to: 'Archive' })).toEqual({
      kind: 'duplicate',
      target: '',
      items: 3,
      place: 'Archive',
    })
    expect(readStep('trash', { paths: ['DL/old'] })).toEqual({ kind: 'trash', target: 'DL/old' })
    expect(readStep('put_back', { paths: ['DL/old', 'DL/x.pdf'] })).toEqual({ kind: 'putBack', target: '', items: 2 })
    // A call missing its list is a batch of nothing, not a crash.
    expect(readStep('trash', {})).toEqual({ kind: 'trash', target: '', items: 0 })
  })

  it('has every kind’s words in both catalogs, the Files page’s own in zh-TW', () => {
    for (const catalog of [en, zhTW]) {
      for (const kind of SUMMARY_ORDER) {
        expect(catalog.chat.steps).toHaveProperty(kind)
        expect(catalog.chat.summary).toHaveProperty(kind)
        expect(catalog.chat.doing).toHaveProperty(kind)
      }
    }
    expect(zhTW.chat.steps).toMatchObject({
      browse: '列出',
      folder: '新增資料夾',
      move: '移動',
      rename: '重新命名',
      duplicate: '建立副本',
      trash: '丟到垃圾桶',
      putBack: '放回原處',
      newDocument: '新增文件',
    })
    expect(zhTW.chat.steps.trash).toBe(zhTW.files.moveToTrash)
    expect(zhTW.chat.steps.putBack).toBe(zhTW.files.putBack)
    expect(zhTW.chat.steps.duplicate).toBe(zhTW.files.duplicate)
    expect(en.chat.steps.trash).toBe(en.files.moveToTrash)
    expect(en.chat.steps.putBack).toBe(en.files.putBack)
    expect(en.chat.steps.duplicate).toBe(en.files.duplicate)
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

  it('names what the library tools looked at before what they changed', () => {
    expect(countKinds(['move', 'read', 'folder', 'browse', 'move'])).toEqual([
      { kind: 'read', count: 1 },
      { kind: 'browse', count: 1 },
      { kind: 'folder', count: 1 },
      { kind: 'move', count: 2 },
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
