/**
 * KaTeX rendering for the result viewer (docs/web.md § Result viewer).
 *
 * One function, one policy: TeX that KaTeX can typeset renders as math; TeX it cannot
 * parse returns null and the caller falls back to the verbatim source — a wrong glyph is
 * worse than visible TeX, so `throwOnError` stays on and the failure is the signal.
 * KaTeX escapes its input and `trust` is off, so the returned HTML is inert markup.
 *
 * KaTeX's stylesheet is imported here, not in the app's global CSS, so it ships only in the
 * chunks that typeset math and never in the prerendered public pages. It comes from the
 * package with its fonts bundled by the build: math depends on nothing remote.
 */
import katex from 'katex'
import 'katex/dist/katex.min.css'

/** Commands transcriptions produce that KaTeX does not define. */
const MACROS: Record<string, string> = {
  '\\degree': '^{\\circ}',
}

// --- repairs ---------------------------------------------------------------------------
//
// TeX a model wrote is TeX a person would have fixed before compiling. Three slips account
// for nearly every display block KaTeX rejects in a transcribed book, each with one
// unambiguous reading, so each is repaired — but only for a formula that already failed:
// source KaTeX accepts is never rewritten (docs/web.md § Result viewer).

/** A display block that is one `aligned` environment and nothing else. */
const WHOLE_ALIGNED = /^\s*\\begin\{aligned\}([\s\S]*)\\end\{aligned\}\s*$/

/** The rows of an alignment body: split on `\\` outside any brace group or nested
 *  environment, where a `\\` belongs to the matrix or `cases` it sits in. */
function alignmentRows(body: string): string[] {
  const rows: string[] = []
  let depth = 0
  let from = 0
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index]
    if (char === '{') {
      depth += 1
    } else if (char === '}') {
      depth -= 1
    } else if (char === '\\') {
      if (body.startsWith('\\begin{', index)) {
        depth += 1
      } else if (body.startsWith('\\end{', index)) {
        depth -= 1
      } else if (body[index + 1] === '\\' && depth === 0) {
        rows.push(body.slice(from, index))
        from = index + 2
      }
      // Whatever follows the backslash is that command's, not a brace or a row break.
      index += 1
    }
  }
  rows.push(body.slice(from))
  return rows
}

/**
 * A numbered derivation arrives as `aligned` with a `\tag` on its rows — what a model
 * writes for equations (2.50)–(2.52) — and TeX allows one tag per *display*, so KaTeX
 * rejects the block. `align` is the same layout with a number per row, which is what the
 * page printed. It also numbers every row by itself, so a row the page left unnumbered
 * says `\notag`. Only when the environment is the whole block: inside `\left[ … \right]`
 * the two are not interchangeable.
 */
function numberRows(tex: string): string {
  const match = WHOLE_ALIGNED.exec(tex)
  if (!match || !tex.includes('\\tag')) {
    return tex
  }
  const rows = alignmentRows(match[1] ?? '').map((row) =>
    row.includes('\\tag') || !row.trim() ? row : `${row} \\notag`,
  )
  return `\\begin{align}${rows.join('\\\\')}\\end{align}`
}

/** The end of the run of `{…}` groups starting at `open`, or -1 when one never closes. */
function groupsEnd(tex: string, open: number): number {
  let index = open
  while (tex[index] === '{') {
    let depth = 0
    for (; index < tex.length; index += 1) {
      if (tex[index] === '\\') {
        index += 1
      } else if (tex[index] === '{') {
        depth += 1
      } else if (tex[index] === '}') {
        depth -= 1
        if (depth === 0) {
          break
        }
      }
    }
    if (index >= tex.length) {
      return -1
    }
    index += 1
  }
  return index
}

/**
 * `V^\boldsymbol{\pi}` — a command with arguments as a bare script. TeX proper lets some
 * of these through and KaTeX none; the braces `V^{\boldsymbol{\pi}}` are what was meant.
 */
function braceScripts(tex: string): string {
  let out = ''
  let cursor = 0
  const script = /(?<!\\)[\^_]\s*(\\[a-zA-Z]+)\s*(?=\{)/g
  for (let match = script.exec(tex); match; match = script.exec(tex)) {
    const open = match.index + match[0].length
    const end = groupsEnd(tex, open)
    if (end === -1 || match.index < cursor) {
      continue
    }
    out += `${tex.slice(cursor, match.index + 1)}{${match[1]}${tex.slice(open, end)}}`
    cursor = end
    script.lastIndex = end
  }
  return out + tex.slice(cursor)
}

const TRAILING_TAG = /\s*\\tag\*?\{[^{}]*\}\s*$/

/**
 * An equation too long for its line is printed on two, and transcribed as two blocks:
 * the `\left\{` opens in one and its `\right\}` closes in the next, so neither compiles.
 * The null delimiter is TeX's own idiom for exactly this — `\right.` closes what the
 * first half left open, `\left.` opens what the second half closes. A closing `\right.`
 * goes before a trailing `\tag`, which must stay outside the delimited group.
 */
function balanceDelimiters(tex: string): string {
  const lefts = (tex.match(/\\left(?![a-zA-Z])/g) ?? []).length
  const rights = (tex.match(/\\right(?![a-zA-Z])/g) ?? []).length
  if (rights > lefts) {
    return `${'\\left. '.repeat(rights - lefts)}${tex}`
  }
  if (lefts > rights) {
    const tag = TRAILING_TAG.exec(tex)
    const body = tag ? tex.slice(0, tag.index) : tex
    return `${body}${' \\right.'.repeat(lefts - rights)}${tag ? tag[0] : ''}`
  }
  return tex
}

/** KaTeX HTML for one TeX source, or null when KaTeX cannot parse it even repaired. */
export function renderTexHtml(tex: string, displayMode: boolean): string | null {
  const html = typeset(tex, displayMode)
  if (html !== null) {
    return html
  }
  let repaired = balanceDelimiters(braceScripts(tex))
  if (displayMode) {
    repaired = numberRows(repaired)
  }
  return repaired === tex ? null : typeset(repaired, displayMode)
}

function typeset(tex: string, displayMode: boolean): string | null {
  try {
    return katex.renderToString(tex, {
      displayMode,
      throwOnError: true,
      // Warnings (about unicode in math mode, say) are not errors — render anyway,
      // and keep them out of the console.
      strict: 'ignore',
      // A fresh copy per call: KaTeX writes `\gdef`-style definitions into this object,
      // and a shared one would let one document's formula redefine commands for every
      // formula rendered after it.
      macros: { ...MACROS },
    })
  } catch {
    return null
  }
}
