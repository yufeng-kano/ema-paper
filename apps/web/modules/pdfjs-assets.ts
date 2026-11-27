/**
 * Serves pdfjs-dist's data files beside the build, at `/pdfjs/<version>/<folder>/`
 * (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Files served beside the build*).
 *
 * A local module, registered by being in `modules/`. It copies the whitelist of
 * shared/pdfjs-assets.ts out of the installed package into the build directory and hands each
 * folder to nitro as a public asset directory, so the files are never committed, always match
 * the installed version, and a path outside the list is a 404 rather than a file someone
 * forgot to read the licence of.
 *
 * The version is in the path because nitro gives a public asset directory with a `maxAge`
 * an `immutable` cache: under a stable name, a browser would keep an older pdf.js's CMaps
 * after an upgrade. `runtimeConfig.public.pdfjsBase` hands that path to the client, read
 * from the installed package rather than written here.
 */
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { defineNuxtModule } from 'nuxt/kit'
import { PDFJS_ASSET_DIRS, selectPdfjsAssets } from '../shared/pdfjs-assets'

/** A year, in seconds: the files under a versioned path never change. */
const YEAR = 31_536_000

export default defineNuxtModule({
  meta: { name: 'pdfjs-assets' },
  setup(_options, nuxt) {
    // `pdfjs-dist` has no `exports` map, so its package.json resolves like any of its files.
    const packageDir = dirname(createRequire(join(nuxt.options.rootDir, 'package.json')).resolve('pdfjs-dist/package.json'))
    const { version } = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8')) as { version: string }
    const base = `/pdfjs/${version}`
    const outDir = join(nuxt.options.buildDir, 'pdfjs-assets')

    // Checked here, when Nuxt loads, so `nuxt dev`, `nuxt prepare` and a build all stop on a
    // file the whitelist names and the installed pdfjs-dist no longer has.
    const served = PDFJS_ASSET_DIRS.map((name) => {
      const source = join(packageDir, name)
      const { files, missing } = selectPdfjsAssets(name, existsSync(source) ? readdirSync(source) : [])
      if (missing.length) {
        throw new Error(
          `pdfjs-assets: pdfjs-dist ${version} has no ${missing.join(', ')} in ${source}. `
          + 'Update the whitelist in shared/pdfjs-assets.ts, and THIRD_PARTY_NOTICES, to the files this version ships.',
        )
      }
      return { name, source, files }
    })

    nuxt.options.runtimeConfig.public.pdfjsBase = base

    // Absolute directories: nitro resolves a relative one against its own source directory,
    // `server/`. A base other than `/` does not fall through, so a file not copied is a 404.
    nuxt.hook('nitro:config', (config) => {
      config.publicAssets ??= []
      config.publicAssets.push(
        ...served.map(({ name }) => ({ dir: join(outDir, name), baseURL: `${base}/${name}`, maxAge: YEAR })),
      )
    })

    // Copied when the build starts, not at setup: `nuxt build` and `nuxt prepare` empty the
    // build directory after the modules have run. Emptied first, so a file an older whitelist
    // took is not served on.
    nuxt.hook('build:before', () => {
      rmSync(outDir, { recursive: true, force: true })
      for (const { name, source, files } of served) {
        mkdirSync(join(outDir, name), { recursive: true })
        for (const file of files) {
          cpSync(join(source, file), join(outDir, name, file))
        }
      }
    })
  },
})
