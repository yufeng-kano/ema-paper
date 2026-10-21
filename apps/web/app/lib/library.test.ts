import { describe, expect, it } from 'vitest'
import type { LibraryDocument, LibraryFolder, UploadLimits } from './api'
import {
  acceptsUpload,
  canMoveFolder,
  childFolders,
  documentsIn,
  flattenTree,
  folderPath,
  isMarkdownUpload,
  isPending,
  parseProgress,
  subtreeIds,
} from './library'

function folder(id: number, name: string, parent_id: number | null = null): LibraryFolder {
  return { id, name, parent_id, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' }
}

function document(
  id: number,
  name: string,
  folder_id: number | null = null,
  overrides: Partial<LibraryDocument> = {},
): LibraryDocument {
  return {
    id,
    name,
    folder_id,
    job_id: `job-${id}`,
    status: 'succeeded',
    kind: 'pdf',
    model: 'vendor/vision',
    page_count: 10,
    pages_done: 10,
    size_bytes: 2048,
    error: null,
    created_at: '2026-08-01T00:00:00Z',
    finished_at: '2026-08-01T00:01:00Z',
    ...overrides,
  }
}

// Home ├─ Invoices ─ 2026 ─ Q1
//      └─ Receipts
const TREE = [
  folder(1, 'Invoices'),
  folder(2, '2026', 1),
  folder(3, 'Q1', 2),
  folder(4, 'Receipts'),
]

describe('childFolders', () => {
  it('lists one level, sorted the way a file manager sorts names', () => {
    const level = [folder(1, 'Page 10'), folder(2, 'page 2'), folder(3, 'Archive')]
    expect(childFolders(level, null).map((row) => row.name)).toEqual(['Archive', 'page 2', 'Page 10'])
  })

  it('treats null as the root rather than as a folder id', () => {
    expect(childFolders(TREE, null).map((row) => row.name)).toEqual(['Invoices', 'Receipts'])
    expect(childFolders(TREE, 1).map((row) => row.name)).toEqual(['2026'])
  })
})

describe('flattenTree', () => {
  it('shows only what is expanded, with a depth per row', () => {
    const collapsed = flattenTree(TREE, new Set())
    expect(collapsed.map((row) => row.folder.name)).toEqual(['Invoices', 'Receipts'])
    expect(collapsed[0]!.hasChildren).toBe(true)
    expect(collapsed[1]!.hasChildren).toBe(false)

    const opened = flattenTree(TREE, new Set([1, 2]))
    expect(opened.map((row) => [row.folder.name, row.depth])).toEqual([
      ['Invoices', 0],
      ['2026', 1],
      ['Q1', 2],
      ['Receipts', 0],
    ])
  })

  it('does not open a folder whose parent is closed', () => {
    const rows = flattenTree(TREE, new Set([2]))
    expect(rows.map((row) => row.folder.name)).toEqual(['Invoices', 'Receipts'])
  })
})

describe('folderPath', () => {
  it('reads root first, which is the order a breadcrumb is read in', () => {
    expect(folderPath(TREE, 3).map((row) => row.name)).toEqual(['Invoices', '2026', 'Q1'])
  })

  it('is empty at the root and for a folder that is gone', () => {
    expect(folderPath(TREE, null)).toEqual([])
    expect(folderPath(TREE, 99)).toEqual([])
  })

  it('stops instead of hanging if the tree ever cycles', () => {
    const cycle = [folder(1, 'A', 2), folder(2, 'B', 1)]
    expect(folderPath(cycle, 1).length).toBeLessThanOrEqual(2)
  })
})

describe('subtreeIds and canMoveFolder', () => {
  it('collects a folder and everything under it', () => {
    expect([...subtreeIds(TREE, 1)].sort()).toEqual([1, 2, 3])
    expect([...subtreeIds(TREE, 4)]).toEqual([4])
  })

  it('refuses a move into the folder itself or its own subtree', () => {
    const invoices = TREE[0]!
    expect(canMoveFolder(TREE, invoices, 1)).toBe(false)
    expect(canMoveFolder(TREE, invoices, 3)).toBe(false)
    expect(canMoveFolder(TREE, invoices, 4)).toBe(true)
  })

  it('refuses a move that changes nothing', () => {
    const q1 = TREE[2]!
    expect(canMoveFolder(TREE, q1, 2)).toBe(false)
    expect(canMoveFolder(TREE, q1, null)).toBe(true)
  })
})

describe('documentsIn', () => {
  it('filters to one folder and sorts by name', () => {
    const documents = [
      document(1, 'zeta.pdf', 1),
      document(2, 'alpha.pdf', 1),
      document(3, 'root.pdf'),
    ]
    expect(documentsIn(documents, 1).map((row) => row.name)).toEqual(['alpha.pdf', 'zeta.pdf'])
    expect(documentsIn(documents, null).map((row) => row.name)).toEqual(['root.pdf'])
  })
})

describe('parse state', () => {
  it('counts queued and running as in flight', () => {
    expect(isPending(document(1, 'a.pdf', null, { status: 'queued' }))).toBe(true)
    expect(isPending(document(1, 'a.pdf', null, { status: 'running' }))).toBe(true)
    expect(isPending(document(1, 'a.pdf', null, { status: 'succeeded' }))).toBe(false)
    expect(isPending(document(1, 'a.pdf', null, { status: 'failed' }))).toBe(false)
  })

  it('reports progress as a share, and zero before the page count is known', () => {
    expect(parseProgress(document(1, 'a.pdf', null, { pages_done: 3, page_count: 12 }))).toBe(0.25)
    expect(parseProgress(document(1, 'a.pdf', null, { pages_done: 0, page_count: null }))).toBe(0)
    expect(parseProgress(document(1, 'a.pdf', null, { pages_done: 20, page_count: 10 }))).toBe(1)
  })
})

describe('acceptsUpload', () => {
  const limits: UploadLimits = {
    upload_max_bytes: 1000,
    page_cap: 500,
    accepted_media_types: ['application/pdf', 'image/jpeg', 'image/heic'],
    accepted_extensions: ['.pdf', '.jpg', '.heic'],
    prompt_max_chars: 8000,
  }

  it('takes a file the browser typed for us', () => {
    expect(acceptsUpload({ name: 'report.pdf', type: 'application/pdf' }, limits)).toBe(true)
  })

  it('falls back to the extension when the browser had no type to give', () => {
    // What a drag out of a file manager with no `.heic` association looks like.
    expect(acceptsUpload({ name: 'scan.heic', type: '' }, limits)).toBe(true)
    expect(acceptsUpload({ name: 'scan.HEIC', type: 'application/octet-stream' }, limits)).toBe(true)
  })

  it('refuses what the server would refuse', () => {
    expect(acceptsUpload({ name: 'notes.txt', type: 'text/plain' }, limits)).toBe(false)
    expect(acceptsUpload({ name: 'notes', type: '' }, limits)).toBe(false)
  })

  it('defers to the server while the limits are still loading', () => {
    expect(acceptsUpload({ name: 'notes.txt', type: 'text/plain' }, null)).toBe(true)
  })
})

describe('isMarkdownUpload', () => {
  const limits: UploadLimits = {
    upload_max_bytes: 1000,
    page_cap: 500,
    accepted_media_types: ['application/pdf', 'text/markdown'],
    accepted_extensions: ['.pdf', '.md', '.markdown'],
    markdown_media_types: ['text/markdown'],
    markdown_extensions: ['.md', '.markdown'],
    markdown_max_bytes: 100,
    prompt_max_chars: 8000,
  }

  it('knows a markdown file by its type or its extension, as the server does', () => {
    expect(isMarkdownUpload({ name: 'readme', type: 'text/markdown' }, limits)).toBe(true)
    expect(isMarkdownUpload({ name: 'Notes.MD', type: '' }, limits)).toBe(true)
    expect(isMarkdownUpload({ name: 'plan.markdown', type: 'application/octet-stream' }, limits)).toBe(true)
  })

  it('takes anything else for a parse', () => {
    expect(isMarkdownUpload({ name: 'report.pdf', type: 'application/pdf' }, limits)).toBe(false)
    expect(isMarkdownUpload({ name: 'notes.md', type: '' }, null)).toBe(false)
  })
})
