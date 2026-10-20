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
  findHeading,
  headingKey,
  inlinePlain,
  isBefore,
  parseInline,
  parseResultMarkdown,
  passageKey,
  placedBlocks,
  sectionExtent,
  type BlockAt,
  type OutlineEntry,
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
 * The passage as an excerpt holds it: every quote in it unwrapped, every figure given as its
 * caption and one without left out, so the excerpt stays one level and holds only text. Null
 * when nothing is left to quote — a passage that is only a captionless figure.
 */
export function excerptPassage(markdown: string): string | null {
  const parts: string[] = []
  const take = (block: ResultBlock) => {
    if (block.kind === 'quote') {
      block.blocks.forEach(take)
    } else if (block.kind === 'fig') {
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
 *  heading is found, any other while its heading is, and `#` — the whole document — always. */
export function linkResolves(
  pages: readonly ResultPageBlocks[],
  outline: readonly OutlineEntry[],
  link: { label: string; anchor: string; passage: string | null },
): boolean {
  if (link.anchor === '') {
    return true
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
