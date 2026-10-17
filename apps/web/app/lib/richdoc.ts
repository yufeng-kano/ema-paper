/**
 * The rich editor's content, to and from markdown (docs/web.md § Editing).
 *
 * The editor never reads markdown with a parser of its own. Its content is built from the
 * blocks `parseResultMarkdown` produces and the segments `parseInline` produces, so what an
 * open block shows is exactly what the rendered block showed — `$5 and $6` stays two prices,
 * `\[87]` stays `[87]` — and it is written back by the serializer below.
 *
 * **Only what the reader changed is rewritten.** Every top-level node carries the source it
 * was built from (`src`) and the canonical serialization of that source (`canon`). On the
 * way back, a node whose canonical form is unchanged is written as its original bytes; only
 * a node the reader actually changed is written in the canonical form. A paragraph split by
 * Enter keeps the attributes on both halves, and neither half matches the canonical form of
 * the whole any more, so both are written fresh — which is right.
 *
 * Framework-free on purpose: it handles TipTap's JSON shape, not an editor, so it is unit
 * tested in node (docs/testing.md § Frontend).
 */
import type { JSONContent } from '@tiptap/core'
import {
  ESCAPABLE,
  WORD_CHAR,
  isEscaped,
  mathSpanAt,
  parseInline,
  parseResultMarkdown,
  readsAsTocRow,
  type InlineSegment,
  type ResultBlock,
} from './markdown'

type Mark = 'bold' | 'italic' | 'code'

// --- markdown -> content -----------------------------------------------------------------

function inlineContent(segments: InlineSegment[], marks: Mark[] = []): JSONContent[] {
  const content: JSONContent[] = []
  for (const segment of segments) {
    switch (segment.kind) {
      case 'text':
        if (segment.text) {
          content.push(textNode(segment.text, marks))
        }
        break
      case 'math':
        content.push({ type: 'inlineMath', attrs: { latex: segment.tex } })
        break
      case 'strong':
        content.push(...inlineContent(segment.segments, [...marks, 'bold']))
        break
      case 'em':
        content.push(...inlineContent(segment.segments, [...marks, 'italic']))
        break
      case 'code':
        if (segment.text) {
          content.push(textNode(segment.text, [...marks, 'code']))
        }
        break
      case 'br':
        content.push({ type: 'hardBreak' })
        break
    }
  }
  return content
}

function textNode(text: string, marks: Mark[]): JSONContent {
  const unique = [...new Set(marks)]
  return unique.length
    ? { type: 'text', text, marks: unique.map((type) => ({ type })) }
    : { type: 'text', text }
}

function paragraph(text: string): JSONContent {
  return { type: 'paragraph', content: inlineContent(parseInline(text)) }
}

/** One parsed block as the node the editor holds, without its source attributes. */
function blockNode(block: ResultBlock): JSONContent {
  switch (block.kind) {
    case 'p':
      return paragraph(block.text)
    case 'h2':
    case 'h3':
      return {
        type: 'heading',
        attrs: { level: Math.min(6, Math.max(1, block.level)) },
        content: inlineContent(parseInline(block.text)),
      }
    case 'list':
      return {
        type: block.ordered ? 'orderedList' : 'bulletList',
        ...(block.ordered ? { attrs: { start: block.start ?? 1 } } : {}),
        content: block.items.map((item) => ({ type: 'listItem', content: [paragraph(item)] })),
      }
    case 'table': {
      const width = Math.max(block.header.length, ...block.rows.map((row) => row.length), 1)
      const row = (cells: string[], type: 'tableHeader' | 'tableCell'): JSONContent => ({
        type: 'tableRow',
        content: Array.from({ length: width }, (_, column) => ({
          type,
          content: [paragraph(cells[column] ?? '')],
        })),
      })
      return {
        type: 'table',
        content: [row(block.header, 'tableHeader'), ...block.rows.map((cells) => row(cells, 'tableCell'))],
      }
    }
    case 'math':
      return { type: 'blockMath', attrs: { latex: block.text } }
    case 'code':
      return {
        type: 'codeBlock',
        attrs: { language: block.lang },
        ...(block.text ? { content: [{ type: 'text', text: block.text }] } : {}),
      }
    // A contents page, a figure placeholder and a comment have no rich-text form: they ride
    // through the editor as their source, untouched.
    case 'toc':
    case 'fig':
    case 'comment':
      return { type: 'rawBlock', attrs: { source: block.src } }
  }
}

/** The editor's document for a run of blocks. Never empty: an editor needs a textblock.
 *  `leads` is the whitespace before each block in the file it came from (`markdownToDoc`). */
export function blocksToDoc(blocks: ResultBlock[], leads: (string | null)[] = []): JSONContent {
  const content = blocks.map((block, index) => {
    const node = blockNode(block)
    const lead = leads[index] ?? null
    // `src` before `canon`: a table's rule row is read from its source, on the way in as on
    // the way back, or an untouched table would never match its canonical form.
    const sourced = { ...node, attrs: { ...node.attrs, src: block.src } }
    return { ...sourced, attrs: { ...sourced.attrs, canon: serializeNode(sourced), lead } }
  })
  return { type: 'doc', content: content.length ? content : [{ type: 'paragraph' }] }
}

const PAGE_MARKER_LINE = /^\s*<!--\s*page:\s*\d+\s*-->\s*$/

/**
 * The editor's document for a markdown string — one block's source, or a whole notes file.
 *
 * The notes editor writes the whole file back, so what lies between blocks is kept too: a
 * page marker the file carries rides through as its own line, and the whitespace before
 * each block is remembered (`lead`) so an edit elsewhere does not respace the file.
 */
export function markdownToDoc(markdown: string): JSONContent {
  const source = markdown.replace(/\r\n?/g, '\n')
  const placed: { at: number; block: ResultBlock }[] = []
  for (const match of source.matchAll(/^[ \t]*(<!--\s*page:\s*\d+\s*-->)[ \t]*$/gm)) {
    placed.push({ at: match.index + match[0].indexOf(match[1] ?? ''), block: { kind: 'comment', src: match[1] ?? '' } })
  }
  // Every block where the file has it — a marker included, so content before the first
  // marker stays before it.
  let cursor = 0
  for (const block of parseResultMarkdown(source).flatMap((page) => page.blocks)) {
    const at = source.indexOf(block.src, cursor)
    placed.push({ at: at === -1 ? cursor : at, block })
    cursor = at === -1 ? cursor : at + block.src.length
  }
  placed.sort((a, b) => a.at - b.at)
  const leads: (string | null)[] = []
  let end = 0
  for (const { at, block } of placed) {
    leads.push(at >= end ? source.slice(end, at) : null)
    end = Math.max(end, at + block.src.length)
  }
  return blocksToDoc(
    placed.map(({ block }) => block),
    leads,
  )
}

// --- content -> markdown -----------------------------------------------------------------

/**
 * Plain text as markdown that `parseInline` reads back as the same text: every character it
 * would take as syntax is backslash-escaped, and nothing else is — `usage_log`, `C:\path`
 * and "$5 and $6" come back as typed.
 */
function escapeText(text: string, inTable: boolean): string {
  let out = ''
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index] ?? ''
    const next = text[index + 1]
    if (char === '\\') {
      // Before punctuation (or at a node's end, where a marker may follow) a backslash
      // would escape what comes next instead of printing itself.
      out += next === undefined || ESCAPABLE.test(next) ? '\\\\' : '\\'
    } else if (char === '*' || char === '`') {
      out += `\\${char}`
    } else if (char === '_') {
      const inWord = WORD_CHAR.test(text[index - 1] ?? '') && WORD_CHAR.test(next ?? '')
      out += inWord ? '_' : '\\_'
    } else if (char === '<' && /^<br\s*\/?>/i.test(text.slice(index))) {
      out += '\\<'
    } else if (char === '|' && inTable) {
      out += '\\|'
    } else {
      out += char
    }
  }
  return out
}

function markSet(node: JSONContent): Mark[] {
  return (node.marks ?? [])
    .map((mark) => mark.type)
    .filter((type): type is Mark => type === 'bold' || type === 'italic' || type === 'code')
}

function sameMarks(a: Mark[], b: Mark[]): boolean {
  return a.length === b.length && a.every((mark) => b.includes(mark))
}

/** Stands in for a formula or a code span while the line's dollars are checked: neither's
 *  characters are prose, so neither may be escaped. A private-use character, never typed. */
const HELD = '\uE000'

/** A textblock's inline content as one line of markdown. */
export function serializeInline(content: JSONContent[] = [], inTable = false): string {
  // Adjacent text with the same marks is one run: `**a**` + `**b**` would read back as
  // one strong span holding a literal `****`.
  const runs: JSONContent[] = []
  for (const node of content) {
    const last = runs[runs.length - 1]
    if (node.type === 'text' && last?.type === 'text' && sameMarks(markSet(node), markSet(last))) {
      runs[runs.length - 1] = { ...last, text: `${last.text ?? ''}${node.text ?? ''}` }
    } else {
      runs.push(node)
    }
  }
  const held: string[] = []
  const hold = (markdown: string) => {
    held.push(markdown)
    return HELD
  }
  const pieces = runs.map((node, index) => {
    if (node.type === 'hardBreak') {
      return '<br>'
    }
    if (node.type === 'inlineMath') {
      const latex = String(node.attrs?.latex ?? '').trim()
      if (!latex) {
        return ''
      }
      // A span never closes on a dollar followed by a digit ("$5 and $6" are prices), so
      // a formula right before one is kept apart by an invisible zero-width space —
      // anything visible would change the text the reader typed.
      const next = runs[index + 1]
      return hold(next?.type === 'text' && /^\d/.test(next.text ?? '') ? `$${latex}$\u200b` : `$${latex}$`)
    }
    const text = node.text ?? ''
    const marks = markSet(node)
    // Whitespace stays outside the markers: `** bold**` opens nothing.
    const [, lead = '', body = '', trail = ''] = /^(\s*)([\s\S]*?)(\s*)$/.exec(text) ?? []
    if (!body) {
      return escapeText(text, inTable)
    }
    let inner: string
    // A code span is single backticks with no backtick inside (`parseInline`), so code
    // that holds one is written as plain text rather than as a broken span. A pipe in a
    // table cell is escaped even inside code: the row is split before the cell is read.
    if (marks.includes('code') && !body.includes('`') && !body.includes('\n')) {
      inner = hold(`\`${inTable ? body.replace(/\|/g, '\\|') : body}\``)
    } else {
      inner = escapeText(body, inTable)
      // A literal dollar right before a formula would become its opener (`price $$x$`).
      if (runs[index + 1]?.type === 'inlineMath' && !trail && inner.endsWith('$') && !isEscaped(inner, inner.length - 1)) {
        inner = `${inner.slice(0, -1)}\\$`
      }
    }
    if (marks.includes('italic')) {
      inner = `*${inner}*`
    }
    if (marks.includes('bold')) {
      inner = `**${inner}**`
    }
    return `${lead}${inner}${trail}`
  })
  // Two emphasis runs that meet (`*a*` then `**b**`) would merge their asterisks into a
  // run the parser cannot split; an invisible zero-width space keeps them apart.
  let line = pieces.reduce((out, piece) => (out.endsWith('*') && piece.startsWith('*') ? `${out}\u200b${piece}` : `${out}${piece}`), '')
  // Dollars last, across the whole line: a literal dollar in one run and another in the
  // next (`\$**sale**\$`) would otherwise pair into a formula.
  for (let index = 0; index < line.length; index += 1) {
    if (line[index] === '$' && !isEscaped(line, index) && mathSpanAt(line, index)) {
      line = `${line.slice(0, index)}\\${line.slice(index)}`
      index += 1
    }
  }
  let next = 0
  return line.replace(new RegExp(HELD, 'g'), () => held[next++] ?? '')
}

/** A line with its leading characters escaped where they would start another block. */
function escapeLineStart(line: string): string {
  // A sentence ending in a dot leader and a number would read back as a contents row; one
  // escaped dot breaks the leader and still renders as a dot.
  const text = readsAsTocRow(line) ? line.replace(/\.(?=[^.]*$)/, '\\.') : line
  if (/^(#|[-+*]\s|\d{1,9}[.)]\s|\$\$|```|~~~|<!--|!\[)/.test(text)) {
    const marker = /^\d{1,9}/.exec(text)
    return marker ? `${marker[0]}\\${text.slice(marker[0].length)}` : `\\${text}`
  }
  return text
}

/** A paragraph's line, with a leading character escaped where it would start a block. */
function paragraphLine(content: JSONContent[] | undefined): string {
  return escapeLineStart(serializeInline(content))
}

/**
 * A figure caption from its editor's markdown: one line, never starting another block —
 * the parser reads only the line under a placeholder as its caption.
 */
export function captionLine(markdown: string): string {
  const line = markdown.replace(/\s*\n+\s*/g, ' ').trim()
  return line ? escapeLineStart(line.replace(/^(#{1,6}|[-+*]|\d{1,9}[.)])\s+/, '')) : ''
}

/** Every textblock under a node, flattened — a list item's paragraphs, a cell's lines. */
function textblocks(node: JSONContent): JSONContent[] {
  if (node.type === 'paragraph' || node.type === 'heading') {
    return [node]
  }
  return (node.content ?? []).flatMap(textblocks)
}

/**
 * A node inside a list item or a table cell as lines of inline markdown. The parser reads
 * items and cells as one line each, so a formula or code block made inside one is written
 * inline (`$…$`, `` `…` ``) rather than dropped.
 */
function inlineLines(node: JSONContent, inTable: boolean): string[] {
  switch (node.type) {
    case 'paragraph':
    case 'heading':
      return [serializeInline(node.content, inTable)]
    case 'blockMath': {
      // One line: an item or a row ends at a line break.
      const latex = String(node.attrs?.latex ?? '').replace(/\s*\n\s*/g, ' ')
      return [serializeInline([{ type: 'inlineMath', attrs: { latex } }], inTable)]
    }
    case 'codeBlock':
      return (node.content ?? [])
        .map((child) => child.text ?? '')
        .join('')
        .split('\n')
        .map((line) => serializeInline(line ? [{ type: 'text', text: line, marks: [{ type: 'code' }] }] : [], inTable))
    case 'rawBlock':
      return [String(node.attrs?.source ?? '')]
    default:
      return (node.content ?? []).flatMap((child) => inlineLines(child, inTable))
  }
}

function serializeTable(node: JSONContent): string {
  const rows = (node.content ?? []).map((row) =>
    (row.content ?? []).map((cell) =>
      inlineLines(cell, true)
        .filter((line) => line !== '')
        .join('<br>'),
    ),
  )
  const width = Math.max(1, ...rows.map((row) => row.length))
  const line = (cells: string[]) =>
    `| ${Array.from({ length: width }, (_, column) => cells[column] ?? '').join(' | ')} |`
  const [header = [], ...body] = rows
  return [line(header), tableRule(node, width), ...body.map(line)].join('\n')
}

/**
 * The separator row: the table's own when it still has as many columns — its alignment
 * colons are the page's — and plain `---` rules otherwise.
 */
function tableRule(node: JSONContent, width: number): string {
  const source = typeof node.attrs?.src === 'string' ? node.attrs.src : ''
  const rule = source
    .split('\n')
    .map((row) => row.trim())
    .find((row) => /^\|?[\s|:-]+\|[\s|:-]*$/.test(row) && row.includes('-'))
  const cells = rule?.replace(/^\|/, '').replace(/\|$/, '').split('|') ?? []
  return rule && cells.length === width
    ? rule
    : `| ${Array.from({ length: width }, () => '---').join(' | ')} |`
}

/** One top-level node in its canonical markdown form; '' for a node with nothing in it. */
export function serializeNode(node: JSONContent): string {
  switch (node.type) {
    case 'paragraph':
      return paragraphLine(node.content)
    case 'heading': {
      const text = serializeInline(node.content)
      return text ? `${'#'.repeat(Number(node.attrs?.level ?? 2))} ${text}` : ''
    }
    case 'bulletList':
    case 'orderedList': {
      // A list is one level (docs/web.md § Known limitation): a nested item that arrived
      // by paste is written as the next item rather than dropped.
      const items = (node.content ?? [])
        .flatMap((item) => collectItems(item))
        .filter((item) => item !== '')
      const start = Number(node.attrs?.start ?? 1)
      return items
        .map((item, index) => (node.type === 'orderedList' ? `${start + index}. ${item}` : `- ${item}`))
        .join('\n')
    }
    case 'table':
      return serializeTable(node)
    case 'blockMath': {
      const latex = String(node.attrs?.latex ?? '').trim()
      return latex ? `$$\n${latex}\n$$` : ''
    }
    case 'codeBlock': {
      // The parser closes a code block on any fence line, whatever opened it, and on a page
      // marker, so either inside the code is kept from closing it by a zero-width space.
      const text = (node.content ?? [])
        .map((child) => child.text ?? '')
        .join('')
        .replace(/^(\s*)(```|~~~|<!--\s*page:)/gm, '$1\u200b$2')
      return `\`\`\`${String(node.attrs?.language ?? '')}\n${text}\n\`\`\``
    }
    case 'rawBlock':
      return String(node.attrs?.source ?? '')
    default:
      return textblocks(node).map((block) => serializeInline(block.content)).join(' ')
  }
}

function collectItems(item: JSONContent): string[] {
  const lines: string[] = []
  const nested: string[] = []
  for (const child of item.content ?? []) {
    if (child.type === 'bulletList' || child.type === 'orderedList') {
      nested.push(...(child.content ?? []).flatMap(collectItems))
    } else {
      lines.push(...inlineLines(child, false))
    }
  }
  return [lines.filter((line) => line !== '').join(' '), ...nested]
}

/**
 * The editor's document as markdown: unchanged blocks as the bytes they came from, changed
 * ones in canonical form, empty ones dropped — emptying a block deletes it. Blocks are
 * separated by the whitespace the file had before them (`lead`), one blank line for a block
 * that is new, a single newline under a page marker; `trailing` is what the file ended with.
 */
export function docToMarkdown(doc: JSONContent, trailing = ''): string {
  let out = ''
  let previous = ''
  for (const node of doc.content ?? []) {
    const fresh = serializeNode(node)
    const { src, canon, lead } = node.attrs ?? {}
    const markdown = typeof src === 'string' && typeof canon === 'string' && fresh === canon ? src : fresh
    if (markdown.trim() === '') {
      continue
    }
    if (out) {
      const gap = typeof lead === 'string' && /\n/.test(lead) ? lead : PAGE_MARKER_LINE.test(previous) ? '\n' : '\n\n'
      out += gap
    }
    out += markdown
    previous = markdown
  }
  return out ? `${out}${trailing}` : ''
}

/**
 * Markdown for a clipboard slice: a run of inline content (a selection inside one block) is
 * one line, anything structural is the blocks it holds.
 */
export function sliceToMarkdown(content: JSONContent[]): string {
  const inline = content.every((node) => node.type === 'text' || node.type === 'inlineMath' || node.type === 'hardBreak')
  return inline ? serializeInline(content) : content.map(serializeNode).filter(Boolean).join('\n\n')
}

/**
 * The `$…$` span a typed closing dollar completes, for the inline-math input rule: `text`
 * is the textblock up to and including that dollar. The text is scanned the way
 * `parseInline` scans it — left to right, a matched span stepped over whole — so the span
 * found is the one the viewer would draw, a price stays a price, and the dollars nested in
 * `$\text{\sout{$E$}}$` belong to the outer formula.
 */
export function inlineMathAtEnd(text: string): { index: number; tex: string } | null {
  if (!text.endsWith('$')) {
    return null
  }
  let scan = 0
  while (scan < text.length) {
    const open = text.indexOf('$', scan)
    if (open === -1) {
      return null
    }
    const span = isEscaped(text, open) ? null : mathSpanAt(text, open)
    if (span) {
      if (open + span.length === text.length) {
        // `$$x$` is a display fence being typed, not a dollar sign and a formula.
        return text[open - 1] === '$' ? null : { index: open, tex: span.tex }
      }
      scan = open + span.length
    } else {
      scan = open + 1
    }
  }
  return null
}
