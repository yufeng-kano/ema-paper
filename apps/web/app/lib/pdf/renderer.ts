/**
 * The WebGPU renderer's developer switch and what it changes when a document opens (docs/pdf-viewer.md
 * § Stage 4: the WebGPU core). This is the one stage 4 module on the default path, so it stays this
 * small and imports nothing.
 */

/** The developer switch, `?pdfRenderer=webgpu` exactly. Stage 7 adds 'canvas'. */
export type RendererSwitch = 'webgpu' | null

export function readRendererSwitch(search: string): RendererSwitch {
  return new URLSearchParams(search).get('pdfRenderer') === 'webgpu' ? 'webgpu' : null
}

export type RendererOpenOptions = Readonly<{ disableFontFace?: true; useSystemFonts?: false }>

const DEFAULT_OPTIONS: RendererOpenOptions = Object.freeze({})
const WEBGPU_OPTIONS: RendererOpenOptions = Object.freeze({ disableFontFace: true, useSystemFonts: false })

/**
 * getDocument options under the switch: glyph outlines in commonObjs (W 34903) for the scene, as the
 * stage 3 check read them; no local font substitution. Without the switch, none, so the default path
 * opens exactly as before.
 */
export function openOptions(requested: RendererSwitch): RendererOpenOptions {
  return requested === 'webgpu' ? WEBGPU_OPTIONS : DEFAULT_OPTIONS
}

/** Every developer console line of the renderer starts with this. */
export const REPORT_PREFIX = '[pdf webgpu]'

/**
 * The one line printed when the switch is on and the document is drawn by Canvas 2D from the start.
 * It lives here rather than with the other console lines because it is printed even when the
 * renderer's own module failed to load.
 */
export function notUsedLine(reason: string): string {
  return `${REPORT_PREFIX} not used: ${reason}`
}
