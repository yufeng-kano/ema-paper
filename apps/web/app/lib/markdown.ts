/**
 * The result document, as blocks the viewer can render.
 *
 * `GET /api/jobs/{id}/result` hands back the whole document as one markdown string with
 * `<!-- page: N -->` markers between pages and `![figN](sightread://pN/…)` placeholders
 * where figures belong (docs/parsing.md § Page markers). The viewer needs it the other way
 * round — ordered blocks grouped by page — so the split happens here, on the client, rather
 * than being asked of an API whose contract is the markdown itself.
 *
 * This is deliberately not a general markdown parser. It understands exactly the shapes the
 * transcription prompt asks the model for: headings, paragraphs, lists, pipe tables, fenced
 * code, display math, figure placeholders (docs/parsing.md § Prompt) and the contents-page
 * lines a book arrives with. Anything else stays
 * a paragraph, which renders as its own source text rather than disappearing.
 *
 * It is also flat, and knowingly so: a nested list comes out as one level, and a fence is
 * closed by any fence rather than by a matching one — so a `~~~` block containing a
 * literal ``` line ends early. The content
 * survives, the structure does not. The alternative is not a deeper version of this — it is
 * rendering through a real GFM parser, which is a dependency and a sanitisation decision
 * (docs/web.md § Result viewer).
 */

/** `[ymin, xmin, ymax, xmax]`, 0–1000 normalized — the `bbox_format` the result declares. */
export type Bbox = [number, number, number, number]

/**
 * Every block keeps `src`: the exact source lines it was read from, joined by `\n`, with
 * nothing trimmed or normalised. It is what the viewer's block editor opens, and what
 * `serializeDocument` writes back — so a document that is parsed and serialized again is
 * the same bytes, and editing one block touches nothing around it (docs/web.md § Editing).
 */
export type ResultBlock =
  /** `level` is the source depth (`#…` count) — the viewer draws two heading sizes, but a
   *  copy must hand the original level back (docs/web.md § Result viewer). */
  | { kind: 'h2'; text: string; level: number; src: string }
  | { kind: 'h3'; text: string; level: number; src: string }
  | { kind: 'p'; text: string; src: string }
  | { kind: 'list'; ordered: boolean; items: string[]; start?: number; src: string }
  | { kind: 'math'; text: string; src: string }
  | { kind: 'code'; text: string; lang: string | null; src: string }
  | { kind: 'table'; header: string[]; rows: string[][]; src: string }
  | { kind: 'fig'; id: string; bbox: Bbox; caption: string | null; src: string }
  | { kind: 'toc'; entries: TocEntry[]; src: string }
  /** An HTML comment that is not a page marker: kept in the source, drawn as nothing. */
  | { kind: 'comment'; src: string }

/**
 * One line of a contents page. `title + leader + page` is the source line exactly — the
 * leader is drawn rather than printed, and copy hands its characters back.
 */
export interface TocEntry {
  title: string
  /** What sat between the title and the page number: dots, or just the space. */
  leader: string
  page: string | null
}

export interface ResultPageBlocks {
  /** The viewer never prints this — it addresses a figure's crop with it. */
  page: number
  blocks: ResultBlock[]
}

const PAGE_MARKER = /^<!--\s*page:\s*(\d+)\s*-->$/
const FIGURE = /^!\[([^\]\n]*)\]\(\s*sightread:\/\/p(\d+)\/(-?\d+),(-?\d+),(-?\d+),(-?\d+)\s*\)$/
const HEADING = /^(#{1,6})\s+(.*)$/
const COMMENT = /^<!--.*-->$/
/** `- item`, `* item`, `+ item`, `1. item`, `2) item`. */
const LIST_ITEM = /^([-*+]|\d{1,9}[.)])\s+(.*)$/
/** A display-math fence: `$$` alone on its line, or sharing a line with the formula. */
const MATH_FENCE = '$$'

/**
 * Whether a line opens display math. `$$` alone does; so does `$$x = 1$$` on one line —
 * how most models write a numbered equation — and `$$x = 1` whose closing dollars come
 * lines later. A line with a second `$$` anywhere but its end (`$$x$$ where x is…`) is
 * prose: read as an opener it would swallow the document up to the next fence.
 */
function opensMath(line: string): boolean {
  if (!line.startsWith(MATH_FENCE)) {
    return false
  }
  const again = line.indexOf(MATH_FENCE, MATH_FENCE.length)
  return again === -1 || (again === line.length - MATH_FENCE.length && again > MATH_FENCE.length)
}

/** A page number as a contents page prints one: arabic or roman, possibly emphasised. */
const TOC_PAGE = String.raw`(?:\*\*|__)?(?:\d+|[ivxlcdm]+)(?:\*\*|__)?`
/** `Title . . . . 13` / `Title.....13`: a dot leader of four or more, then the page. */
const TOC_LEADER = new RegExp(String.raw`^(.*?\S)(\s*(?:\.\s*){4,})(${TOC_PAGE})$`, 'i')
/** Whether a line would be read as a contents row — what an editor writing prose must
 *  escape so a typed `Wait.... 5` stays a sentence. */
export function readsAsTocRow(line: string): boolean {
  return TOC_LEADER.test(line)
}

/** A row of the same run without a leader — `**1 Introduction** **13**`. Digits only:
 *  with no leader to vouch for it, a trailing "mix" or "civil" is a word, not a numeral. */
const TOC_BARE = /^(.*?\S)(\s+)((?:\*\*|__)?\d+(?:\*\*|__)?)$/

/**
 * The lines of a paragraph as contents-page rows, or null when none of them carries a dot
 * leader — which is what tells a contents page from prose that happens to wrap.
 */
function tocEntries(rows: string[]): TocEntry[] | null {
  if (!rows.some((row) => TOC_LEADER.test(row))) {
    return null
  }
  return rows.map((row) => {
    const match = TOC_LEADER.exec(row) ?? TOC_BARE.exec(row)
    return match
      ? { title: match[1] ?? '', leader: match[2] ?? '', page: match[3] ?? null }
      : { title: row, leader: '', page: null }
  })
}
/** A fenced code block's opening or closing line, with its optional info string. */
const CODE_FENCE = /^(?:```|~~~)\s*([^\s`]*)\s*$/

/** A pipe table's separator row: `| --- | :--: |`, with or without the outer pipes. */
function isTableRule(line: string): boolean {
  return /^\|?[\s|:-]+\|[\s|:-]*$/.test(line) && line.includes('-')
}

/**
 * A table is its header row *plus* the separator under it — GFM makes the outer pipes
 * optional, so `Name | Value` over `--- | ---` is as valid as the fully piped form and the
 * only reliable marker is the separator. Looking one line ahead is what lets both forms in
 * without treating every sentence containing a pipe as a table.
 */
function isTableStart(line: string, next: string): boolean {
  return line.includes('|') && isTableRule(next)
}

/** `1.` and `2)` are ordered; `-`, `*` and `+` are not. */
function isOrdered(marker: string): boolean {
  return !'-*+'.includes(marker)
}

/**
 * Every line that starts a block of its own — so a paragraph knows where to stop. `next` is
 * the line after it, which only the table check needs.
 */
function startsBlock(line: string, next = ''): boolean {
  return (
    !line ||
    opensMath(line) ||
    CODE_FENCE.test(line) ||
    isTableStart(line, next) ||
    line.startsWith('#') ||
    LIST_ITEM.test(line) ||
    FIGURE.test(line) ||
    PAGE_MARKER.test(line) ||
    COMMENT.test(line)
  )
}

/**
 * Splits on the pipes that separate columns, which is not every pipe: GFM escapes a literal
 * one as `\|`, and splitting on that both invents a column and leaves the backslash on
 * screen. The outer pipes are optional, so they are only trimmed when present.
 */
function splitRow(line: string): string[] {
  return line
    .replace(/^\s*\|/, '')
    .replace(/(?<!\\)\|\s*$/, '')
    .split(/(?<!\\)\|/)
    .map((cell) => cell.trim().replace(/\\\|/g, '|'))
}

/**
 * Groups the document by page marker. Content before the first marker — a result stored
 * before the pipeline wrote them, say — is kept as page 1 rather than dropped: the viewer
 * would otherwise show an empty document for a job that has one.
 */
export function parseResultMarkdown(markdown: string): ResultPageBlocks[] {
  const pages: ResultPageBlocks[] = []
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n')

  let current: ResultPageBlocks | null = null

  function page(): ResultPageBlocks {
    if (!current) {
      current = { page: 1, blocks: [] }
      pages.push(current)
    }
    return current
  }

  /** The raw source of lines `from` through `to`, inclusive — a block's `src`. */
  function source(from: number, to: number): string {
    return lines.slice(from, to + 1).join('\n')
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = (lines[index] ?? '').trim()
    const first = index

    const marker = PAGE_MARKER.exec(line)
    if (marker) {
      const number = Number(marker[1])
      // A marker for the page already open (the leading-content case above) continues it
      // rather than starting an empty duplicate.
      if (!current || current.page !== number) {
        current = { page: number, blocks: [] }
        pages.push(current)
      }
      continue
    }

    if (!line) {
      continue
    }
    if (COMMENT.test(line)) {
      // Not a page marker, but the file's: a block of its own so an edit elsewhere
      // leaves it in the source (docs/web.md § Editing).
      page().blocks.push({ kind: 'comment', src: source(first, index) })
      continue
    }

    const figure = FIGURE.exec(line)
    if (figure) {
      const target = page()
      // The caption is written verbatim on the next line, and only there — anything that
      // starts another block is the next block, not this figure's caption.
      const next = (lines[index + 1] ?? '').trim()
      const captioned = Boolean(next) && !startsBlock(next, (lines[index + 2] ?? '').trim())
      if (captioned) {
        index += 1
      }
      target.blocks.push({
        kind: 'fig',
        id: figure[1] || `fig${target.blocks.filter((block) => block.kind === 'fig').length + 1}`,
        bbox: [Number(figure[3]), Number(figure[4]), Number(figure[5]), Number(figure[6])],
        caption: captioned ? next : null,
        src: source(first, index),
      })
      continue
    }

    // Code, like math, is content whose line breaks and indentation are the point. The
    // prompt tells the model not to wrap its *answer* in a fence, so a fence that does
    // arrive is a code block the page carried.
    const codeFence = CODE_FENCE.exec(line)
    if (codeFence) {
      const rows: string[] = []
      let cursor = index + 1
      while (
        cursor < lines.length &&
        !CODE_FENCE.test((lines[cursor] ?? '').trim()) &&
        !PAGE_MARKER.test((lines[cursor] ?? '').trim())
      ) {
        rows.push((lines[cursor] ?? '').trimEnd())
        cursor += 1
      }
      index = CODE_FENCE.test((lines[cursor] ?? '').trim()) ? cursor : cursor - 1
      page().blocks.push({
        kind: 'code',
        text: rows.join('\n'),
        lang: codeFence[1] || null,
        src: source(first, index),
      })
      continue
    }

    // Display math is the one block whose line breaks *are* its content: an aligned group
    // of equations joined into a paragraph is no longer the formula the page carried, and
    // the prompt asks for formulas to be kept.
    if (opensMath(line)) {
      // The dollars may share a line with the formula at either end, so what follows the
      // opener and what precedes the closer are rows like any other.
      const opening = line.slice(MATH_FENCE.length)
      if (opening.endsWith(MATH_FENCE)) {
        page().blocks.push({
          kind: 'math',
          text: opening.slice(0, -MATH_FENCE.length).trim(),
          src: source(first, index),
        })
        continue
      }
      const rows: string[] = opening.trim() ? [opening.trim()] : []
      let cursor = index + 1
      let closed = false
      // A page marker closes an unclosed fence: a model that forgets the closing `$$` must
      // not swallow the rest of the document into one formula.
      while (cursor < lines.length && !PAGE_MARKER.test((lines[cursor] ?? '').trim())) {
        const row = (lines[cursor] ?? '').trimEnd()
        if (row.endsWith(MATH_FENCE)) {
          const last = row.slice(0, -MATH_FENCE.length).trimEnd()
          if (last.trim()) {
            rows.push(last)
          }
          closed = true
          break
        }
        rows.push(row)
        cursor += 1
      }
      // Past the closing fence, or back onto the page marker so the next pass sees it.
      index = closed ? cursor : cursor - 1
      page().blocks.push({ kind: 'math', text: rows.join('\n'), src: source(first, index) })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      const level = (heading[1] ?? '').length
      page().blocks.push({
        kind: level >= 3 ? 'h3' : 'h2',
        text: (heading[2] ?? '').trim(),
        level,
        src: source(first, index),
      })
      continue
    }

    // A list before the paragraph fallback: merged into prose, `- First` / `- Second`
    // becomes the single line "- First - Second", losing both the structure and the
    // markers the model was asked to produce.
    const listItem = LIST_ITEM.exec(line)
    if (listItem) {
      const marker = listItem[1] ?? ''
      const ordered = isOrdered(marker)
      // A list continued from the previous page starts at 5, not at 1, and renumbering it
      // changes what the document says.
      const start = ordered ? Number.parseInt(marker, 10) : Number.NaN
      const items: string[] = []
      let cursor = index
      while (cursor < lines.length) {
        const raw = lines[cursor] ?? ''
        const trimmed = raw.trim()

        // One block per marker kind: a bulleted list directly under a numbered one is two
        // lists, not one with a confused type.
        const match = LIST_ITEM.exec(trimmed)
        if (match) {
          if (isOrdered(match[1] ?? '') !== ordered) {
            break
          }
          items.push((match[2] ?? '').trim())
          cursor += 1
          continue
        }

        // An indented line that starts no block of its own is the rest of the item above
        // it — a wrapped item is one item, not an item and a stray paragraph.
        const continues =
          items.length > 0 &&
          /^\s+\S/.test(raw) &&
          !startsBlock(trimmed, (lines[cursor + 1] ?? '').trim())
        if (!continues) {
          break
        }
        items[items.length - 1] += ` ${trimmed}`
        cursor += 1
      }
      index = cursor - 1
      page().blocks.push({
        kind: 'list',
        ordered,
        items,
        ...(Number.isFinite(start) && start !== 1 ? { start } : {}),
        src: source(first, index),
      })
      continue
    }

    // `isTableStart` for both forms, with no shortcut for a leading pipe: the prompt asks
    // for formulas, and `|x| = 3` is a line of maths, not a header-only table.
    if (isTableStart(line, (lines[index + 1] ?? '').trim())) {
      const rows: string[][] = []
      let cursor = index
      while (cursor < lines.length) {
        const row = (lines[cursor] ?? '').trim()
        // A row is anything still carrying a pipe. Once the pipes stop, so does the table —
        // the prose under it is the next block.
        if (!row.includes('|')) {
          break
        }
        if (!isTableRule(row)) {
          rows.push(splitRow(row))
        }
        cursor += 1
      }
      index = cursor - 1
      const [header, ...body] = rows
      page().blocks.push({
        kind: 'table',
        header: header ?? [],
        rows: body,
        src: source(first, index),
      })
      continue
    }

    // Everything else is prose: consecutive lines are one paragraph, as in markdown.
    const paragraph: string[] = [line]
    let cursor = index + 1
    while (cursor < lines.length) {
      const next = (lines[cursor] ?? '').trim()
      if (startsBlock(next, (lines[cursor + 1] ?? '').trim())) {
        break
      }
      paragraph.push(next)
      cursor += 1
    }
    index = cursor - 1
    // A contents page keeps its lines: joined, a book's `1.1 Title . . . . 13` rows read
    // as one run-on paragraph of dots (docs/web.md § Result viewer).
    const entries = tocEntries(paragraph)
    const src = source(first, index)
    page().blocks.push(
      entries ? { kind: 'toc', entries, src } : { kind: 'p', text: paragraph.join(' '), src },
    )
  }

  return pages
}

// --- back to markdown --------------------------------------------------------------------
//
// The viewer edits one block at a time and the API stores one markdown string, so the
// blocks have to become that string again. They do so from their `src`, never from their
// parsed fields: a block the parser only half understood (a nested list, a `~~~` fence
// holding a ``` line — docs/web.md § Known limitation) still serializes as the bytes it
// came from rather than as a flattened reading of them.

/** How the pipeline writes a page's marker (docs/parsing.md § Page markers). */
function pageMarker(page: number): string {
  return `<!-- page: ${page} -->`
}

/**
 * The document as the one markdown string the API stores: each page as its marker over
 * its blocks, blocks separated by one blank line, pages by one blank line — the shape
 * `parsing/markdown.py` assembles. A block whose source is blank is no block: emptying a
 * block in the editor deletes it rather than leaving a hole of blank lines.
 *
 * Byte-stable through `parseResultMarkdown` for a document in that shape; a document with
 * extra blank lines between blocks, or content ahead of its first marker, comes back in
 * it, which is the one normalisation this round trip makes.
 */
export function serializeDocument(pages: ResultPageBlocks[]): string {
  return pages
    .map((page) => {
      const blocks = page.blocks.map((block) => block.src).filter((src) => src.trim() !== '')
      const marker = pageMarker(page.page)
      return blocks.length ? `${marker}\n${blocks.join('\n\n')}` : marker
    })
    .join('\n\n')
}

/**
 * `markdown` with one block's source replaced — the viewer's edit, as the string to save.
 * The new source need not stay one block: a paragraph edited into two is two on the next
 * parse, and an emptied one disappears (see `serializeDocument`).
 */
export function replaceBlockSource(
  markdown: string,
  pageIndex: number,
  blockIndex: number,
  src: string,
): string {
  const pages = parseResultMarkdown(markdown)
  const block = pages[pageIndex]?.blocks[blockIndex]
  if (block) {
    block.src = src
  }
  return serializeDocument(pages)
}

/**
 * `markdown` with a new block appended to one page — how text comes back to a page whose
 * last block was emptied, and to a document with no block at all: a document without a
 * page gets page 1. Only the source matters to the stored string; the block parses into
 * its kind on the next render.
 */
export function appendBlockSource(markdown: string, pageIndex: number, src: string): string {
  const pages = parseResultMarkdown(markdown)
  const page = pages[pageIndex] ?? pages[pages.length - 1]
  if (page) {
    page.blocks.push({ kind: 'p', text: src, src })
  } else {
    pages.push({ page: 1, blocks: [{ kind: 'p', text: src, src }] })
  }
  return serializeDocument(pages)
}

// --- inline content --------------------------------------------------------------------
//
// Transcriptions carry inline TeX the way papers do — `Qingwen Bu$^{1,2}$`, `H$_2$O` — and
// inline markdown the way tables do — `**96.5**`, `UniVLA<br>(Ours)` — and showing either
// as source characters makes the document read like a syntax error. `$…$` spans are split
// out here and typeset by KaTeX at render time (docs/web.md § Result viewer); the text
// between them is parsed for strong/em, inline code and `<br>` line breaks. Each styled
// segment keeps its exact source (`tex`, `src`), which is what copy-as-markdown reads back.

export type InlineSegment =
  | { kind: 'text'; text: string }
  /** `tex` is the source between the dollars, verbatim — copy-as-markdown reads it back. */
  | { kind: 'math'; tex: string }
  | { kind: 'strong'; segments: InlineSegment[]; src: string }
  | { kind: 'em'; segments: InlineSegment[]; src: string }
  | { kind: 'code'; text: string; src: string }
  | { kind: 'br'; src: string }

/**
 * The `$…$` span opening at `open`, or null when there is none.
 *
 * Not a regex, because the closer is not simply the next dollar: **TeX nests math inside
 * text**, and a transcription of a struck-through equation is full of it —
 * `$\text{\sout{$E'_v = 1$}}$` is one formula, not two. So the span closes on the first
 * dollar at **brace depth zero**, and the dollars inside a `\text{…}` are its content.
 * Closing on the first dollar instead cut such a formula in half and handed KaTeX an
 * unbalanced fragment, which fell back to source characters and left the whole line reading
 * as raw TeX.
 *
 * The rest of the rules are the ones this parser always had: a span never crosses a backtick
 * (a rejected price before a code span — ``cost $5 and `$x$` `` — must not steal its closer
 * from inside the code, and TeX has no backticks to lose) or a line break, never holds
 * leading or trailing space, and never closes on a dollar followed by a digit, so
 * "$5 and $6" stays two prices.
 */
export function mathSpanAt(text: string, open: number): { tex: string; length: number } | null {
  let depth = 0
  for (let index = open + 1; index < text.length; index += 1) {
    const char = text[index]
    // An escaped character is content, whatever it is: `\$` is a dollar sign and `\{` is a
    // brace, and neither counts towards the nesting this scan is tracking.
    if (char === '\\') {
      index += 1
      continue
    }
    if (char === '`' || char === '\n') {
      return null
    }
    if (char === '{') {
      depth += 1
    } else if (char === '}') {
      depth = Math.max(0, depth - 1)
    } else if (char === '$' && depth === 0) {
      const tex = text.slice(open + 1, index)
      if (!tex || /^\s|\s$/.test(tex) || /\d/.test(text[index + 1] ?? '')) {
        return null
      }
      return { tex, length: index + 1 - open }
    }
  }
  return null
}

/** `<br>` in a table cell is a line break, not text — the one HTML tag transcriptions use. */
const BR_AT = /^<br\s*\/?>/i

/** An inline code span. Single backticks only — a fenced block never reaches this parser. */
const CODE_AT = /^`([^`\n]+)`/

/**
 * GFM's backslash escape: before an ASCII punctuation character a backslash makes that
 * character literal **and does not print itself** — `\[87]` in a reference list is `[87]`.
 * Before anything else (`C:\path`, a stray `\n`) it is an ordinary backslash and stays.
 */
export const ESCAPABLE = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/

// Emphasis opens on a marker followed by non-space and closes on non-space + marker, per
// GFM's flanking idea reduced to what transcriptions produce. The asterisk forms allow
// inner single `*`/`_` so `**a*b**` survives; the underscore forms additionally require a
// non-word character on both outsides — word in the Unicode sense, so `usage_log`,
// `café_value` and `測試_變數` all stay text.
// A closer preceded by an *unescaped* backslash is escaped and cannot close a span —
// `(?<!(?<!\\)\\)` on each closing marker, so `*a\*` stays literal while `*path\\*`
// (the backslash escapes itself) closes. Runs of three or more backslashes are beyond
// what a lookbehind can count and beyond what transcriptions produce. Every interior
// admits a complete `` `code` `` span as one unit (the leading alternative in each
// group), so a delimiter *inside* code — `*use \`a*b\` now*` — cannot close the
// emphasis around it, mirroring how the math scan protects code spans.
export const WORD_CHAR = /[\p{L}\p{N}_]/u
/** `***text***` is em(strong(text)) — matched before the pair forms, which would
 *  otherwise close two asterisks early and strand the third. */
const TRIPLE_AST_AT = /^\*\*\*(?![\s*])((?:`[^`\n]*`|[^*\n])+?)(?<=\S)(?<!(?<!\\)\\)\*\*\*(?!\*)/
const STRONG_AST_AT = /^\*\*(?!\s)((?:`[^`\n]*`|[^*\n]|\*(?!\*))+?)(?<=\S)(?<!(?<!\\)\\)\*\*/
/** An em's interior admits `**` pairs — `*outer **inner** text*` nests — but never a
 *  bare single `*`, which would be indistinguishable from its own closer. */
const EM_AST_AT = /^\*(?![\s*])((?:`[^`\n]*`|[^*\n]|\*\*(?!\*))+?)(?<=\S)(?<!(?<!\\)\\)\*(?!\*)/
const STRONG_UND_AT = /^__(?!\s)((?:`[^`\n]*`|[^_\n]|_(?!_))+?)(?<=\S)(?<!(?<!\\)\\)__(?![\p{L}\p{N}_])/u
const EM_UND_AT = /^_(?![\s_])((?:`[^`\n]*`|[^_\n])+?)(?<=\S)(?<!(?<!\\)\\)_(?![\p{L}\p{N}_])/u

/**
 * A marker at `index` is escaped only when an *odd* run of backslashes precedes it: in
 * `\*word*` the asterisk is literal, but in `\\*word*` the first backslash escapes the
 * second and the asterisk is a live delimiter.
 */
export function isEscaped(text: string, index: number): boolean {
  let count = 0
  while (index - 1 - count >= 0 && text[index - 1 - count] === '\\') {
    count += 1
  }
  return count % 2 === 1
}

/**
 * Parses the styled-inline shapes — emphasis, code, `<br>` — inside one math-free run of
 * text. Recursive for nesting (`_**Abstract**_` is an `em` holding a `strong`); anything
 * that fails a marker's rules stays literal text, which is also the wrong-guess fallback:
 * visible markers beat text silently bolded.
 */
function parseStyled(text: string): InlineSegment[] {
  const segments: InlineSegment[] = []
  let cursor = 0
  let scan = 0

  /** Merged into the run before it when there is one: an escape splits a paragraph mid-word
   *  (`\[87]` between two plain runs), and three text segments where one will do is three
   *  DOM nodes a selection has to be stitched back across. */
  function pushText(value: string): void {
    if (!value) {
      return
    }
    const last = segments[segments.length - 1]
    if (last?.kind === 'text') {
      last.text += value
      return
    }
    segments.push({ kind: 'text', text: value })
  }

  function flushText(end: number): void {
    if (end > cursor) {
      pushText(text.slice(cursor, end))
    }
  }

  function push(segment: InlineSegment, length: number): void {
    flushText(scan)
    segments.push(segment)
    cursor = scan + length
    scan = cursor
  }

  while (scan < text.length) {
    const char = text[scan]
    if (char !== '*' && char !== '_' && char !== '`' && char !== '<' && char !== '\\') {
      scan += 1
      continue
    }
    // An escape is consumed here, which is also why nothing below re-checks for one: a
    // backslash and the punctuation it escapes are taken as a pair, so the scan can never
    // land on a marker that still has a live backslash in front of it. `\*literal\*` is
    // the two asterisks as text, and `\\*word*` is one backslash and a real emphasis.
    if (char === '\\') {
      const escaped = text[scan + 1] ?? ''
      if (ESCAPABLE.test(escaped)) {
        flushText(scan)
        pushText(escaped)
        cursor = scan + 2
        scan = cursor
        continue
      }
      scan += 1
      continue
    }
    const rest = text.slice(scan)
    if (char === '<') {
      const br = BR_AT.exec(rest)
      if (br) {
        push({ kind: 'br', src: br[0] }, br[0].length)
        continue
      }
      scan += 1
      continue
    }
    if (char === '`') {
      const code = CODE_AT.exec(rest)
      if (code) {
        push({ kind: 'code', text: code[1] ?? '', src: code[0] }, code[0].length)
        continue
      }
      scan += 1
      continue
    }
    // `***text***` reads as em holding strong, the way GFM resolves the triple run.
    if (char === '*') {
      const triple = TRIPLE_AST_AT.exec(rest)
      if (triple) {
        const content = triple[1] ?? ''
        push(
          {
            kind: 'em',
            segments: [
              { kind: 'strong', segments: parseStyled(content), src: `**${content}**` },
            ],
            src: triple[0],
          },
          triple[0].length,
        )
        continue
      }
    }
    // Underscores open emphasis only off a word: mid-identifier they are identifier —
    // in any script, so `café_value` and `測試_變數` stay text (\p{L}, not [A-Za-z]).
    const wordBefore = scan > 0 && WORD_CHAR.test(text[scan - 1] ?? '')
    const strong = char === '*' ? STRONG_AST_AT.exec(rest) : wordBefore ? null : STRONG_UND_AT.exec(rest)
    if (strong) {
      push({ kind: 'strong', segments: parseStyled(strong[1] ?? ''), src: strong[0] }, strong[0].length)
      continue
    }
    const em = char === '*' ? EM_AST_AT.exec(rest) : wordBefore ? null : EM_UND_AT.exec(rest)
    if (em) {
      push({ kind: 'em', segments: parseStyled(em[1] ?? ''), src: em[0] }, em[0].length)
      continue
    }
    scan += 1
  }
  flushText(text.length)
  return segments
}

/**
 * Splits one line of prose (a paragraph, heading, list item, table cell or caption) into
 * plain text, math spans and styled-inline segments.
 *
 * Math is split first, and scanned dollar by dollar rather than with one global match on
 * purpose: a rejected candidate (a price, a span padded with spaces) resumes just past its
 * *opening* dollar — a global match would have consumed the next span's opener as its own
 * closer, so "cost $5 and variable $x$" would lose the real formula. Splitting math first
 * also keeps `$a_{1}$` out of the underscore-emphasis rules; the cost is that emphasis
 * *around* a math span stays source characters, which is the rarer shape.
 */
export function parseInline(text: string): InlineSegment[] {
  const segments: InlineSegment[] = []
  let cursor = 0
  let scan = 0

  function flushStyled(end: number): void {
    if (end > cursor) {
      segments.push(...parseStyled(text.slice(cursor, end)))
    }
  }

  while (scan < text.length) {
    const open = text.indexOf('$', scan)
    // A code span protects its contents: in "write `$x$` literally" the dollars are code,
    // not math, so a complete span is stepped over whole — parseStyled reads it later from
    // the same text. An unclosed backtick protects nothing, and an escaped one is a
    // literal character (the same rule parseStyled applies), so it must not open a span
    // here and swallow a real formula behind it.
    const tick = text.indexOf('`', scan)
    if (tick !== -1 && (open === -1 || tick < open)) {
      const code = isEscaped(text, tick) ? null : CODE_AT.exec(text.slice(tick))
      scan = tick + (code ? code[0].length : 1)
      continue
    }
    if (open === -1) {
      break
    }
    // A backslash-escaped dollar is a dollar sign, never an opener — the same rule
    // `parseStyled` applies when it renders `\$` as `$`.
    if (isEscaped(text, open)) {
      scan = open + 1
      continue
    }
    const match = mathSpanAt(text, open)
    if (!match) {
      scan = open + 1
      continue
    }
    flushStyled(open)
    segments.push({ kind: 'math', tex: match.tex })
    cursor = open + match.length
    scan = cursor
  }
  flushStyled(text.length)
  if (segments.length === 0) {
    segments.push({ kind: 'text', text: '' })
  }
  return segments
}
