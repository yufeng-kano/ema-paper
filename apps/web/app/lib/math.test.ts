import { describe, expect, it } from 'vitest'
import { renderTexHtml } from './math'

describe('renderTexHtml', () => {
  it('typesets a cases environment in display mode', () => {
    const html = renderTexHtml('\\begin{cases} a & b \\\\ c & d \\end{cases}', true)

    expect(html).not.toBeNull()
    expect(html).toContain('katex-display')
  })

  it('typesets affiliation-grade inline math', () => {
    expect(renderTexHtml('^{1,2}', false)).toContain('katex')
  })

  it('covers the \\degree transcriptions produce', () => {
    expect(renderTexHtml('\\text{25}\\degree', false)).not.toBeNull()
  })

  it('returns null for TeX KaTeX cannot parse, so the caller keeps the source', () => {
    expect(renderTexHtml('\\frobnicate{x}', false)).toBeNull()
  })
})

describe('macro isolation', () => {
  it('does not let one formula redefine commands for the next', () => {
    expect(renderTexHtml('\\gdef\\zzz{9}\\zzz', false)).not.toBeNull()
    expect(renderTexHtml('\\zzz', false)).toBeNull()
  })
})

describe('a numbered derivation', () => {
  const tex = '\\begin{aligned}\n0 &= a - b \\tag{2.50} \\\\\n&= c \\tag{2.51}\n\\end{aligned}'

  it('typesets `aligned` rows that each carry a \\tag, numbers included', () => {
    const html = renderTexHtml(tex, true)
    expect(html).not.toBeNull()
    expect(html).toContain('2.50')
    expect(html).toContain('2.51')
  })

  it('still falls back to source when the rows themselves do not parse', () => {
    expect(renderTexHtml(tex.replace('a - b', '\\frac{a'), true)).toBeNull()
  })
})

describe('repairs for TeX a model wrote', () => {
  it('gives an unnumbered row of a numbered derivation no number of its own', () => {
    const html = renderTexHtml(
      '\\begin{aligned}\na &= b \\tag{3.1} \\\\\n&= \\begin{pmatrix} 1 \\\\ 2 \\end{pmatrix} \\\\\n&= c \\tag{3.2}\n\\end{aligned}',
      true,
    )
    expect(html).not.toBeNull()
    // `align` numbers a row through a CSS counter on an empty `.eqn-num`; a tagged row
    // carries its number as text instead.
    expect(html).not.toContain('class="eqn-num"')
    expect(html).toContain('3.2')
  })

  it('braces a command used as a bare script', () => {
    expect(renderTexHtml('V_i^\\boldsymbol{\\pi}(h) = Q_i^\\boldsymbol{\\pi}(h, a)', true)).not.toBeNull()
    expect(renderTexHtml('x^\\frac{1}{2}', false)).not.toBeNull()
  })

  it('closes the halves of an equation printed across two lines', () => {
    expect(renderTexHtml('L = \\left\\{ (V - v)^2 - \\sum_a p(a) \\tag{4.5}', true)).not.toBeNull()
    expect(renderTexHtml('+ (r - M)^2 \\right\\} \\tag{4.6}', true)).not.toBeNull()
  })
})

