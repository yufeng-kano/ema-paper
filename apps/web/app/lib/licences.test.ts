import { existsSync, readdirSync, readFileSync, realpathSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { PDFJS_ASSET_DIRS, PDFJS_ASSETS, selectPdfjsAssets } from '../../shared/pdfjs-assets'

/**
 * The licence check (docs/pdf-viewer.md § Licences). It reads the installed tree rather than
 * the lockfile, the way Node resolves it, so what it checks is what the build bundles: every
 * package `dependencies` reaches, through their `dependencies`, their `optionalDependencies`
 * installed on this platform and their peers. No network and no pnpm.
 */

const appDir = join(import.meta.dirname, '../..')
const repoDir = join(appDir, '../..')

/** What the PDF viewer takes: its libraries and everything they bring are held to this. */
const VIEWER_LICENCES = new Set(['Apache-2.0', 'MIT', 'BSD-2-Clause', 'BSD-3-Clause'])

/**
 * What the rest of the web app's tree carries besides, every one permissive: it asks for
 * attribution at most, never for the app's own source. Only licences the tree really has
 * are listed, so a new one is read before it is allowed.
 */
const APP_LICENCES = new Set([
  ...VIEWER_LICENCES,
  // ISC and Blue Oak: MIT-style permission with a notice.
  'ISC',
  'BlueOak-1.0.0',
  // A public-domain dedication (mdn-data, which CSS tooling reads).
  'CC0-1.0',
  // argparse, a port of Python's own module under Python's permissive licence.
  'Python-2.0',
  // caniuse-lite, the browser-support data browserslist reads at build time: data under
  // attribution, nothing of it in a page.
  'CC-BY-4.0',
])

/** The viewer's libraries (docs/pdf-viewer.md § Libraries), by package name. */
const VIEWER_LIBRARIES = ['pdfjs-dist']

/**
 * Packages a licence off both lists is allowed for, by name, each with its reason. A family
 * covers its platform builds too (`lightningcss-linux-x64-gnu`), whose names differ from
 * machine to machine.
 */
const NAMED_EXCEPTIONS: { family: string; licence: string; reason: string }[] = [
  {
    family: 'lightningcss',
    licence: 'MPL-2.0',
    // MPL-2.0 is copyleft per file: it binds changes to lightningcss's own files, of which
    // there are none, and nothing it runs on reaches a browser.
    reason: 'the CSS compiler Vite builds with, run at build time only and never shipped',
  },
]

/**
 * Packages in a viewer library's tree that never reach a browser, so they are held to the
 * app's list rather than the viewer's. Their licences are still checked.
 */
const NODE_ONLY: Record<string, string> = {
  '@napi-rs/canvas':
    'pdfjs-dist\'s optional canvas for pdf.js under Node; the browser build never loads it and nothing of it ships',
}

function inFamily(name: string, family: string): boolean {
  return name === family || name.startsWith(`${family}-`)
}

interface Installed {
  name: string
  version: string
  licence: string
  dir: string
  /** Dependency names: required, then optional (absent on another platform), then peers. */
  needs: [string, boolean][]
}

function readPackage(dir: string): Installed {
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
  // The SPDX `license` string, or the old `{ type }` object, or the older `licenses` list.
  const declared = pkg.license ?? pkg.licenses
  const licence = typeof declared === 'string'
    ? declared
    : Array.isArray(declared)
      ? declared.map((entry: { type?: string }) => entry.type).join(' OR ')
      : declared?.type ?? 'UNKNOWN'
  return {
    name: pkg.name,
    version: pkg.version,
    licence,
    dir,
    needs: [
      ...Object.keys(pkg.dependencies ?? {}).map((name): [string, boolean] => [name, false]),
      ...Object.keys(pkg.optionalDependencies ?? {}).map((name): [string, boolean] => [name, true]),
      ...Object.keys(pkg.peerDependencies ?? {}).map((name): [string, boolean] => [name, true]),
    ],
  }
}

/** Where Node finds `name` from a package in `from`: the nearest `node_modules` above it,
 *  which under pnpm is the package's own store entry, then the store's hoisted folder. */
function resolve(from: string, name: string): string | null {
  for (let dir = from; ; dir = dirname(dir)) {
    const candidate = join(dir, 'node_modules', name)
    if (basename(dir) !== 'node_modules' && existsSync(join(candidate, 'package.json'))) {
      return realpathSync(candidate)
    }
    if (dir === dirname(dir)) {
      return null
    }
  }
}

/** Every package reached from `roots`, once each, by its real directory; none below `skip`. */
function closure(roots: string[], skip: (name: string) => boolean = () => false): { found: Installed[]; missing: string[] } {
  const found = new Map<string, Installed>()
  const missing: string[] = []
  const queue: [string, string, boolean][] = roots.map((name) => [appDir, name, false])
  for (let entry = queue.shift(); entry; entry = queue.shift()) {
    const [from, name, optional] = entry
    const dir = resolve(from, name)
    if (!dir) {
      if (!optional) {
        missing.push(`${name}, needed from ${from}`)
      }
      continue
    }
    if (found.has(dir) || skip(name)) {
      continue
    }
    const installed = readPackage(dir)
    found.set(dir, installed)
    queue.push(...installed.needs.map(([need, isOptional]): [string, string, boolean] => [dir, need, isOptional]))
  }
  return { found: [...found.values()], missing }
}

/**
 * Whether an SPDX expression holds under `allowed`: `OR` takes either side, `AND` needs both
 * and binds tighter, as SPDX defines them. `WITH` is not understood, so an exception clause
 * fails and is read by a person.
 */
function satisfies(expression: string, allowed: ReadonlySet<string>): boolean {
  const tokens = expression.match(/[()]|[^\s()]+/g) ?? []
  let at = 0
  const term = (): boolean => {
    if (tokens[at] === '(') {
      at++
      const value = anyOf()
      at++
      return value
    }
    return allowed.has(tokens[at++] ?? '')
  }
  const allOf = (): boolean => {
    let value = term()
    while (tokens[at] === 'AND') {
      at++
      value = term() && value
    }
    return value
  }
  const anyOf = (): boolean => {
    let value = allOf()
    while (tokens[at] === 'OR') {
      at++
      value = allOf() || value
    }
    return value
  }
  return anyOf() && at === tokens.length
}

function refused(packages: Installed[], allowed: ReadonlySet<string>): string[] {
  return packages
    .filter(({ name, licence }) => !satisfies(licence, allowed)
      && !NAMED_EXCEPTIONS.some((exception) => inFamily(name, exception.family) && licence === exception.licence))
    .map(({ name, version, licence }) => `${name}@${version}: ${licence}`)
    .sort()
}

/**
 * The sources a file of the page scene may be ported from (docs/pdf-viewer.md § What is taken from
 * where), each with the licence its header must name and the title of its section in the notices.
 * Poppler (GPL) and MuPDF (AGPL) are not among them.
 */
const PORT_SOURCES: Record<string, { licence: RegExp; notice: string }> = {
  'pdf.js': { licence: /Apache-2\.0/, notice: 'PDF.js' },
  Vello: { licence: /Apache-2\.0|MIT/, notice: 'Vello' },
  kurbo: { licence: /Apache-2\.0|MIT/, notice: 'kurbo' },
  hayro: { licence: /Apache-2\.0|MIT/, notice: 'hayro' },
  glifo: { licence: /Apache-2\.0|MIT/, notice: 'glifo' },
  Skia: { licence: /BSD-3-Clause/, notice: 'Skia' },
  PDFium: { licence: /BSD-3-Clause|Apache-2\.0/, notice: 'PDFium' },
}

describe('the licence check', () => {
  const manifest = JSON.parse(readFileSync(join(appDir, 'package.json'), 'utf8')) as { dependencies: Record<string, string> }

  it('allows every licence in the web app\'s production tree', () => {
    const { found, missing } = closure(Object.keys(manifest.dependencies))
    expect(missing).toEqual([])
    expect(found.map(({ name }) => name)).toEqual(expect.arrayContaining(Object.keys(manifest.dependencies)))
    expect(refused(found, APP_LICENCES)).toEqual([])
  })

  it('holds the PDF viewer\'s libraries to Apache-2.0, MIT and BSD', () => {
    expect(Object.keys(manifest.dependencies)).toEqual(expect.arrayContaining(VIEWER_LIBRARIES))
    const nodeOnly = (name: string) => Object.keys(NODE_ONLY).some((family) => inFamily(name, family))
    const { found } = closure(VIEWER_LIBRARIES, nodeOnly)
    expect(found.map(({ name }) => name)).toEqual(expect.arrayContaining(VIEWER_LIBRARIES))
    expect(refused(found, VIEWER_LICENCES)).toEqual([])
  })

  it('serves only the whitelisted pdfjs-dist files, each with its licence in the notices', () => {
    const packageDir = resolve(appDir, 'pdfjs-dist')!
    const notices = readFileSync(join(repoDir, 'THIRD_PARTY_NOTICES'), 'utf8')
    for (const dir of PDFJS_ASSET_DIRS) {
      const names = readdirSync(join(packageDir, dir))
      const { files, missing } = selectPdfjsAssets(dir, names)
      // A whitelisted file pdf.js dropped or renamed would stop the build; it is caught here first.
      expect(missing).toEqual([])
      // Nothing outside the list: no Liberation font, no WebAssembly, no QuickJS or qcms.
      expect(files.filter((name) => /^Liberation|\.wasm$|quickjs|qcms/i.test(name))).toEqual([])
      expect(files.filter((name) => !PDFJS_ASSETS[dir].files.includes(name) && !PDFJS_ASSETS[dir].pattern?.test(name))).toEqual([])
      // Every licence file served is one the notices carry, by the path it is served at.
      for (const licence of files.filter((name) => name.startsWith('LICENSE'))) {
        expect(notices).toContain(`/pdfjs/<version>/${dir}/${licence}`)
      }
    }
    // The ICC profile's folder is not served at all.
    expect(PDFJS_ASSET_DIRS).not.toContain('iccs')
  })

  it('names a permissive source, its licence, the revision and the changes in each ported file of the page scene, the rasteriser and the GPU side', () => {
    const notices = readFileSync(join(repoDir, 'THIRD_PARTY_NOTICES'), 'utf8')
    const ported: string[] = []
    // A folder that does not exist fails here, so the expected list below cannot pass half built.
    for (const dir of ['scene', 'raster', 'gpu']) {
      const folder = join(appDir, 'app/lib/pdf', dir)
      for (const file of readdirSync(folder).filter((entry) => entry.endsWith('.ts'))) {
        const name = `${dir}/${file}`
        const source = readFileSync(join(folder, file), 'utf8')
        expect(source, name).not.toMatch(/poppler|mupdf/i)
        // The file's header is its first comment.
        const header = source.slice(0, source.indexOf('*/'))
        const from = /Ported from (\S+)/.exec(header)?.[1]
        if (from === undefined) {
          continue
        }
        ported.push(name)
        expect(Object.keys(PORT_SOURCES), name).toContain(from)
        const { licence, notice } = PORT_SOURCES[from]!
        expect(header, name).toMatch(licence)
        // Apache-2.0 § 4(b) and the doc's rule: the revision it was ported from, a release or a git
        // revision for a source whose ported code is unreleased, and what was changed.
        expect(header, name).toMatch(/Ported from \S+ (\d+\.\d+(\.\d+)?|git [0-9a-f]{7,})\b/)
        expect(header, name).toContain('Changed:')
        // The header points to the source's section of the notices, which must exist.
        const section = new RegExp(`^(\\d+)\\. ${notice}\\b`, 'm').exec(notices)?.[1]
        expect(section, `${name}: no section for ${from} in THIRD_PARTY_NOTICES`).toBeDefined()
        expect(header, name).toContain(`THIRD_PARTY_NOTICES § ${section}`)
      }
    }
    expect(ported.sort()).toEqual(['gpu/shaders.ts', 'raster/clip.ts', 'raster/encode.ts', 'raster/flatten.ts', 'raster/offset.ts',
      'raster/region.ts', 'raster/sampling.ts', 'raster/stroke.ts', 'raster/strips.ts', 'raster/tiles.ts',
      'scene/adapter.ts', 'scene/matrix.ts', 'scene/ops.ts', 'scene/state.ts', 'scene/text.ts'])
    expect(notices).toContain('apps/web/app/lib/pdf/scene/')
    expect(notices).toContain('apps/web/app/lib/pdf/raster/')
    expect(notices).toContain('apps/web/app/lib/pdf/gpu/')
  })
})
