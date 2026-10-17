/**
 * The rich editor's schema and behaviour (docs/web.md § Editing). Framework-free: the node
 * views are plain DOM, so the schema also builds in node for the round-trip tests.
 *
 * The schema is exactly what the viewer's parser reads — paragraphs, headings, one-level
 * lists, pipe tables, display and inline math, code — and no more: a blockquote, a rule or a
 * strikethrough the editor could make but `markdown.ts` cannot read back would be an edit
 * that vanishes when the block closes. Elements carry the same `md-*` classes the rendered
 * block uses, so opening a block moves nothing on screen.
 */
import { Extension, InputRule, Node, mergeAttributes, type Extensions } from '@tiptap/core'
import { Heading } from '@tiptap/extension-heading'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import { Placeholder } from '@tiptap/extensions'
import StarterKit from '@tiptap/starter-kit'
import { renderTexHtml } from './math'
import { inlineMathAtEnd } from './richdoc'

/** Where the caret goes when an editor opens: a click's coordinates, an edge, or — after
 *  Backspace joined two blocks — the seam between the first block and the second. */
export type Caret = { x: number; y: number } | 'start' | 'end' | 'join'

/** Where a formula sits and what it holds, for the floating TeX field. */
export interface MathTarget {
  pos: number
  latex: string
  display: boolean
  dom: HTMLElement
}

export interface RichOptions {
  placeholder?: string
  /** A click on a formula: open its TeX field. */
  onEditMath?: (target: MathTarget) => void
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
        types: ['paragraph', 'heading', 'bulletList', 'orderedList', 'table', 'codeBlock', 'blockMath', 'rawBlock'],
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
    Table.configure({ resizable: false, HTMLAttributes: { class: 'md-table' } }),
    TableRow,
    TableHeader,
    TableCell,
    inlineMath(options),
    blockMath(options),
    RawBlock,
    SourceAttributes,
    FlatLists,
    ...(options.placeholder ? [Placeholder.configure({ placeholder: options.placeholder })] : []),
  ]
}
