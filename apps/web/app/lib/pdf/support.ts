/**
 * Which pdf.js build this browser can run (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer,
 * *Loading*). The modern build calls 2025-26 built-ins without a guard, some on every render, and an engine
 * without a global `Iterator` fails at its `import()`; the legacy build polyfills them. The
 * viewer probes before importing either, and always pairs a build with its own worker.
 */

function has(owner: unknown, name: string): boolean {
  return (typeof owner === 'object' || typeof owner === 'function') && owner !== null
    ? typeof (owner as Record<string, unknown>)[name] === 'function'
    : false
}

/**
 * Whether `scope` (the window, in the browser) has every built-in the modern build needs:
 * `Map`/`WeakMap#getOrInsertComputed`, `Math.sumPrecise`, `Promise.try`,
 * `Uint8Array.fromBase64` and `#toHex`, iterator helpers, `Set#intersection`, `URL.parse` and
 * `Response#bytes`. The main thread stands in for the worker, which runs on the same engine.
 */
export function supportsModernPdfjs(scope: unknown = globalThis): boolean {
  try {
    const global = scope as Record<string, { prototype?: unknown } | undefined>
    return (
      has(global.Map?.prototype, 'getOrInsertComputed') &&
      has(global.WeakMap?.prototype, 'getOrInsertComputed') &&
      has(global.Math, 'sumPrecise') &&
      has(global.Promise, 'try') &&
      has(global.Uint8Array, 'fromBase64') &&
      has(global.Uint8Array?.prototype, 'toHex') &&
      has(global.Iterator?.prototype, 'toArray') &&
      has(global.Set?.prototype, 'intersection') &&
      has(global.URL, 'parse') &&
      has(global.Response?.prototype, 'bytes')
    )
  } catch {
    // A getter that throws (a locked-down or proxied global) is a browser to keep to the
    // legacy build.
    return false
  }
}
