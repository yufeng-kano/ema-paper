import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decodeFixture } from './fixture.ts'
import { OP_NAMES, OPS, PDFJS_OPS_VERSION } from './ops.ts'

const fixturesDir = join(import.meta.dirname, 'fixtures')
const fixtures = readdirSync(fixturesDir).filter((name) => name.endsWith('.oplist.json')).sort()
  .map((name) => decodeFixture(readFileSync(join(fixturesDir, name), 'utf8')))

describe('pdf.js’s op codes', () => {
  it('name each code once, and leave the unassigned ones empty', () => {
    expect(OP_NAMES.length).toBe(Math.max(...Object.values(OPS)) + 1)
    for (const [name, code] of Object.entries(OPS)) expect(OP_NAMES[code]).toBe(name)
    expect([0, 78, 79, 82].map((code) => OP_NAMES[code])).toEqual([undefined, undefined, undefined, undefined])
    expect(OP_NAMES.filter((name) => name !== undefined)).toHaveLength(Object.keys(OPS).length)
  })

  it('are the codes every stored fixture was captured with', () => {
    expect(fixtures.length).toBeGreaterThan(0)
    for (const fixture of fixtures) expect(fixture.ops).toEqual(OPS)
  })

  // A pdfjs-dist upgrade fails here until the codes are checked and the fixtures captured again.
  it('come from the pdf.js version installed and captured', () => {
    const manifest = JSON.parse(readFileSync(join(import.meta.dirname, '../../../../package.json'), 'utf8')) as {
      dependencies: Record<string, string>
    }
    expect(manifest.dependencies['pdfjs-dist']).toBe(PDFJS_OPS_VERSION)
    for (const fixture of fixtures) expect(fixture.pdfjs).toBe(PDFJS_OPS_VERSION)
  })
})
