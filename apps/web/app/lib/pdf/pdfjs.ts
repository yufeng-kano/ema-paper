/**
 * pdf.js for the Original view (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Loading*):
 * the one place that imports `pdfjs-dist`, picks its build, starts its worker and opens a
 * document. Everything a pdf.js promise can do wrong on the way — hang after a destroy, reject
 * with a message that carries the file's URL — stops here, so the viewer only ever sees a
 * settled promise and a failure `errors.ts` can read.
 *
 * Client only: the module is imported from the viewer's `onMounted`, and pdf.js itself is
 * imported on first use, so no page loads it with its first paint and a server render never
 * does.
 */

import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'
import { destinationFraction, parseExplicitDestination, type ExplicitDestination } from './links'
import { supportsModernPdfjs } from './support'

/** The pdf.js module, either build: the legacy entry re-exports the modern one's types. */
export type Pdfjs = typeof import('pdfjs-dist')

let loading: Promise<Pdfjs> | null = null

/**
 * The pdf.js build this browser can run, with its own worker, imported once. The modern and
 * legacy builds report the same version, so a mismatched pair would pass pdf.js's check and
 * fail later on a missing built-in; each import therefore takes its build's worker beside it.
 * The worker comes in through `?url`, a same-origin `/_nuxt/` asset, because pdf.js wraps a
 * cross-origin one in a `blob:` URL the site's `worker-src 'self'` refuses. `rendererSrc` is
 * never set: pages render on the main thread (§ *Render budget*).
 */
export function loadPdfjs(): Promise<Pdfjs> {
  loading ??= (async () => {
    const [pdfjs, worker] = supportsModernPdfjs()
      ? await Promise.all([import('pdfjs-dist'), import('pdfjs-dist/build/pdf.worker.min.mjs?url')])
      : await Promise.all([
          import('pdfjs-dist/legacy/build/pdf.mjs'),
          import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'),
        ])
    pdfjs.GlobalWorkerOptions.workerSrc = worker.default
    return pdfjs
  })().catch((error: unknown) => {
    // A chunk that failed to arrive is tried again on the next open rather than remembered.
    loading = null
    throw error
  })
  return loading
}

/**
 * A failure as plain fields `classifyLoadError` and `classifyPageError` read. pdf.js's own
 * messages are not safe to keep: a `ResponseException` names the full URL of the file, which
 * can carry a token, so every URL in `message` and `details` is cut out here and nothing
 * downstream has to remember to.
 */
export class PdfFailure extends Error {
  readonly status: number | undefined
  readonly missing: boolean | undefined
  readonly details: string | undefined

  constructor(name: string, message: string, fields: { status?: number; missing?: boolean; details?: string } = {}) {
    super(message)
    this.name = name
    this.status = fields.status
    this.missing = fields.missing
    this.details = fields.details
  }
}

const URL_PATTERN = /[a-z][a-z\d+.-]*:\/\/\S*/gi

function redact(text: unknown, url?: string): string | undefined {
  if (typeof text !== 'string') {
    return undefined
  }
  const cut = text.replace(URL_PATTERN, '<url>')
  return url ? cut.split(url).join('<url>') : cut
}

function read(error: unknown, name: string): unknown {
  return typeof error === 'object' && error !== null ? (error as Record<string, unknown>)[name] : undefined
}

/** `error` as a `PdfFailure`, with `url` (the file's own, as given) cut out of its text too. */
export function pdfFailure(error: unknown, url?: string): PdfFailure {
  if (error instanceof PdfFailure) {
    return error
  }
  const name = read(error, 'name')
  const status = read(error, 'status')
  const missing = read(error, 'missing')
  return new PdfFailure(typeof name === 'string' ? name : 'Error', redact(read(error, 'message'), url) ?? '', {
    status: typeof status === 'number' ? status : undefined,
    missing: typeof missing === 'boolean' ? missing : undefined,
    details: redact(read(error, 'details'), url),
  })
}

/** What every promise of a destroyed document settles with: a cancel, which shows nothing. */
function cancelled(): PdfFailure {
  return new PdfFailure('AbortException', 'Cancelled')
}

/** A page's size and turn, as the layout model takes them. */
export interface PdfPageInfo {
  page: PDFPageProxy
  /** Points, with the page's `/Rotate` and `userUnit` applied (`getViewport({ scale: 1 })`). */
  width: number
  height: number
  userUnit: number
  /** The page's `/Rotate`, 0, 90, 180 or 270. */
  rotation: number
}

/** Where an internal link goes. */
export interface PdfDestination {
  /** From 1. */
  page: number
  /** The destination's top, in points down the drawn page, or null for the page's own top. */
  top: number | null
  /** The same as a fraction of the drawn page's height, for `layout.destinationOffset`. */
  fraction: number | null
}

/** An open document. Every method settles, even once the document is destroyed. */
export interface PdfDocumentHandle {
  readonly pdfjs: Pdfjs
  readonly proxy: PDFDocumentProxy
  readonly numPages: number
  /** Rejects with a `PdfFailure` (a cancel once destroyed). */
  getPageInfo(pageNumber: number): Promise<PdfPageInfo>
  /** The page a destination (a name or an explicit array) is on, from 1, or null. Never rejects. */
  destinationPage(dest: unknown): Promise<number | null>
  /** Where a destination lands, or null for one that cannot be followed. Never rejects. */
  resolveDestination(dest: unknown): Promise<PdfDestination | null>
  readonly destroyed: boolean
  /** Destroys the loading task and waits for its worker to stop. Safe to call twice. */
  destroy(): Promise<void>
}

/** pdf.js's question for a password: the first ask or a retry after a wrong one. */
export interface PdfPasswordRequest {
  reason: 'need' | 'incorrect'
  /** Hands the password to pdf.js alone; it is never kept here. Only the first call counts. */
  submit(password: string): void
}

export interface OpenPdfOptions {
  /** The file's inline URL, same origin. */
  url: string
  /** `useRuntimeConfig().public.pdfjsBase`, such as `/pdfjs/6.5.438`. */
  assetBase: string
  /** Called for each ask; a destroy while it is open settles the opening as a cancel. */
  onPassword(request: PdfPasswordRequest): void
}

export interface PdfOpening {
  /** The open document, or a `PdfFailure`. A destroy settles it at once as a cancel. */
  readonly promise: Promise<PdfDocumentHandle>
  destroy(): Promise<void>
}

/**
 * Opens a document. The returned `destroy` works at any point, before pdf.js has loaded,
 * while the password prompt is open, or once the document is open, and `promise` always
 * settles: pdf.js's own promise can stay pending for ever after a destroy, so every await here
 * is raced against the destroy and checked when it resumes.
 */
export function openPdf(options: OpenPdfOptions): PdfOpening {
  const { url } = options
  let task: PDFDocumentLoadingTask | null = null
  let destroyed = false
  let destroying: Promise<void> | null = null
  // Not `Promise.withResolvers`: this runs before pdf.js is chosen, on engines the legacy
  // build is for.
  let stopNow: (reason: unknown) => void = () => {}
  const stopped = new Promise<never>((_, reject) => {
    stopNow = reject
  })
  stopped.catch(() => {})

  /** `promise`, unless the document is destroyed first. */
  async function guard<T>(promise: Promise<T>): Promise<T> {
    const value = await Promise.race([promise, stopped])
    if (destroyed) {
      throw cancelled()
    }
    return value
  }

  function destroy(): Promise<void> {
    destroying ??= (async () => {
      destroyed = true
      stopNow(cancelled())
      // A viewer that destroys a document has moved on and may never look at `promise` again;
      // its cancel must not surface as an unhandled rejection.
      promise.catch(() => {})
      // A destroy before setup rejects pdf.js's promise with a plain error and after it may
      // never settle it; either way the opening has already settled above.
      await task?.destroy().catch(() => {})
    })()
    return destroying
  }

  async function open(): Promise<PdfDocumentHandle> {
    const pdfjs = await guard(loadPdfjs())
    // The worker resolves a relative URL against its own `/_nuxt/` address, and a `*Url`
    // option must be a string ending in `/`.
    const assets = new URL(`${options.assetBase.replace(/\/+$/, '')}/`, location.origin).href
    task = pdfjs.getDocument({
      url,
      cMapUrl: `${assets}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${assets}standard_fonts/`,
      // Needed even without WebAssembly: the JBIG2 and JPEG 2000 fallbacks are imported from it.
      wasmUrl: `${assets}wasm/`,
      useWasm: false,
      useWorkerFetch: true,
      enableXfa: false,
      disableWorkerRendering: true,
      verbosity: pdfjs.VerbosityLevel.ERRORS,
    })
    task.onPassword = (update: (password: string | Error) => void, reason: number) => {
      let answered = false
      options.onPassword({
        reason: reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD ? 'incorrect' : 'need',
        submit(password) {
          if (answered || destroyed) {
            return
          }
          answered = true
          update(password)
        },
      })
    }
    const proxy = await guard(task.promise)
    return handle(pdfjs, proxy)
  }

  function handle(pdfjs: Pdfjs, proxy: PDFDocumentProxy): PdfDocumentHandle {
    /** A named destination looked up, an explicit one read, and its page found (from 0). */
    async function locate(dest: unknown): Promise<{ parsed: ExplicitDestination; index: number } | null> {
      const parsed = parseExplicitDestination(typeof dest === 'string' ? await guard(proxy.getDestination(dest)) : dest)
      if (!parsed) {
        return null
      }
      const index =
        'index' in parsed.page
          ? parsed.page.index
          : (proxy.cachedPageNumber(parsed.page.ref) ?? (await guard(proxy.getPageIndex(parsed.page.ref))) + 1) - 1
      return index >= 0 && index < proxy.numPages ? { parsed, index } : null
    }

    return {
      pdfjs,
      proxy,
      numPages: proxy.numPages,
      get destroyed() {
        return destroyed
      },
      destroy,
      async getPageInfo(pageNumber) {
        try {
          const page = await guard(proxy.getPage(pageNumber))
          const { width, height } = page.getViewport({ scale: 1 })
          return { page, width, height, userUnit: page.userUnit, rotation: page.rotate }
        } catch (error) {
          throw destroyed ? cancelled() : pdfFailure(error, url)
        }
      },
      async destinationPage(dest) {
        try {
          const found = await locate(dest)
          return found ? found.index + 1 : null
        } catch {
          return null
        }
      },
      async resolveDestination(dest) {
        try {
          const found = await locate(dest)
          if (!found) {
            return null
          }
          const page = await guard(proxy.getPage(found.index + 1))
          const fraction = destinationFraction(found.parsed, page.view, page.rotate)
          const { height } = page.getViewport({ scale: 1 })
          return { page: found.index + 1, fraction, top: fraction === null ? null : fraction * height }
        } catch {
          return null
        }
      },
    }
  }

  const promise = open().catch((error: unknown) => {
    throw destroyed ? cancelled() : pdfFailure(error, url)
  })
  return { promise, destroy }
}
