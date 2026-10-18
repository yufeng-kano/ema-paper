/**
 * Copy-as-markdown: turns the selected part of the rendered document back into the
 * markdown it was rendered from (docs/web.md § Result viewer).
 *
 * The serializer walks the *cloned selection fragment* — `Range.cloneContents()` — so a
 * partial table selection arrives as a partial table and comes out as a valid partial pipe
 * table. Structure is recognized by the viewer's own block classes and data attributes:
 * `data-tex` gives inline math back as `$…$`, `data-md` gives a figure back as its
 * placeholder, and everything unrecognized falls through to its text.
 *
 * Written against a structural subset of the DOM (`MarkdownNode`) rather than `Node`, so
 * the logic is unit-testable in the framework-free node suite (docs/testing.md).
 */

const TEXT_NODE = 3
const ELEMENT_NODE = 1

/** The subset of `Node`/`Element` the serializer reads. Real DOM nodes satisfy it. */
export interface MarkdownNode {
  nodeType: number
  textContent?: string | null
  childNodes?: ArrayLike<MarkdownNode>
  tagName?: string
  getAttribute?: (name: string) => string | null
}

function children(node: MarkdownNode): MarkdownNode[] {
  return Array.from(node.childNodes ?? [])
}

function attribute(node: MarkdownNode, name: string): string | null {
  return node.getAttribute ? node.getAttribute(name) : null
}

function classes(node: MarkdownNode): string[] {
  return (attribute(node, 'class') ?? '').split(/\s+/).filter(Boolean)
}

function tag(node: MarkdownNode): string {
  return (node.tagName ?? '').toLowerCase()
}

/**
 * Collapses runs of whitespace into one space and trims — except inside `` `code` ``
 * spans, whose spaces are content: `` `a  b` `` restored from `data-src` must not lose
 * its double space to the surrounding block's normalization.
 */
function collapseWhitespace(text: string): string {
  return text
    .split(/(`[^`\n]*`)/)
    .map((part, index) => (index % 2 ? part : part.replace(/\s+/g, ' ')))
    .join('')
    .trim()
}

/** Inline content: text runs plus math spans, which come back as their `data-tex`. */
function inlineText(node: MarkdownNode): string {
  if (node.nodeType === TEXT_NODE) {
    return node.textContent ?? ''
  }
  if (node.nodeType !== ELEMENT_NODE) {
    return ''
  }
  const tex = attribute(node, 'data-tex')
  if (tex !== null) {
    return tex
  }
  // Rendered emphasis, code, <br>, <sup> and <sub> carry their exact markdown source (docs/web.md).
  const src = attribute(node, 'data-src')
  if (src !== null) {
    return inlineSource(node, src)
  }
  return children(node).map(inlineText).join('')
}

/**
 * A styled element's markdown. `data-src` is the exact source — but a selection boundary
 * can cut through the element: `cloneContents` then keeps the element (and its full
 * attribute) over truncated children, and handing back the whole source would claim text
 * the user did not select. So the source is trusted only when the children still serialize
 * to exactly what sits between its markers; a partial clone re-wraps what actually
 * survived. Nested styling still round-trips, because nested elements carry `data-src` of
 * their own.
 */
function inlineSource(node: MarkdownNode, src: string): string {
  const name = tag(node)
  let marker: string
  if (name === 'strong') {
    marker = src.startsWith('__') ? '__' : '**'
  } else if (name === 'em') {
    marker = src.startsWith('_') ? '_' : '*'
  } else if (name === 'code') {
    marker = '`'
  } else if (name === 'sup' || name === 'sub') {
    marker = `<${name}>`
  } else {
    // <br> has no children to lose — its source is its source.
    return src
  }
  // A tag closes with its end tag; every other marker closes with itself.
  const close = marker.startsWith('<') ? `</${name}>` : marker
  const inner = children(node).map(inlineText).join('')
  // A boundary sitting exactly on the element's edge clones it empty — markers around
  // nothing are not markdown, they are noise.
  if (!inner) {
    return ''
  }
  const expected = src.slice(marker.length, src.length - close.length)
  return inner === expected ? src : `${marker}${inner}${close}`
}

/** A table cell's markdown: inline content with its pipes escaped, on one line. */
function cellText(node: MarkdownNode): string {
  return collapseWhitespace(inlineText(node)).replace(/\|/g, '\\|')
}

function tableRows(node: MarkdownNode): MarkdownNode[] {
  const rows: MarkdownNode[] = []
  for (const child of children(node)) {
    if (tag(child) === 'tr') {
      rows.push(child)
    } else {
      rows.push(...tableRows(child))
    }
  }
  return rows
}

function rowsToMarkdown(rows: MarkdownNode[]): string {
  const lines: string[] = []
  for (const [index, row] of rows.entries()) {
    const cells = children(row).filter((cell) => tag(cell) === 'th' || tag(cell) === 'td')
    if (!cells.length) {
      continue
    }
    lines.push(`| ${cells.map(cellText).join(' | ')} |`)
    // The separator goes under the first row: a partial selection may not include the
    // real header, but a pipe table without a separator is not a table to a renderer.
    if (index === 0) {
      lines.push(`| ${cells.map(() => '---').join(' | ')} |`)
    }
  }
  return lines.join('\n')
}

function tableToMarkdown(node: MarkdownNode): string {
  return rowsToMarkdown(tableRows(node))
}

/**
 * One pipe table from cells the caller has already grouped into rows — Firefox reports a
 * table-region selection as one range per cell, and serializing each range alone would
 * make every cell its own one-column table. The copy handler groups the live cells by
 * their row and hands them here.
 */
export function cellGroupsToMarkdown(rows: MarkdownNode[][]): string {
  return rowsToMarkdown(
    rows.map((cells) => ({ nodeType: ELEMENT_NODE, tagName: 'TR', childNodes: cells })),
  )
}

function listToMarkdown(node: MarkdownNode): string {
  const ordered = tag(node) === 'ol'
  const start = Number.parseInt(attribute(node, 'start') ?? '1', 10) || 1
  const items = children(node).filter((child) => tag(child) === 'li')
  return items
    .map((item, index) => {
      const marker = ordered ? `${start + index}.` : '-'
      return `${marker} ${collapseWhitespace(inlineText(item))}`
    })
    .join('\n')
}

function codeToMarkdown(node: MarkdownNode): string {
  const code = children(node).find((child) => tag(child) === 'code')
  if (code) {
    const language =
      classes(code)
        .find((name) => name.startsWith('language-'))
        ?.slice('language-'.length) ?? ''
    return `\`\`\`${language}\n${code.textContent ?? ''}\n\`\`\``
  }
  // No <code> child: the viewer's display-math block.
  return `$$\n${node.textContent ?? ''}\n$$`
}

/** A KaTeX clone's visible glyph text — the `.katex-html` layer when present, so the
 *  invisible MathML mirror does not double every character into the copy. */
function renderedMathText(node: MarkdownNode): string {
  const html = findByClass(node, 'katex-html')
  return (((html ?? node).textContent ?? '')).replace(/\s+/g, ' ').trim()
}

function findByClass(node: MarkdownNode, name: string): MarkdownNode | null {
  if (node.nodeType === ELEMENT_NODE && classes(node).includes(name)) {
    return node
  }
  for (const child of children(node)) {
    const found = findByClass(child, name)
    if (found) {
      return found
    }
  }
  return null
}

/** One block element's markdown, or null to fall through to its children. */
function blockToMarkdown(node: MarkdownNode, partialBlocks?: ReadonlySet<string>): string | null {
  const source = attribute(node, 'data-md')
  if (source !== null) {
    // A display-math block a selection boundary sits inside arrives as its element —
    // full `data-md` over truncated KaTeX children — and handing back the source would
    // claim formula content outside the selection. The caller names those blocks from
    // the live selection by their per-occurrence `data-md-id` (a repeated formula's
    // twins share one source); for them the surviving glyph text is the copy.
    const id = attribute(node, 'data-md-id')
    if (id !== null && partialBlocks?.has(id)) {
      return renderedMathText(node)
    }
    return source
  }
  const names = classes(node)
  if (names.includes('md-h2') || names.includes('md-h3')) {
    // The viewer draws two heading sizes, but the source depth rides on `data-level` —
    // copying `# Title` must not demote it to `##`, nor `####` promote to `###`.
    const fallback = names.includes('md-h2') ? 2 : 3
    const level = Number.parseInt(attribute(node, 'data-level') ?? '', 10) || fallback
    return `${'#'.repeat(level)} ${inlineText(node).trim()}`
  }
  if (names.includes('md-toc')) {
    // Rows go back one per line, as the contents page wrote them: blank lines between
    // them would make each a paragraph of its own. The leader's dots ride on `data-src`.
    return children(node)
      .filter((child) => tag(child) === 'p')
      .map((row) => collapseWhitespace(inlineText(row)))
      .join('\n')
  }
  switch (tag(node)) {
    case 'table':
      return tableToMarkdown(node)
    case 'ul':
    case 'ol':
      return listToMarkdown(node)
    case 'pre':
      return codeToMarkdown(node)
    case 'p':
      return collapseWhitespace(inlineText(node))
    default:
      return null
  }
}

/** Tags that carry inline content — a selection inside one paragraph arrives as these. */
const INLINE_TAGS = new Set(['span', 'sup', 'sub', 'em', 'strong', 'i', 'b', 'a', 'code', 'br'])

function isInline(node: MarkdownNode): boolean {
  if (node.nodeType === TEXT_NODE) {
    return true
  }
  if (node.nodeType !== ELEMENT_NODE || attribute(node, 'data-md') !== null) {
    return false
  }
  return INLINE_TAGS.has(tag(node))
}

interface Walk {
  blocks: string[]
  inline: string[]
  /** `data-md` sources of display-math blocks a live selection boundary cut through. */
  partialBlocks?: ReadonlySet<string>
}

function flush(walk: Walk): void {
  const text = collapseWhitespace(walk.inline.join(''))
  if (text) {
    walk.blocks.push(text)
  }
  walk.inline = []
}

/**
 * A selection confined to one table or list is cloned *without* its structural root:
 * `Range.cloneContents()` copies the parent chain only up to — exclusive of — the range's
 * common ancestor, so dragging across a table hands over bare `thead`/`tbody`/`tr` (or
 * `td` for one row), and a multi-item list hands over bare `li`. These runs are
 * reassembled here into the table or list their parent would have made of them.
 */
const TABLE_PARTS = new Set(['thead', 'tbody', 'tfoot', 'tr'])
const CELL_PARTS = new Set(['td', 'th'])

function orphanGroup(node: MarkdownNode): 'rows' | 'cells' | 'items' | null {
  if (node.nodeType !== ELEMENT_NODE) {
    return null
  }
  const name = tag(node)
  if (TABLE_PARTS.has(name)) {
    return 'rows'
  }
  if (CELL_PARTS.has(name)) {
    return 'cells'
  }
  if (name === 'li') {
    return 'items'
  }
  return null
}

/** What the selection's own DOM knows about the list its cloned `li` runs fell out of —
 *  computed by the copy handler from the live range, since the clone no longer holds it. */
export interface OrphanListContext {
  ordered: boolean
  /** The number the first selected item carries in the live list. */
  start: number
}

function orphansToMarkdown(
  kind: 'rows' | 'cells' | 'items',
  group: MarkdownNode[],
  list?: OrphanListContext,
): string {
  if (kind === 'rows') {
    return rowsToMarkdown(group.flatMap((part) => (tag(part) === 'tr' ? [part] : tableRows(part))))
  }
  if (kind === 'cells') {
    // Cells with no row: one row's worth of a table.
    return rowsToMarkdown([{ nodeType: ELEMENT_NODE, tagName: 'TR', childNodes: group }])
  }
  // Items with no list: the live selection's context says what the ancestor was — an
  // ordered run keeps its numbering. Without context, bullets.
  return group
    .map((item, index) => {
      const marker = list?.ordered ? `${list.start + index}.` : '-'
      return `${marker} ${collapseWhitespace(inlineText(item))}`
    })
    .join('\n')
}

function walkNodes(nodes: MarkdownNode[], walk: Walk, list?: OrphanListContext): void {
  let index = 0
  while (index < nodes.length) {
    const node = nodes[index]!
    const kind = orphanGroup(node)
    if (kind !== null) {
      // Consecutive orphaned parts are one structure, not a block each; formatting
      // whitespace between them does not break the run.
      const group: MarkdownNode[] = []
      while (index < nodes.length) {
        const candidate = nodes[index]!
        if (orphanGroup(candidate) === kind) {
          group.push(candidate)
          index += 1
          continue
        }
        if (candidate.nodeType === TEXT_NODE && !(candidate.textContent ?? '').trim()) {
          index += 1
          continue
        }
        break
      }
      flush(walk)
      const block = orphansToMarkdown(kind, group, list)
      if (block) {
        walk.blocks.push(block)
      }
      continue
    }
    walkNode(node, walk)
    index += 1
  }
}

function walkNode(node: MarkdownNode, walk: Walk): void {
  // Consecutive inline runs — text and math spans from inside one paragraph — stay one
  // line rather than becoming a block each.
  if (isInline(node)) {
    walk.inline.push(inlineText(node))
    return
  }
  if (node.nodeType !== ELEMENT_NODE) {
    return
  }
  const block = blockToMarkdown(node, walk.partialBlocks)
  if (block !== null) {
    flush(walk)
    if (block) {
      walk.blocks.push(block)
    }
    return
  }
  // A container (section, article, div): its children are blocks of their own.
  flush(walk)
  walkNodes(children(node), walk)
  flush(walk)
}

/**
 * The selection fragment as markdown. A selection inside one paragraph comes back as the
 * plain text it looks like (math spans as their `$…$`); anything structural comes back as
 * source a markdown editor will re-render — including a selection whose table or list
 * root the range clone left behind. `list` is that lost root's context, when the caller
 * could read it off the live selection — it only affects top-level orphaned `li` runs.
 * `partialBlocks` names the display-math blocks a selection boundary sits inside (again
 * read off the live selection): their `data-md` would claim unselected formula content,
 * so they copy as the glyph text the clone actually carries.
 */
export function nodesToMarkdown(
  nodes: ArrayLike<MarkdownNode>,
  list?: OrphanListContext,
  partialBlocks?: ReadonlySet<string>,
): string {
  const walk: Walk = { blocks: [], inline: [], partialBlocks }
  walkNodes(Array.from(nodes), walk, list)
  flush(walk)
  return walk.blocks.join('\n\n')
}
