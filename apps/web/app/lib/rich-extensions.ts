/**
 * The rich editor's schema and behaviour (docs/web.md § Editing). Framework-free: the node
 * views are plain DOM, so the schema also builds in node for the round-trip tests.
 *
 * The schema is exactly what the viewer's parser reads — paragraphs, headings, one-level
 * lists, one-level quotes, pipe tables, display and inline math, code, superscript and
 * subscript, and in the notes section links — and no more: a rule or a strikethrough the
 * editor could make but `markdown.ts` cannot read back would be an edit that vanishes when the
 * block closes. Elements carry the same `md-*` classes the rendered block uses, so opening a
 * block moves nothing on screen.
 */
import { Extension, InputRule, Node, mergeAttributes, type Extensions, type JSONContent } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { findWrapping } from '@tiptap/pm/transform'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { Heading } from '@tiptap/extension-heading'
import { Subscript } from '@tiptap/extension-subscript'
import { Superscript } from '@tiptap/extension-superscript'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import { Placeholder } from '@tiptap/extensions'
import StarterKit from '@tiptap/starter-kit'
import { BLOCK_DRAG_TYPE, NOTES_BLOCK_DRAG_TYPE } from './dnd'
import { inlinePlain, parseInline, parseResultMarkdown, type InlineSegment, type ResultBlock } from './markdown'
import { renderTexHtml } from './math'
import { UnitSelection } from './note-blocks'
import { inlineMathAtEnd, sliceToMarkdown } from './richdoc'

export type Point = { x: number; y: number }

/**
 * A place in the text as the rendered blocks and the editor both hold it (docs/web.md
 * § Editing, *Selecting is editing*): the textblock — a paragraph, a heading, a list item, a
 * table cell, a code block's text, a display formula, in reading order from the editor's
 * first block — and the character offset within it, an inline formula and a line break each
 * one character.
 */
export interface TextPoint {
  slot: number
  offset: number
}

/** Where the caret goes when an editor opens: a click's coordinates, a selection's two ends
 *  by text position, an edge, or — after Backspace joined two blocks — the seam between the
 *  first block and the second. */
export type Caret = Point | { from: TextPoint; to: TextPoint } | 'start' | 'end' | 'join'

/** How the reader left an editor: Escape, focus going elsewhere, or Enter in a one-line one. */
export type LeaveReason = 'escape' | 'blur' | 'enter'

/** Where a formula sits and what it holds, for the floating TeX field. */
export interface MathTarget {
  pos: number
  latex: string
  display: boolean
  dom: HTMLElement
}

/** A section link in the notes, as the viewer resolves and follows it: its text and anchor,
 *  and — when it is an excerpt's link, right after a quote — the quote's markdown. */
export interface SectionLinkTarget {
  label: string
  anchor: string
  passage: string | null
}

/** How a section link is drawn: a chip, a chip held pressed while the document shows what it
 *  located, or its plain text when nothing it names can be found. */
export type SectionLinkState = 'chip' | 'pressed' | 'text'

export interface RichOptions {
  placeholder?: string
  /** A click on a formula: open its TeX field. */
  onEditMath?: (target: MathTarget) => void
  /** Section links are read (the notes): how each is drawn, and what a press on a chip does. */
  links?: {
    state: (target: SectionLinkTarget) => SectionLinkState
    locate: (target: SectionLinkTarget) => void
    /** Pasted or dropped markdown before it is read: block references to this document become
     *  section links (docs/web.md § Notes, *Quotes and section links*). */
    rewrite?: (markdown: string) => string
  }
  /** Figures and images drawn as themselves rather than as their source: the address of a
   *  block's picture, or null for the dashed frame, the note under it, or null, and the
   *  frame's words for each. */
  figures?: {
    url: (block: Extract<ResultBlock, { kind: 'fig' | 'img' }>) => string | null
    note: (block: Extract<ResultBlock, { kind: 'fig' | 'img' }>) => string | null
    pending: { figure: string; image: string }
  }
  /** The notes' blocks move by a handle (docs/web.md § Notes): a unit selected or carried is
   *  drawn as such. */
  blocks?: boolean
}

/** Typesets into `dom`, falling back to the dollars-and-source the viewer falls back to. */
function typesetInto(dom: HTMLElement, latex: string, display: boolean): void {
  const html = latex.trim() ? renderTexHtml(latex, display) : null
  if (html !== null) {
    dom.innerHTML = html
    dom.classList.remove('is-source')
  } else {
    dom.textContent = display ? `$$ ${latex} $$` : `$${latex}$`
    dom.classList.add('is-source')
  }
}

/**
 * The source a top-level node was built from and that source's canonical form —
 * `richdoc.ts` writes an unchanged node back as its original bytes. Never rendered and
 * never parsed from HTML: a pasted block has no source to keep.
 */
const SourceAttributes = Extension.create({
  name: 'sourceAttributes',
  // A drag of the viewer's blocks, or of a notes block by its handle, lands between blocks,
  // where the editor draws its own line (`RichEditor`): the drop cursor, which marks a place in
  // the text, is not drawn for it.
  extendNodeSchema() {
    return {
      disableDropCursor: (_view: unknown, _pos: unknown, event: DragEvent) => {
        const types = [...(event.dataTransfer?.types ?? [])]
        return types.includes(BLOCK_DRAG_TYPE) || types.includes(NOTES_BLOCK_DRAG_TYPE)
      },
    }
  },
  addGlobalAttributes() {
    return [
      {
        types: ['paragraph', 'heading', 'bulletList', 'orderedList', 'blockquote', 'table', 'codeBlock', 'blockMath', 'rawBlock'],
        attributes: {
          src: { default: null, rendered: false, parseHTML: () => null, keepOnSplit: true },
          canon: { default: null, rendered: false, parseHTML: () => null, keepOnSplit: true },
          // The whitespace before the block in its file. Not kept by a split: the new half
          // is a new block, and gets the default gap.
          lead: { default: null, rendered: false, parseHTML: () => null, keepOnSplit: false },
        },
      },
    ]
  },
})

function inlineMath(options: RichOptions) {
  return Node.create({
    name: 'inlineMath',
    group: 'inline',
    inline: true,
    atom: true,
    selectable: true,
    addAttributes() {
      return { latex: { default: '' } }
    },
    parseHTML() {
      return [{ tag: 'span[data-inline-math]', getAttrs: (el) => ({ latex: (el as HTMLElement).dataset.latex ?? '' }) }]
    },
    renderHTML({ node, HTMLAttributes }) {
      return ['span', mergeAttributes(HTMLAttributes, { 'data-inline-math': '', 'data-latex': node.attrs.latex }), `$${node.attrs.latex}$`]
    },
    addNodeView() {
      return ({ node, getPos }) => {
        const dom = document.createElement('span')
        dom.className = 'md-inline-math rich-math'
        dom.contentEditable = 'false'
        let latex = String(node.attrs.latex)
        typesetInto(dom, latex, false)
        dom.addEventListener('mousedown', (event) => {
          event.preventDefault()
          const pos = getPos()
          if (typeof pos === 'number') {
            options.onEditMath?.({ pos, latex, display: false, dom })
          }
        })
        return {
          dom,
          update(next) {
            if (next.type.name !== 'inlineMath') {
              return false
            }
            latex = String(next.attrs.latex)
            typesetInto(dom, latex, false)
            return true
          },
          ignoreMutation: () => true,
        }
      }
    },
    addInputRules() {
      // `$x$` becomes a formula the moment its closing dollar is typed — by the viewer's
      // own span rules, so "$5 and $" stays text.
      return [
        new InputRule({
          find: (text) => {
            const match = inlineMathAtEnd(text)
            return match ? { index: match.index, text: text.slice(match.index), data: { tex: match.tex } } : null
          },
          handler: ({ state, range, match }) => {
            // Dollars inside a code span are code, as the parser reads them.
            const code = state.schema.marks.code
            if (code && (state.doc.rangeHasMark(range.from, range.to, code) || code.isInSet(state.doc.resolve(range.from).marks()))) {
              return null
            }
            const tex = String((match as unknown as { data?: { tex?: string } }).data?.tex ?? '')
            state.tr.replaceWith(range.from, range.to, this.type.create({ latex: tex }))
          },
        }),
      ]
    },
  })
}

function blockMath(options: RichOptions) {
  return Node.create({
    name: 'blockMath',
    group: 'block',
    atom: true,
    selectable: true,
    addAttributes() {
      return { latex: { default: '' } }
    },
    parseHTML() {
      return [{ tag: 'div[data-block-math]', getAttrs: (el) => ({ latex: (el as HTMLElement).dataset.latex ?? '' }) }]
    },
    renderHTML({ node, HTMLAttributes }) {
      return ['div', mergeAttributes(HTMLAttributes, { 'data-block-math': '', 'data-latex': node.attrs.latex }), `$$${node.attrs.latex}$$`]
    },
    addNodeView() {
      return ({ node, getPos }) => {
        const dom = document.createElement('div')
        dom.className = 'md-math-block rich-math'
        dom.contentEditable = 'false'
        let latex = String(node.attrs.latex)
        typesetInto(dom, latex, true)
        dom.addEventListener('mousedown', (event) => {
          event.preventDefault()
          const pos = getPos()
          if (typeof pos === 'number') {
            options.onEditMath?.({ pos, latex, display: true, dom })
          }
        })
        return {
          dom,
          update(next) {
            if (next.type.name !== 'blockMath') {
              return false
            }
            latex = String(next.attrs.latex)
            typesetInto(dom, latex, true)
            return true
          },
          ignoreMutation: () => true,
        }
      }
    },
    addInputRules() {
      // `$$` and a space on an empty line: a display formula, its TeX field open.
      return [
        new InputRule({
          find: /^\$\$\s$/,
          handler: ({ state, range }) => {
            const $from = state.doc.resolve(range.from)
            if ($from.parent.textContent !== '$$ ') {
              return null
            }
            const start = $from.before()
            state.tr.replaceWith(start, $from.after(), this.type.create({ latex: '' }))
            setTimeout(() => {
              const dom = this.editor.view.nodeDOM(start)
              if (dom instanceof HTMLElement) {
                options.onEditMath?.({ pos: start, latex: '', display: true, dom })
              }
            })
          },
        }),
      ]
    },
  })
}

/** The figure or image a raw block's source is, alone, or null. */
function pictureOf(source: string): Extract<ResultBlock, { kind: 'fig' | 'img' }> | null {
  const blocks = parseResultMarkdown(source).flatMap((page) => page.blocks)
  const only = blocks.length === 1 ? blocks[0]! : null
  return only?.kind === 'fig' || only?.kind === 'img' ? only : null
}

/** The dashed frame a picture with nothing to load is drawn as, as `MdBlock` draws it. */
function pictureFrame(pending: string): HTMLElement {
  const frame = document.createElement('div')
  frame.className = 'figure-frame'
  frame.textContent = pending
  return frame
}

/** A block with no rich-text form — a contents page, a figure placeholder, an image, a
 *  comment — carried through as its source. Given `figures`, a figure or an image is drawn as
 *  the document draws it, the picture and its caption, and stays one atom (docs/web.md
 *  § Editing, *Images*). */
function rawBlock(options: RichOptions) {
  const figures = options.figures
  return Node.create({
    name: 'rawBlock',
    group: 'block',
    atom: true,
    addAttributes() {
      return { source: { default: '' } }
    },
    parseHTML() {
      return [{ tag: 'pre[data-raw-block]', getAttrs: (el) => ({ source: (el as HTMLElement).textContent ?? '' }) }]
    },
    renderHTML({ node, HTMLAttributes }) {
      return ['pre', mergeAttributes(HTMLAttributes, { 'data-raw-block': '', class: 'md-math rich-raw' }), node.attrs.source]
    },
    ...(figures
      ? {
          addNodeView() {
            return ({ node }) => {
              const source = String(node.attrs.source)
              // Any other change is a new view: a figure's source is its whole address.
              const update = (next: typeof node) => next.type === node.type && next.attrs.source === source
              const picture = pictureOf(source)
              if (!picture) {
                const dom = document.createElement('pre')
                dom.className = 'md-math rich-raw'
                dom.dataset.rawBlock = ''
                dom.textContent = source
                return { dom, update }
              }
              const dom = document.createElement('figure')
              dom.className = 'md-figure rich-figure'
              dom.contentEditable = 'false'
              const pending = picture.kind === 'fig' ? figures.pending.figure : figures.pending.image
              const url = figures.url(picture)
              if (url) {
                const image = document.createElement('img')
                image.className = 'figure-image'
                image.src = url
                image.alt = picture.caption ? inlinePlain(picture.caption) : picture.kind === 'fig' ? picture.id : picture.alt
                image.draggable = false
                image.addEventListener('error', () => image.replaceWith(pictureFrame(pending)), { once: true })
                dom.append(image)
              } else {
                dom.append(pictureFrame(pending))
              }
              const note = figures.note(picture)
              if (note) {
                const element = document.createElement('p')
                element.className = 'figure-note'
                element.dataset.chrome = ''
                element.textContent = note
                dom.append(element)
              }
              if (picture.caption) {
                const caption = document.createElement('figcaption')
                caption.className = 'figure-caption'
                caption.textContent = inlinePlain(picture.caption)
                dom.append(caption)
              }
              return { dom, update }
            }
          },
        }
      : {}),
  })
}

/**
 * A quote: blocks behind a bar, one level — its content holds no quote, as the parser reads
 * none (docs/web.md § Result viewer). `> ` at the start of a paragraph at the top level makes
 * one; not in a list item or a table cell, which the parser reads as one line. Backspace at
 * its start lifts the block back out, ProseMirror's own join.
 */
const Quote = Node.create({
  name: 'blockquote',
  group: 'block',
  content: '(paragraph | heading | bulletList | orderedList | table | codeBlock | blockMath | rawBlock)+',
  defining: true,
  parseHTML() {
    return [{ tag: 'blockquote' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['blockquote', mergeAttributes(HTMLAttributes, { class: 'md-quote' }), 0]
  },
  addInputRules() {
    return [
      new InputRule({
        find: /^>\s$/,
        handler: ({ state, range }) => {
          const $from = state.doc.resolve(range.from)
          if ($from.depth !== 1 || $from.parent.type.name !== 'paragraph') {
            return null
          }
          const tr = state.tr.delete(range.from, range.to)
          const blockRange = tr.doc.resolve(range.from).blockRange()
          const wrapping = blockRange && findWrapping(blockRange, this.type)
          if (!blockRange || !wrapping) {
            return null
          }
          tr.wrap(blockRange, wrapping)
        },
      }),
    ]
  },
})

const SVG = 'http://www.w3.org/2000/svg'

/** The locate glyph, as `UiIcon` draws it — a node view is plain DOM. */
function locateGlyph(): SVGSVGElement {
  const svg = document.createElementNS(SVG, 'svg')
  for (const [name, value] of Object.entries({
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '1.75',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
  })) {
    svg.setAttribute(name, value)
  }
  const path = document.createElementNS(SVG, 'path')
  path.setAttribute('d', 'M2 12h3M19 12h3M12 2v3M12 19v3')
  const circle = document.createElementNS(SVG, 'circle')
  circle.setAttribute('cx', '12')
  circle.setAttribute('cy', '12')
  circle.setAttribute('r', '7')
  svg.append(path, circle)
  return svg
}

/** A link's text as drawn — formulas typeset, emphasis kept — in plain DOM. */
function drawSegments(into: HTMLElement, segments: readonly InlineSegment[]): void {
  for (const segment of segments) {
    switch (segment.kind) {
      case 'text':
        into.append(segment.text)
        break
      case 'code': {
        const code = document.createElement('code')
        code.className = 'md-inline-code'
        code.textContent = segment.text
        into.append(code)
        break
      }
      case 'math': {
        const span = document.createElement('span')
        span.className = 'md-inline-math'
        const html = renderTexHtml(segment.tex, false)
        if (html === null) {
          span.textContent = `$${segment.tex}$`
        } else {
          span.innerHTML = html
        }
        into.append(span)
        break
      }
      case 'br':
        into.append(' ')
        break
      case 'cite':
        drawSegments(into, segment.segments)
        break
      default: {
        const tag = { strong: 'strong', em: 'em', sup: 'sup', sub: 'sub' }[segment.kind]
        const element = document.createElement(tag)
        drawSegments(element, segment.segments)
        into.append(element)
      }
    }
  }
}

/**
 * A section link in the notes (docs/web.md § Notes, *Quotes and section links*): one atom that
 * keeps its markdown source byte for byte — the caret steps over it and Backspace deletes it
 * whole — drawn as the answers' chip while what it names can be found, else as its text. An
 * excerpt's link is the one right after a quote, which hands the resolver its passage.
 */
function sectionLink(options: RichOptions) {
  const live = new Set<() => void>()
  return Node.create({
    name: 'sectionLink',
    group: 'inline',
    inline: true,
    atom: true,
    selectable: true,
    addStorage() {
      return { refresh: () => live.forEach((draw) => draw()) }
    },
    addAttributes() {
      return { src: { default: '' }, label: { default: '' }, anchor: { default: '' } }
    },
    parseHTML() {
      return [
        {
          tag: 'span[data-section-link]',
          getAttrs: (el) => {
            const dataset = (el as HTMLElement).dataset
            return { src: dataset.src ?? '', label: dataset.label ?? '', anchor: dataset.anchor ?? '' }
          },
        },
      ]
    },
    renderHTML({ node, HTMLAttributes }) {
      return [
        'span',
        mergeAttributes(HTMLAttributes, {
          'data-section-link': '',
          'data-src': node.attrs.src,
          'data-label': node.attrs.label,
          'data-anchor': node.attrs.anchor,
        }),
        node.attrs.label,
      ]
    },
    addNodeView() {
      return ({ node, getPos, editor }) => {
        const dom = document.createElement('span')
        dom.contentEditable = 'false'
        dom.dataset.src = String(node.attrs.src)
        let current = node
        /** The quote right before the paragraph this link is the whole of, as markdown. */
        const target = (): SectionLinkTarget => {
          const label = String(current.attrs.label)
          const anchor = String(current.attrs.anchor)
          const pos = getPos()
          if (typeof pos !== 'number') {
            return { label, anchor, passage: null }
          }
          const $pos = editor.state.doc.resolve(pos)
          const paragraph = $pos.parent
          let links = 0
          let other = false
          paragraph.forEach((child) => {
            if (child.type.name === 'sectionLink') {
              links += 1
            } else if (!child.isText || (child.text ?? '').trim()) {
              other = true
            }
          })
          const index = $pos.depth >= 1 ? $pos.index($pos.depth - 1) : 0
          const before = index > 0 ? $pos.node($pos.depth - 1).child(index - 1) : null
          const passage =
            links === 1 && !other && before?.type.name === 'blockquote'
              ? sliceToMarkdown((before.content.toJSON() ?? []) as JSONContent[])
              : null
          return { label, anchor, passage }
        }
        const draw = () => {
          const state = options.links?.state(target()) ?? 'text'
          dom.replaceChildren()
          const segments = parseInline(String(current.attrs.src), { cites: true })
          const cite = segments.find((segment) => segment.kind === 'cite')
          if (state === 'text') {
            dom.className = 'rich-link-text'
            drawSegments(dom, cite?.kind === 'cite' ? cite.segments : [{ kind: 'text', text: String(current.attrs.label) }])
            return
          }
          dom.className = state === 'pressed' ? 'rich-link pressed' : 'rich-link'
          const label = document.createElement('span')
          label.className = 'rich-link-label'
          drawSegments(label, cite?.kind === 'cite' ? cite.segments : [{ kind: 'text', text: String(current.attrs.label) }])
          dom.append(locateGlyph(), label)
        }
        draw()
        live.add(draw)
        dom.addEventListener('mousedown', (event) => event.preventDefault())
        dom.addEventListener('click', () => {
          if (dom.classList.contains('rich-link')) {
            options.links?.locate(target())
          }
        })
        return {
          dom,
          update(next) {
            if (next.type.name !== 'sectionLink') {
              return false
            }
            current = next
            dom.dataset.src = String(next.attrs.src)
            draw()
            return true
          },
          destroy() {
            live.delete(draw)
          },
          ignoreMutation: () => true,
        }
      }
    },
  })
}

/** Headings drawn as the viewer draws them: two sizes, `#`/`##` large, the rest small. */
const ViewerHeading = Heading.extend({
  renderHTML({ node, HTMLAttributes }) {
    const large = Number(node.attrs.level) <= 2
    return [
      large ? 'h3' : 'h4',
      mergeAttributes(HTMLAttributes, { class: large ? 'md-h2' : 'md-h3', 'data-level': node.attrs.level }),
      0,
    ]
  },
})

/** The wash's state: top-level blocks drawn washed, and whether the wash is fading. */
export const washKey = new PluginKey<DecorationSet>('wash')

/** What a transaction's `washKey` meta asks: wash these top-level blocks, fade the wash, or
 *  end it. */
export type WashMeta = { first: number; count: number } | 'fade' | 'clear'

/**
 * The located heading's wash, inside an editor (docs/web.md § Notes, *Then the notes show
 * it*): an excerpt the notes just took, or the one a marker leads to, is drawn washed and
 * then fades — as decorations, never written into the text, so they follow the text if it
 * changes underneath.
 */
const Washes = Extension.create({
  name: 'washes',
  addProseMirrorPlugins() {
    return [
      new Plugin<DecorationSet>({
        key: washKey,
        state: {
          init: () => DecorationSet.empty,
          apply(tr, set) {
            const meta = tr.getMeta(washKey) as WashMeta | undefined
            if (meta === undefined) {
              return set.map(tr.mapping, tr.doc)
            }
            if (meta === 'clear') {
              return DecorationSet.empty
            }
            if (meta === 'fade') {
              const faded = set.find().map((wash) => Decoration.node(wash.from, wash.to, { class: 'rich-wash rich-wash-fade' }))
              return DecorationSet.create(tr.doc, faded)
            }
            const washes: Decoration[] = []
            tr.doc.forEach((node, offset, index) => {
              if (index >= meta.first && index < meta.first + meta.count) {
                washes.push(Decoration.node(offset, offset + node.nodeSize, { class: 'rich-wash' }))
              }
            })
            return DecorationSet.create(tr.doc, washes)
          },
        },
        props: {
          decorations: (state) => washKey.getState(state),
        },
      }),
    ]
  },
})

/** A unit drawn as something other than text: selected while its menu is open, faded while it
 *  is dragged. What a transaction's `unitLookKey` meta sets; null draws nothing. */
export type UnitLook = { from: number; to: number; look: 'selected' | 'moving' } | null

export const unitLookKey = new PluginKey<UnitLook>('unitLook')

/**
 * How the notes draw a unit taken whole (docs/web.md § Notes, *The notes' blocks have handles*):
 * the unit selection, and the unit a menu or a drag holds, as node decorations on its top-level
 * blocks — an excerpt's two drawn as one layer, joined where they meet.
 */
const NoteBlocks = Extension.create({
  name: 'noteBlocks',
  addProseMirrorPlugins() {
    return [
      new Plugin<UnitLook>({
        key: unitLookKey,
        state: {
          init: () => null,
          apply(tr, look) {
            const meta = tr.getMeta(unitLookKey) as UnitLook | undefined
            if (meta !== undefined) {
              return meta
            }
            if (!look || !tr.docChanged) {
              return look
            }
            const from = tr.mapping.map(look.from, 1)
            const to = tr.mapping.map(look.to, -1)
            return from < to ? { ...look, from, to } : null
          },
        },
        props: {
          decorations: (state) => {
            const looks: NonNullable<UnitLook>[] = []
            if (state.selection instanceof UnitSelection) {
              looks.push({ from: state.selection.from, to: state.selection.to, look: 'selected' })
            }
            const held = unitLookKey.getState(state)
            if (held) {
              looks.push(held)
            }
            const decorations: Decoration[] = []
            for (const { from, to, look } of looks) {
              const blocks: { pos: number; end: number }[] = []
              state.doc.forEach((node, pos) => {
                if (pos >= from && pos + node.nodeSize <= to) {
                  blocks.push({ pos, end: pos + node.nodeSize })
                }
              })
              blocks.forEach(({ pos, end }, index) => {
                let classes = `rich-unit-${look}`
                if (index > 0) {
                  classes += ' rich-unit-join-before'
                }
                if (index < blocks.length - 1) {
                  classes += ' rich-unit-join-after'
                }
                decorations.push(Decoration.node(pos, end, { class: classes }))
              })
            }
            return DecorationSet.create(state.doc, decorations)
          },
        },
      }),
    ]
  },
})

/** Lists stay one level: Tab inside a list item does nothing rather than nest. */
const FlatLists = Extension.create({
  name: 'flatLists',
  priority: 1000,
  addKeyboardShortcuts() {
    return {
      Tab: () => this.editor.isActive('listItem'),
      'Shift-Tab': () => this.editor.isActive('listItem'),
    }
  },
})

export function richExtensions(options: RichOptions = {}): Extensions {
  return [
    StarterKit.configure({
      heading: false,
      blockquote: false,
      horizontalRule: false,
      strike: false,
      underline: false,
      link: false,
      trailingNode: false,
      paragraph: { HTMLAttributes: { class: 'md-p' } },
      bulletList: { HTMLAttributes: { class: 'md-list' } },
      orderedList: { HTMLAttributes: { class: 'md-list' } },
      code: { HTMLAttributes: { class: 'md-inline-code' } },
      codeBlock: { HTMLAttributes: { class: 'md-math' } },
    }),
    ViewerHeading.configure({ levels: [1, 2, 3, 4, 5, 6] }),
    Superscript.configure({ HTMLAttributes: { class: 'md-sup' } }),
    Subscript.configure({ HTMLAttributes: { class: 'md-sub' } }),
    Table.configure({ resizable: false, HTMLAttributes: { class: 'md-table' } }),
    TableRow,
    TableHeader,
    TableCell,
    inlineMath(options),
    blockMath(options),
    Quote,
    ...(options.links ? [sectionLink(options)] : []),
    rawBlock(options),
    SourceAttributes,
    FlatLists,
    Washes,
    ...(options.blocks ? [NoteBlocks] : []),
    ...(options.placeholder ? [Placeholder.configure({ placeholder: options.placeholder })] : []),
  ]
}
