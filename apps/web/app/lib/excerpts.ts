/**
 * Excerpts: passages of the document quoted in the notes, and the links between the two
 * (docs/web.md § Notes, *Excerpts*, *Locate*, *Backlink markers*).
 *
 * An excerpt is a quote of the passage followed by a paragraph that is only a section link —
 * the section the passage is in. Everything here is pure: the excerpt's markdown, where it
 * goes in the notes, whether it is there already, where its passage is in the document, and
 * which blocks carry a marker. A passage is compared by its text — markdown removed, formulas
 * as their TeX, spacing ignored — the same way everywhere, so a marker, a locate and a
 * duplicate check never disagree about whether a passage is there.
 */
import {
  blockPlain,
  decodeBlockAnchor,
  findHeading,
  headingKey,
  inlinePlain,
  isBefore,
  nearestBlock,
  parseInline,
  parseResultMarkdown,
  passageKey,
  placedBlocks,
  resolveAnchor,
  sectionExtent,
  type BlockAt,
  type OutlineEntry,
  type PlaceNear,
  type PlacedBlock,
  type ResultBlock,
  type ResultPageBlocks,
} from './markdown'
import { quoteLines } from './richdoc'

/** The `##` headings an excerpt section may carry, compared with case and spacing ignored:
 *  a heading in the other language is still the reader's. */
export const EXCERPT_HEADINGS = ['excerpts', '摘錄']

function blocksOf(markdown: string): ResultBlock[] {
  return parseResultMarkdown(markdown).flatMap((page) => page.blocks)
}

/** A passage's text as it is compared. */
export function passageText(markdown: string): string {
  return passageKey(
    blocksOf(markdown)
      .map(blockPlain)
      .filter(Boolean)
      .join(' '),
  )
}

/**
 * The passage as an excerpt holds it: every quote in it unwrapped, every figure or image given
 * as its caption and one without left out, so the excerpt stays one level and holds only text.
 * Null when nothing is left to quote — a passage that is only a captionless figure.
 */
export function excerptPassage(markdown: string): string | null {
  const parts: string[] = []
  const take = (block: ResultBlock) => {
    if (block.kind === 'quote') {
      block.blocks.forEach(take)
    } else if (block.kind === 'fig' || block.kind === 'img') {
      if (block.caption) {
        parts.push(block.caption)
      }
    } else if (block.kind !== 'comment') {
      parts.push(block.src)
    }
  }
  blocksOf(markdown).forEach(take)
  const passage = parts.join('\n\n').trim()
  return passage ? passage : null
}

/**
 * Blocks dragged from the document, as the notes take them (docs/web.md § Notes, *A block
 * dragged into the notes lands as blocks*): every block as it was — a figure and an image
 * whole, drawn in the notes as they are in the document, a quote included — and a page marker
 * not at all. Null when nothing is left.
 */
export function droppedBlocksMarkdown(markdown: string): string | null {
  const parts: string[] = []
  for (const block of blocksOf(markdown)) {
    if (block.kind !== 'comment') {
      parts.push(block.src)
    }
  }
  const dropped = parts.join('\n\n').trim()
  return dropped ? dropped : null
}

/**
 * An answer's block put at the end of the notes (docs/web.md § The agent panel, *An answer is
 * blocks*): taken as a drop takes it, after the last block with a blank line between, the
 * notes' trailing whitespace kept after it. `at` is where it starts, `count` how many blocks
 * it is. Null when nothing is left to add.
 */
export function appendBlocks(notes: string, markdown: string): { notes: string; at: number; count: number } | null {
  const blocks = droppedBlocksMarkdown(markdown)
  if (blocks === null) {
    return null
  }
  const body = notes.replace(/\s+$/, '')
  const lead = body ? `${body}\n\n` : ''
  return { notes: `${lead}${blocks}${notes.slice(body.length)}`, at: lead.length, count: blocksOf(blocks).length }
}

/**
 * Blocks put into the notes at a place held by its neighbours (docs/web.md § Editing,
 * *Images*): before or after the block `nearestBlock` finds, one blank line on each side, the
 * rest of the text kept as written — at the end, as `appendBlocks` puts them, when neither
 * neighbour is there. Null when nothing is left to add.
 */
export function insertNoteBlocks(notes: string, near: PlaceNear, markdown: string): string | null {
  const blocks = droppedBlocksMarkdown(markdown)
  if (blocks === null) {
    return null
  }
  const all = located(notes)
  const found = nearestBlock(all.map((entry) => entry.block.src), near)
  if (!found) {
    return appendBlocks(notes, blocks)?.notes ?? null
  }
  const entry = all[found.index]!
  const at = found.place === 'after' ? entry.end : entry.start
  const head = notes.slice(0, at).replace(/\s+$/, '')
  const rest = notes.slice(at)
  const tail = rest.replace(/^\s+/, '')
  return `${head ? `${head}\n\n` : ''}${blocks}${tail ? `\n\n${tail}` : rest}`
}

/** A markdown link to an address, `[label](https://…)`: the label free of bare brackets as
 *  `CITE_AT` reads a section link's, the address free of spaces, its parentheses balanced one
 *  level deep (`…block=a~Let+(x)+be`), as a markdown link's destination may hold them. */
const ADDRESS_LINK = /\[((?:\\[\s\S]|[^[\]\\\n])+)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/g

/** Code in markdown, where nothing is a link: a fenced block — backticks or tildes, as
 *  `CODE_FENCE` opens one — then a span on one line. */
const CODE_REGIONS = /(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`)/

/**
 * Block references pasted into the notes (docs/web.md § Notes, *Quotes and section links*):
 * each markdown link whose address `anchorOf` knows as this document's — it answers the
 * block anchor the address carries, or null — becomes the section link `[label](#anchor)`,
 * the anchor escaped so it reads as one (`CITE_AT` allows no spaces or parentheses in it).
 * Any other link stays as written, and so does code — a fenced block, a span — and a link
 * whose bracket is escaped: those are the text they show.
 */
export function rewriteBlockLinks(markdown: string, anchorOf: (address: string) => string | null): string {
  return markdown
    .split(CODE_REGIONS)
    .map((part, index) =>
      index % 2
        ? part
        : part.replace(ADDRESS_LINK, (whole, label: string, address: string, offset: number) => {
            const slashes = /\\*$/.exec(part.slice(0, offset))?.[0].length ?? 0
            const anchor = slashes % 2 ? null : anchorOf(address)
            if (anchor === null) {
              return whole
            }
            const escaped = encodeURIComponent(decodeBlockAnchor(anchor)).replace(/\(/g, '%28').replace(/\)/g, '%29')
            return `[${label}](#${escaped})`
          }),
    )
    .join('')
}

/** The section a passage is in, as its excerpt's link names it; null before the first heading. */
export interface ExcerptSection {
  /** The heading's markdown as the document has it. */
  text: string
  slug: string
}

/** Brackets in a link's text would end it early: they are escaped, which reads the same. */
function linkText(text: string): string {
  return text.replace(/[[\]]/g, (bracket) => `\\${bracket}`)
}

/**
 * An excerpt's markdown: the passage as a quote, then its section's link — `[3.2 Attention](#32-attention)`,
 * the shape answers use — or, before the first heading, `[Beginning of the document](#)`.
 */
export function excerptMarkdown(passage: string, section: ExcerptSection | null, beginning: string): string {
  const link = section ? `[${linkText(section.text)}](#${section.slug})` : `[${linkText(beginning)}](#)`
  return `${quoteLines(passage)}\n\n${link}`
}

/** Each top-level block of a markdown file with where its source starts and ends. */
function located(markdown: string): { block: ResultBlock; start: number; end: number }[] {
  const out: { block: ResultBlock; start: number; end: number }[] = []
  let cursor = 0
  for (const block of blocksOf(markdown)) {
    const start = markdown.indexOf(block.src, cursor)
    if (start === -1) {
      continue
    }
    out.push({ block, start, end: start + block.src.length })
    cursor = start + block.src.length
  }
  return out
}

/** Whether a block is a `##` heading reading *Excerpts* (or *摘錄*). */
function isExcerptHeading(block: ResultBlock): boolean {
  return block.kind === 'h2' && block.level === 2 && EXCERPT_HEADINGS.includes(headingKey(inlinePlain(block.text)))
}

/** Where an excerpt went: the notes it made, and where in them it starts. `duplicate` says it
 *  was there already and nothing was added — `at` is then that one. */
export interface AppendedExcerpt {
  notes: string
  at: number
  duplicate: boolean
}

/**
 * The notes with an excerpt added (docs/web.md § Notes, *Where it goes*, *Never twice*): at
 * the end of the notes' Excerpts section — a `##` heading in either language, to the next
 * heading of the same or a higher level — or under a new `heading` at the end of the notes.
 * A passage whose whole text equals a quote already there is not added again.
 */
export function appendExcerpt(notes: string, passage: string, excerpt: string, heading: string): AppendedExcerpt {
  const blocks = located(notes)
  const key = passageText(passage)
  const existing = blocks.find((entry) => entry.block.kind === 'quote' && passageText(quoteContent(entry.block)) === key)
  if (existing && key) {
    return { notes, at: existing.start, duplicate: true }
  }
  const section = blocks.findIndex((entry) => isExcerptHeading(entry.block))
  if (section !== -1) {
    let last = section
    while (last + 1 < blocks.length) {
      const next = blocks[last + 1]!.block
      if ((next.kind === 'h2' || next.kind === 'h3') && next.level <= 2) {
        break
      }
      last += 1
    }
    const end = blocks[last]!.end
    return { notes: `${notes.slice(0, end)}\n\n${excerpt}${notes.slice(end)}`, at: end + 2, duplicate: false }
  }
  const body = notes.replace(/\s+$/, '')
  const trailing = notes.slice(body.length)
  const lead = body ? `${body}\n\n` : ''
  const added = `${lead}${heading}\n\n${excerpt}`
  return { notes: `${added}${trailing}`, at: lead.length + heading.length + 2, duplicate: false }
}

/** A quote block's content as markdown: its lines with `>` and one space removed. */
function quoteContent(block: Extract<ResultBlock, { kind: 'quote' }>): string {
  return block.src
    .split('\n')
    .map((line) => line.replace(/^\s*> ?/, ''))
    .join('\n')
}

// --- finding a passage ------------------------------------------------------------------------

/** A run of text in the order the reader reads it — a text node's, or a formula's TeX. */
export interface PassageHit {
  /** Indices into the parts, and offsets into those parts' text; `end` is exclusive. */
  start: { part: number; offset: number }
  end: { part: number; offset: number }
}

/**
 * Where a passage's text runs through a sequence of text parts, spacing ignored — the text
 * walk locate makes over the rendered blocks, with each formula a part holding its TeX
 * (`findPassage`, docs/web.md § Notes, *Locate*). Null when the parts do not hold it.
 */
export function findPassage(parts: readonly string[], passage: string): PassageHit | null {
  const key = passageKey(passage)
  if (!key) {
    return null
  }
  let text = ''
  const map: { part: number; offset: number }[] = []
  parts.forEach((value, part) => {
    for (let offset = 0; offset < value.length; offset += 1) {
      const char = value[offset]!
      if (!/\s/.test(char)) {
        text += char
        map.push({ part, offset })
      }
    }
  })
  const at = text.indexOf(key)
  if (at === -1) {
    return null
  }
  const last = map[at + key.length - 1]!
  return { start: map[at]!, end: { part: last.part, offset: last.offset + 1 } }
}

// --- excerpts and the document --------------------------------------------------------------

/** One excerpt in the notes, matched against the document. */
export interface ExcerptMatch {
  /** Where its quote starts in the notes. */
  at: number
  /** The quote's markdown. */
  passage: string
  label: string
  anchor: string
  /** The blocks its passage starts and ends in, or null when the document no longer holds it. */
  found: { start: BlockAt; end: BlockAt } | null
  /** The heading its link names, when the document still has it. */
  heading: OutlineEntry | null
}

/** The one section link a paragraph is, or null: an excerpt's link line. */
function linkLine(block: ResultBlock): { label: string; anchor: string } | null {
  if (block.kind !== 'p') {
    return null
  }
  const segments = parseInline(block.text, { cites: true }).filter((segment) => segment.kind !== 'text' || segment.text.trim())
  const only = segments.length === 1 ? segments[0] : undefined
  return only?.kind === 'cite' ? { label: only.label, anchor: only.anchor } : null
}

/** The document as a passage is searched: its blocks in reading order, each as its text compared
 *  — read once for every excerpt the notes hold, not once per excerpt. */
interface PassageIndex {
  placed: PlacedBlock[]
  keys: string[]
}

function passageIndex(pages: readonly ResultPageBlocks[]): PassageIndex {
  const placed = placedBlocks(pages)
  return { placed, keys: placed.map((entry) => (entry.block.kind === 'comment' ? '' : passageKey(blockPlain(entry.block)))) }
}

function locateIn(index: PassageIndex, passage: string, heading: OutlineEntry | null): { start: BlockAt; end: BlockAt } | null {
  const key = passageText(passage)
  if (!key) {
    return null
  }
  const { placed, keys } = index
  const search = (from: number, to: number) => {
    let text = ''
    const owner: number[] = []
    for (let block = from; block < to; block += 1) {
      text += keys[block]!
      for (let char = 0; char < keys[block]!.length; char += 1) {
        owner.push(block)
      }
    }
    const at = text.indexOf(key)
    if (at === -1) {
      return null
    }
    const first = placed[owner[at]!]!
    const last = placed[owner[at + key.length - 1]!]!
    return { start: { page: first.page, index: first.index }, end: { page: last.page, index: last.index } }
  }
  if (heading) {
    const at = placed.findIndex((entry) => entry.page === heading.page && entry.index === heading.index)
    if (at !== -1) {
      const { end } = sectionExtent(placed, at)
      const found = search(at, end)
      if (found) {
        return found
      }
    }
  }
  return search(0, placed.length)
}

/**
 * Where a passage is in the document: within the linked section first, then anywhere — the
 * first match in reading order, which is also the block a marker sits on. `heading` null with
 * an empty anchor is the whole document (`[Beginning of the document](#)`).
 */
export function locatePassage(
  pages: readonly ResultPageBlocks[],
  passage: string,
  heading: OutlineEntry | null,
): { start: BlockAt; end: BlockAt } | null {
  return locateIn(passageIndex(pages), passage, heading)
}

/**
 * Every excerpt in the notes — a quote right before a paragraph that is only a section link —
 * matched against the document (`matchExcerpts`, docs/web.md § Notes, *Backlink markers*):
 * where its passage is, by the comparison locate makes, and whether its heading is still there.
 */
export function matchExcerpts(notes: string, pages: readonly ResultPageBlocks[], outline: readonly OutlineEntry[]): ExcerptMatch[] {
  const blocks = located(notes)
  const matches: ExcerptMatch[] = []
  let index_: PassageIndex | null = null
  blocks.forEach((entry, index) => {
    const next = blocks[index + 1]
    if (entry.block.kind !== 'quote' || !next) {
      return
    }
    const link = linkLine(next.block)
    if (!link) {
      return
    }
    const passage = quoteContent(entry.block)
    const heading = link.anchor === '' ? null : findHeading(outline, link.label, link.anchor)
    index_ ??= passageIndex(pages)
    matches.push({ at: entry.start, passage, ...link, heading, found: locateIn(index_, passage, heading) })
  })
  return matches
}

/**
 * The markers the matches put on the document: each excerpt whose passage is found, on the
 * block its passage ends in, by `page-index`, in the notes' order — a block quoted by several
 * leads to the first, then the next.
 */
export function excerptMarkers(matches: readonly ExcerptMatch[]): Map<string, ExcerptMatch[]> {
  const markers = new Map<string, ExcerptMatch[]>()
  for (const match of matches) {
    if (match.found) {
      const key = `${match.found.end.page}-${match.found.end.index}`
      markers.set(key, [...(markers.get(key) ?? []), match])
    }
  }
  return markers
}

/** Whether a section link in the notes leads anywhere: an excerpt's while its passage or its
 *  heading is found, a block reference's while its anchor resolves, any other while its
 *  heading is found, and `#` — the whole document — always. */
export function linkResolves(
  pages: readonly ResultPageBlocks[],
  outline: readonly OutlineEntry[],
  link: { label: string; anchor: string; passage: string | null },
): boolean {
  if (link.anchor === '') {
    return true
  }
  if (link.anchor.includes('~')) {
    // A block reference: it leads wherever its anchor resolves to.
    return resolveAnchor(pages, decodeBlockAnchor(link.anchor), outline) !== null
  }
  const heading = findHeading(outline, link.label, link.anchor)
  return heading !== null || (link.passage !== null && locatePassage(pages, link.passage, null) !== null)
}

/** The section a block sits in, as an excerpt of it names: the last heading before it — or
 *  the block itself, when it is a heading. */
export function sectionOf(outline: readonly OutlineEntry[], at: BlockAt): OutlineEntry | null {
  let found: OutlineEntry | null = null
  for (const entry of outline) {
    if (entry.page === at.page && entry.index === at.index) {
      return entry
    }
    if (!isBefore(entry, at)) {
      break
    }
    found = entry
  }
  return found
}
