/**
 * The pdfjs-dist data files the PDF viewer is served beside the build, and only these
 * (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Files served beside the build*).
 * Framework-free: the local module that copies them (modules/pdfjs-assets.ts) and the licence
 * check (app/lib/licences.test.ts) read the same list, and the check must not load Nuxt.
 *
 * A list of what is taken rather than of what is left out, so a pdf.js upgrade that adds a
 * file serves nothing new until someone has read its licence. The Liberation fonts (GPL-2.0
 * with a font exception), the ICC profile, the WebAssembly binaries, qcms and QuickJS are
 * therefore absent by not being named (docs/pdf-viewer.md § Licences).
 */

/** The folders of pdfjs-dist that are served, each at `/pdfjs/<version>/<folder>/`. */
export const PDFJS_ASSET_DIRS = ['cmaps', 'standard_fonts', 'wasm'] as const

export type PdfjsAssetDir = (typeof PDFJS_ASSET_DIRS)[number]

interface Whitelist {
  /** Files taken by name. Every one must exist, or the build stops. */
  files: readonly string[]
  /** Files taken by pattern, for a set too long to name. At least one must match. */
  pattern?: RegExp
}

export const PDFJS_ASSETS: Readonly<Record<PdfjsAssetDir, Whitelist>> = {
  // Adobe's packed CMaps, for CJK text in fonts that are not embedded (cMapPacked: true).
  cmaps: { pattern: /^[\w-]+\.bcmap$/, files: ['LICENSE'] },
  // The Foxit Type 1 fonts from PDFium, which pdf.js draws Symbol and ZapfDingbats with.
  standard_fonts: { pattern: /^Foxit\w*\.pfb$/, files: ['LICENSE_FOXIT'] },
  // The JavaScript builds of the JBIG2 and JPEG 2000 decoders, which pdf.js imports from
  // `wasmUrl` when it runs without WebAssembly (useWasm: false), with their four licences.
  wasm: {
    files: [
      'jbig2_nowasm_fallback.js',
      'openjpeg_nowasm_fallback.js',
      'LICENSE_JBIG2',
      'LICENSE_PDFJS_JBIG2',
      'LICENSE_OPENJPEG',
      'LICENSE_PDFJS_OPENJPEG',
    ],
  },
}

/**
 * Which of a folder's `names` are served, and what the list asks for that the folder lacks:
 * a named file, or the pattern when nothing matches it. A file pdf.js renamed in an upgrade
 * shows up here as missing, so the module can stop the build rather than serve a 404.
 */
export function selectPdfjsAssets(dir: PdfjsAssetDir, names: readonly string[]): { files: string[]; missing: string[] } {
  const { files, pattern } = PDFJS_ASSETS[dir]
  const matched = pattern ? names.filter((name) => pattern.test(name)) : []
  const missing = files.filter((name) => !names.includes(name))
  if (pattern && !matched.length) {
    missing.push(String(pattern))
  }
  return { files: [...matched, ...files.filter((name) => names.includes(name))].sort(), missing }
}
