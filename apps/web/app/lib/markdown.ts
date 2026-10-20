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
  /** `page` is the one its placeholder names (`sightread://pN/…`), not the page the block
   *  sits in: blocks move across page markers, and the crop stays where it was cut
   *  (docs/web.md § Result viewer, *Figures*). */
  | { kind: 'fig'; id: string; page: number; bbox: Bbox; caption: string | null; src: string }
  | { kind: 'toc'; entries: TocEntry[]; src: string }
  /** A run of `>` lines, its content read as blocks of their own — one level: a `>` inside
   *  is that quote's text (docs/web.md § Result viewer, *Blockquotes*). */
  | { kind: 'quote'; blocks: ResultBlock[]; src: string }
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

/** A quote's line: `>` first. Inside a quote it is text — quotes are one level deep. */
function isQuoteLine(line: string, quoted: boolean): boolean {
  return !quoted && line.startsWith('>')
}

/**
 * Every line that starts a block of its own — so a paragraph knows where to stop. `next` is
 * the line after it, which only the table check needs; `quoted` reads a quote's content,
 * where a `>` is text.
 */
function startsBlock(line: string, next = '', quoted = false): boolean {
  return (
    !line ||
    isQuoteLine(line, quoted) ||
    opensMath(line) ||
    CODE_FENCE.test(line) ||
    isTableStart(line, next) ||
    line.startsWith('#') ||
    LIST_ITEM.test(line) ||
    (!quoted && FIGURE.test(line)) ||
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
  return parsePages(markdown, false)
}

/** A quote's content as blocks: no page of its own, and a `>` in it stays text. */
function parseQuoted(content: string): ResultBlock[] {
  return parsePages(content, true).flatMap((page) => page.blocks)
}

/** `>` and one space after it, off a quote's line. */
const QUOTE_MARKER = /^\s*> ?/

function parsePages(markdown: string, quoted: boolean): ResultPageBlocks[] {
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

    // Inside a quote a page marker is the quote's own comment: a quote holds no pages.
    const marker = quoted ? null : PAGE_MARKER.exec(line)
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
    if (isQuoteLine(line, quoted)) {
      // Every line starting with `>` up to the first that does not: a blank line or any other
      // line ends the quote, so two quotes a blank line apart stay two.
      const content: string[] = []
      let cursor = index
      while (cursor < lines.length && isQuoteLine((lines[cursor] ?? '').trim(), false)) {
        content.push((lines[cursor] ?? '').replace(QUOTE_MARKER, ''))
        cursor += 1
      }
      index = cursor - 1
      page().blocks.push({ kind: 'quote', blocks: parseQuoted(content.join('\n')), src: source(first, index) })
      continue
    }
    if (COMMENT.test(line)) {
      // Not a page marker, but the file's: a block of its own so an edit elsewhere
      // leaves it in the source (docs/web.md § Editing).
      page().blocks.push({ kind: 'comment', src: source(first, index) })
      continue
    }

    // Inside a quote a placeholder is a line of text: a quote holds only what reads as text
    // (docs/web.md § Notes, *Excerpts*), and a crop has no place in one.
    const figure = quoted ? null : FIGURE.exec(line)
    if (figure) {
      const target = page()
      // The caption is written verbatim on the next line, and only there — anything that
      // starts another block is the next block, not this figure's caption.
      const next = (lines[index + 1] ?? '').trim()
      const captioned = Boolean(next) && !startsBlock(next, (lines[index + 2] ?? '').trim(), quoted)
      if (captioned) {
        index += 1
      }
      target.blocks.push({
        kind: 'fig',
        id: figure[1] || `fig${target.blocks.filter((block) => block.kind === 'fig').length + 1}`,
        page: Number(figure[2]),
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
          !startsBlock(trimmed, (lines[cursor + 1] ?? '').trim(), quoted)
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
      if (startsBlock(next, (lines[cursor + 1] ?? '').trim(), quoted)) {
        break
      }
      paragraph.push(next)
      cursor += 1
    }
    index = cursor - 1
    // A contents page keeps its lines: joined, a book's `1.1 Title . . . . 13` rows read
    // as one run-on paragraph of dots (docs/web.md § Result viewer).
    const entries = quoted ? null : tocEntries(paragraph)
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
// between them is parsed for strong/em, inline code, `<br>` line breaks and `<sup>`/`<sub>`.
// Each styled segment keeps its exact source (`tex`, `src`), which is what copy-as-markdown
// reads back.

export type InlineSegment =
  | { kind: 'text'; text: string }
  /** `tex` is the source between the dollars, verbatim — copy-as-markdown reads it back. */
  | { kind: 'math'; tex: string }
  | { kind: 'strong'; segments: InlineSegment[]; src: string }
  | { kind: 'em'; segments: InlineSegment[]; src: string }
  | { kind: 'sup'; segments: InlineSegment[]; src: string }
  | { kind: 'sub'; segments: InlineSegment[]; src: string }
  | { kind: 'code'; text: string; src: string }
  | { kind: 'br'; src: string }
  /** An answer's link to a section of the document — `[3.2 Attention](#32-attention)`,
   *  or with `doc.md` before the `#` — read only when the caller asks for it (`cites`,
   *  docs/web.md § The agent panel, *Section links*). `segments` is its text as drawn,
   *  `label` that text with the markdown removed, `anchor` what follows the `#`. */
  | { kind: 'cite'; segments: InlineSegment[]; label: string; anchor: string; src: string }

/** Whether a section link's heading is still in the document: a link that answers yes is
 *  drawn as a chip, one that answers no as its plain text. */
export type CiteResolver = (label: string, anchor: string) => boolean

/** What `parseInline` reads beyond the document's own shapes. */
export interface InlineOptions {
  /** Section links, which only an answer of the agent panel writes; everywhere else a
   *  link stays the text it was (docs/web.md § Result viewer, *Inline markdown*). */
  cites?: boolean
}

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

/** `<br>` in a table cell is a line break, not text. */
const BR_AT = /^<br\s*\/?>/i

/**
 * `<sup>…</sup>` / `<sub>…</sub>`: GFM has no superscript syntax, so a transcription that does
 * not write `$^{1}$` writes the tag GitHub renders — an author's affiliation mark, a chemical
 * index. Bare tags only: these and `<br>` are the only HTML read, every other tag stays text.
 */
const SCRIPT_AT = /^<(sup|sub)>([^\n]+?)<\/\1>/i

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
 * A section link: a non-blank text in brackets — escaped brackets and code allowed inside,
 * bare ones not — then `(#anchor)` or `(doc.md#anchor)`, the anchor free of spaces and
 * parentheses. Any other target (`https://…`, `notes.md#x`, `#a b`) is not one.
 */
const CITE_AT = /^\[((?:\\[\s\S]|[^[\]\\\n])+)\]\((?:doc\.md)?#([^\s()]*)\)/

/** The section link opening at `index`, or null. Its text is read like any other inline
 *  run — math and emphasis drawn — but holds no link of its own. */
function citeAt(text: string, index: number): { segment: InlineSegment; length: number } | null {
  const match = CITE_AT.exec(text.slice(index))
  const label = match?.[1] ?? ''
  if (!match || !label.trim()) {
    return null
  }
  const segments = parseInline(label)
  return {
    segment: { kind: 'cite', segments, label: plainText(segments).replace(/\s+/g, ' ').trim(), anchor: match[2] ?? '', src: match[0] },
    length: match[0].length,
  }
}

/**
 * Parses the styled-inline shapes — emphasis, code, `<br>`, `<sup>`/`<sub>` and, when asked,
 * section links — inside one math-free run of text. Recursive for nesting (`_**Abstract**_`
 * is an `em` holding a `strong`); anything that fails a marker's rules stays literal text,
 * which is also the wrong-guess fallback: visible markers beat text silently bolded.
 */
function parseStyled(text: string, options?: InlineOptions): InlineSegment[] {
  const cites = options?.cites === true
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
    if (char !== '*' && char !== '_' && char !== '`' && char !== '<' && char !== '\\' && !(cites && char === '[')) {
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
    if (char === '[') {
      const cite = citeAt(text, scan)
      if (cite) {
        push(cite.segment, cite.length)
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
      const script = SCRIPT_AT.exec(rest)
      if (script) {
        const kind = script[1]?.toLowerCase() === 'sub' ? 'sub' : 'sup'
        push({ kind, segments: parseStyled(script[2] ?? '', options), src: script[0] }, script[0].length)
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
              { kind: 'strong', segments: parseStyled(content, options), src: `**${content}**` },
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
      push({ kind: 'strong', segments: parseStyled(strong[1] ?? '', options), src: strong[0] }, strong[0].length)
      continue
    }
    const em = char === '*' ? EM_AST_AT.exec(rest) : wordBefore ? null : EM_UND_AT.exec(rest)
    if (em) {
      push({ kind: 'em', segments: parseStyled(em[1] ?? '', options), src: em[0] }, em[0].length)
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
 *
 * `cites` reads section links as well (`InlineOptions`); without it a link is the text it
 * always was.
 */
export function parseInline(text: string, options?: InlineOptions): InlineSegment[] {
  const segments: InlineSegment[] = []
  let cursor = 0
  let scan = 0

  function flushStyled(end: number): void {
    if (end > cursor) {
      segments.push(...parseStyled(text.slice(cursor, end), options))
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
    // A section link before the next dollar is stepped over whole too: its text may hold
    // math of its own (`[3.1 The $\epsilon$-greedy policy](#…)`), which parseStyled reads
    // with the link rather than splitting the link around it.
    const bracket = options?.cites && open !== -1 ? text.indexOf('[', scan) : -1
    if (bracket !== -1 && bracket < open && (tick === -1 || bracket < tick)) {
      const cite = isEscaped(text, bracket) ? null : citeAt(text, bracket)
      scan = bracket + (cite ? cite.length : 1)
      continue
    }
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

/** Inline segments as the text they draw: markdown removed, a formula as its TeX. What a
 *  heading and a section link are compared by, and how a heading reads in a chip. */
export function plainText(segments: readonly InlineSegment[]): string {
  return segments
    .map((segment) => {
      switch (segment.kind) {
        case 'text':
        case 'code':
          return segment.text
        case 'math':
          return segment.tex
        case 'br':
          return ' '
        default:
          return plainText(segment.segments)
      }
    })
    .join('')
}

/** A line of inline markdown as plain text, its spacing collapsed. */
export function inlinePlain(text: string): string {
  return plainText(parseInline(text)).replace(/\s+/g, ' ').trim()
}

// --- the document's sections ----------------------------------------------------------------
//
// The agent panel adds a section to a message by its heading and draws an answer's link to
// one as a chip that scrolls the document there (docs/web.md § The agent panel). Both read
// the document's headings the same way, from the blocks the viewer renders.

/** One heading of the document, where it sits and the two ways a link can name it. */
export interface OutlineEntry {
  /** Indices into the parsed pages and that page's blocks. */
  page: number
  index: number
  /** The `#…` depth the source wrote. */
  level: number
  /** The heading's markdown as the document has it. */
  text: string
  /** Its text with the markdown removed. */
  plain: string
  /** `plain` with its spacing collapsed and lower-cased: what a link's text is matched by. */
  key: string
  /** Its GitHub-style anchor, a repeat numbered as GitHub numbers it (`intro-1`). */
  slug: string
}

/** How a heading's text is compared: spacing and case ignored. */
export function headingKey(plain: string): string {
  return plain.replace(/\s+/g, ' ').trim().toLowerCase()
}

/** GitHub's anchor for a heading: lower case, every character but letters, digits, `_`,
 *  `-` and spaces dropped, each space a hyphen. */
export function headingSlug(plain: string): string {
  return plain
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}\p{Pc}\- ]/gu, '')
    .replace(/ /g, '-')
}

/** Every heading of the document in reading order, page markers crossed. */
export function documentOutline(pages: readonly ResultPageBlocks[]): OutlineEntry[] {
  const outline: OutlineEntry[] = []
  const repeats = new Map<string, number>()
  pages.forEach((page, pageIndex) => {
    page.blocks.forEach((block, index) => {
      if (block.kind !== 'h2' && block.kind !== 'h3') {
        return
      }
      const plain = inlinePlain(block.text)
      const slug = headingSlug(plain)
      const seen = repeats.get(slug) ?? 0
      repeats.set(slug, seen + 1)
      outline.push({
        page: pageIndex,
        index,
        level: block.level,
        text: block.text,
        plain,
        key: headingKey(plain),
        slug: seen ? `${slug}-${seen}` : slug,
      })
    })
  })
  return outline
}

/** A link's anchor as a slug compares with one: percent-escapes read, lower case. */
function anchorKey(anchor: string): string {
  let decoded = anchor
  try {
    decoded = decodeURIComponent(anchor)
  } catch {
    // A stray `%` is part of the anchor as written.
  }
  return decoded.trim().toLowerCase()
}

/**
 * The heading a section link points to: the first whose text matches the link's text,
 * markdown removed and spacing and case ignored, else the one whose GitHub-style anchor is
 * the link's (docs/web.md § The agent panel, *Section links*). Null when the document no
 * longer has it — the link then reads as its plain text.
 */
export function findHeading(outline: readonly OutlineEntry[], label: string, anchor: string): OutlineEntry | null {
  const key = headingKey(label)
  const byText = key ? outline.find((entry) => entry.key === key) : undefined
  if (byText) {
    return byText
  }
  const slug = anchorKey(anchor)
  return (slug && outline.find((entry) => entry.slug === slug)) || null
}

/**
 * A whole section's markdown, as a message carries it (docs/chat.md § What a user message
 * carries): the heading's source through the last block before the next heading of the
 * same or a higher level (`#` count no deeper than its own), across page markers, blocks
 * joined by blank lines. The markers and the document's other comments are left out — they
 * are the file's bookkeeping, not the section. Null when the block is not a heading; how
 * long is too long is the caller's to decide.
 */
export function sectionSource(pages: readonly ResultPageBlocks[], pageIndex: number, blockIndex: number): string | null {
  const heading = pages[pageIndex]?.blocks[blockIndex]
  if (!heading || (heading.kind !== 'h2' && heading.kind !== 'h3')) {
    return null
  }
  const parts = [heading.src]
  for (let page = pageIndex; page < pages.length; page += 1) {
    const blocks = pages[page]?.blocks ?? []
    for (let index = page === pageIndex ? blockIndex + 1 : 0; index < blocks.length; index += 1) {
      const block = blocks[index]!
      if ((block.kind === 'h2' || block.kind === 'h3') && block.level <= heading.level) {
        return parts.join('\n\n')
      }
      if (block.kind !== 'comment' && block.src.trim()) {
        parts.push(block.src)
      }
    }
  }
  return parts.join('\n\n')
}

// --- a block's words ---------------------------------------------------------------------
//
// A block link names a block by its first words, locate finds a quoted passage by its text,
// and the viewer names a block by its words (docs/web.md § Files, § Notes, § Editing). All
// three read a block the same way: as the text it shows.

/** A block as the text it reads as: markdown removed, a formula as its TeX, a list's items, a
 *  table's cells and a quote's blocks one after another, spacing collapsed. A figure is its
 *  caption; a comment is nothing. */
export function blockPlain(block: ResultBlock): string {
  switch (block.kind) {
    case 'h2':
    case 'h3':
    case 'p':
      return inlinePlain(block.text)
    case 'list':
      return block.items.map(inlinePlain).join(' ')
    case 'math':
    case 'code':
      return block.text.replace(/\s+/g, ' ').trim()
    case 'table':
      return [block.header, ...block.rows]
        .flat()
        .map(inlinePlain)
        .filter(Boolean)
        .join(' ')
    case 'fig':
      return block.caption ? inlinePlain(block.caption) : ''
    case 'toc':
      return block.entries
        .map((entry) => [inlinePlain(entry.title), entry.page === null ? '' : inlinePlain(entry.page)].filter(Boolean).join(' '))
        .join(' ')
    case 'quote':
      return block.blocks.map(blockPlain).filter(Boolean).join(' ')
    case 'comment':
      return ''
  }
}

/** Text as a passage is compared: spacing ignored — a line wrapped differently, or a
 *  formula's TeX spaced differently, is the same passage. */
export function passageKey(text: string): string {
  return text.replace(/\s+/g, '')
}

/** The first words of a block's text, as a block link names it: at most `words` words, then
 *  at most `chars` characters (code points) — a sentence without spaces is cut by length. */
export function firstWords(text: string, words = 8, chars = 60): string {
  const cut = text.replace(/\s+/g, ' ').trim().split(' ').slice(0, words).join(' ')
  const points = [...cut]
  return (points.length > chars ? points.slice(0, chars).join('') : cut).trim()
}

// --- blocks as a run, across pages ------------------------------------------------------

/** Where a block sits: indices into the parsed pages and that page's blocks. */
export interface BlockAt {
  page: number
  index: number
}

/** A block with where it sits — the document in reading order, page markers crossed. */
export interface PlacedBlock extends BlockAt {
  block: ResultBlock
}

export function placedBlocks(pages: readonly ResultPageBlocks[]): PlacedBlock[] {
  return pages.flatMap((page, pageIndex) => page.blocks.map((block, index) => ({ page: pageIndex, index, block })))
}

/** Whether `a` comes before `b` in reading order. */
export function isBefore(a: BlockAt, b: BlockAt): boolean {
  return a.page < b.page || (a.page === b.page && a.index < b.index)
}

function isHeading(block: ResultBlock): block is Extract<ResultBlock, { kind: 'h2' | 'h3' }> {
  return block.kind === 'h2' || block.kind === 'h3'
}

/**
 * The blocks a heading's section holds, by their positions in `placedBlocks`: from the block
 * after the heading up to the last before the next heading of the same or a higher level —
 * the extent `sectionSource` reads and a collapsed heading hides. `[start, end)`.
 */
export function sectionExtent(placed: readonly PlacedBlock[], heading: number): { start: number; end: number } {
  const block = placed[heading]?.block
  const level = block && isHeading(block) ? block.level : 0
  let end = heading + 1
  while (end < placed.length) {
    const next = placed[end]!.block
    if (isHeading(next) && next.level <= level) {
      break
    }
    end += 1
  }
  return { start: heading + 1, end }
}

// --- moving blocks --------------------------------------------------------------------------

/** A move as it lands: the document, and where the moved run — trailing comments included —
 *  now sits. */
export interface MovedBlocks {
  markdown: string
  first: BlockAt
  last: BlockAt
}

/**
 * The document with the run of blocks from `from` to `to` — in reading order, page markers
 * crossed — moved before or after `target` (docs/web.md § Editing, *Moving blocks*). Parse,
 * splice, `serializeDocument`: everything not moved keeps its bytes, and the page markers stay
 * where they were, so a page a move empties keeps its marker and draws nothing.
 *
 * A comment travels with the block before it on its page: one right after the run goes with
 * it, and a move placed after `target` lands past the comments `target` carries. Null when the
 * positions are not a run, the target is inside it, or the move would change nothing.
 */
export function moveBlocks(
  markdown: string,
  run: { from: BlockAt; to: BlockAt },
  target: BlockAt,
  place: 'before' | 'after',
): MovedBlocks | null {
  const pages = parseResultMarkdown(markdown)
  const placed = placedBlocks(pages)
  const find = (at: BlockAt) => placed.findIndex((entry) => entry.page === at.page && entry.index === at.index)
  const first = find(run.from)
  let last = find(run.to)
  const aim = find(target)
  if (first === -1 || last < first || aim === -1) {
    return null
  }
  while (last + 1 < placed.length && placed[last + 1]!.block.kind === 'comment' && placed[last + 1]!.page === placed[last]!.page) {
    last += 1
  }
  if (aim >= first && aim <= last) {
    return null
  }
  const moving = placed.slice(first, last + 1).map((entry) => entry.block)
  const aimed = placed[aim]!.block
  const staying = new Set(moving)
  const rest = pages.map((page) => ({ page: page.page, blocks: page.blocks.filter((block) => !staying.has(block)) }))
  const host = rest.find((page) => page.blocks.includes(aimed))!
  let at = host.blocks.indexOf(aimed)
  if (place === 'after') {
    at += 1
    while (host.blocks[at]?.kind === 'comment') {
      at += 1
    }
  }
  host.blocks.splice(at, 0, ...moving)
  const next = serializeDocument(rest)
  if (next === serializeDocument(pages)) {
    return null
  }
  const page = rest.indexOf(host)
  return { markdown: next, first: { page, index: at }, last: { page, index: at + moving.length - 1 } }
}

// --- links to a block -------------------------------------------------------------------------

/**
 * A block's anchor, as *Copy link to block* writes it (docs/web.md § Files, *A block can be
 * linked to*): a heading's own slug; any other block's section slug — empty before the first
 * heading — then `~` and its first words. A block with no words of its own, a figure without a
 * caption, is its section. Not yet URL-encoded.
 */
export function blockAnchor(pages: readonly ResultPageBlocks[], at: BlockAt, outline = documentOutline(pages)): string {
  const block = pages[at.page]?.blocks[at.index]
  if (!block) {
    return ''
  }
  let section = ''
  for (const entry of outline) {
    if (entry.page === at.page && entry.index === at.index) {
      return entry.slug
    }
    if (!isBefore(entry, at)) {
      break
    }
    section = entry.slug
  }
  const words = firstWords(blockPlain(block))
  return words ? `${section}~${words}` : section
}

/**
 * Where a block link leads: the first block in its section whose text starts with its words,
 * else such a block anywhere, else the section's heading, else null — the top of the document.
 * Compared as a located passage is, spacing ignored; a link survives edits above it and a
 * block moved to another section.
 */
export function resolveAnchor(pages: readonly ResultPageBlocks[], anchor: string, outline = documentOutline(pages)): BlockAt | null {
  const tilde = anchor.indexOf('~')
  const slug = (tilde === -1 ? anchor : anchor.slice(0, tilde)).trim().toLowerCase()
  const key = tilde === -1 ? '' : passageKey(anchor.slice(tilde + 1))
  const placed = placedBlocks(pages)
  const heading = slug ? outline.find((entry) => entry.slug === slug) : undefined
  const headingAt = heading ? placed.findIndex((entry) => entry.page === heading.page && entry.index === heading.index) : -1
  const position = (index: number): BlockAt => ({ page: placed[index]!.page, index: placed[index]!.index })
  if (key) {
    const starts = (index: number) => {
      const block = placed[index]!.block
      return block.kind !== 'comment' && passageKey(blockPlain(block)).startsWith(key)
    }
    // The section first: after its heading to the next of its level, or — with no slug —
    // everything before the first heading.
    let start = -1
    let end = -1
    if (headingAt !== -1) {
      ;({ start, end } = sectionExtent(placed, headingAt))
    } else if (!slug) {
      start = 0
      end = placed.findIndex((entry) => isHeading(entry.block))
      end = end === -1 ? placed.length : end
    }
    for (let index = start; index !== -1 && index < end; index += 1) {
      if (starts(index)) {
        return position(index)
      }
    }
    for (let index = 0; index < placed.length; index += 1) {
      if (starts(index)) {
        return position(index)
      }
    }
  }
  return headingAt === -1 ? null : position(headingAt)
}
