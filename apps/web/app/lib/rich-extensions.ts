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
import { findWrapping } from '@tiptap/pm/transform'
import { Heading } from '@tiptap/extension-heading'
import { Subscript } from '@tiptap/extension-subscript'
import { Superscript } from '@tiptap/extension-superscript'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import { Placeholder } from '@tiptap/extensions'
import StarterKit from '@tiptap/starter-kit'
import { parseInline, type InlineSegment } from './markdown'
import { renderTexHtml } from './math'
import { inlineMathAtEnd, sliceToMarkdown } from './richdoc'

export type Point = { x: number; y: number }

/** Where the caret goes when an editor opens: a click's coordinates, a selection's two ends,
 *  an edge, or — after Backspace joined two blocks — the seam between the first block and
 *  the second. */
export type Caret = Point | { from: Point; to: Point } | 'start' | 'end' | 'join'

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
  }
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

/** A block with no rich-text form — a contents page, a figure placeholder, a comment —
 *  carried through as its source. */
const RawBlock = Node.create({
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
})

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
    RawBlock,
    SourceAttributes,
    FlatLists,
    ...(options.placeholder ? [Placeholder.configure({ placeholder: options.placeholder })] : []),
  ]
}
