import type { Ref, ShallowRef } from 'vue'
import { ApiRequestError } from '~/lib/api'
import { backingSize, canvasLimits, canvasesToRelease, type CanvasLimits, type LiveCanvas } from '~/lib/pdf/budget'
import { classifyPageError, type PageErrorKind } from '~/lib/pdf/errors'
import {
  pagesToMeasure,
  renderQueue,
  renderWindow,
  setPageSizes,
  visibleRange,
  visibleRect,
  type Layout,
  type PageSizeChange,
  type Size,
} from '~/lib/pdf/layout'
import { PageView } from '~/lib/pdf/page-view'
import type { PdfDestination, PdfDocumentHandle, PdfPageInfo } from '~/lib/pdf/pdfjs'

/** A page not drawn this long after it came into the render window shows a spinner. */
const SPINNER_DELAY_MS = 400

export type PdfPageFailureKind = Exclude<PageErrorKind, 'cancelled'>

/** A page that failed: why, whether its retry runs, and whether it has been announced (once,
 *  while in view). */
export interface PdfPageFailure {
  kind: PdfPageFailureKind
  retrying: boolean
  announced: boolean
}

export interface PdfPagesOptions {
  layout: ShallowRef<Layout | null>
  pane: Ref<HTMLElement | null>
  paneSize: ShallowRef<Size>
  handle: ShallowRef<PdfDocumentHandle | null>
  /** No scroll for 150 ms: a capped page's detail canvas is drawn only then. */
  settled(): boolean
  /** A pinch shows its transform: nothing is drawn until it commits. */
  pinching(): boolean
  /** Asks the viewer for another frame. */
  schedule(): void
  onNavigate(target: PdfDestination): void
  announce(text: string): void
  /** A page failure's cause in the reader's words. */
  failureText(kind: PdfPageFailureKind): string
}

/**
 * The Original view's pages (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Layout and
 * scrolling*, *Render budget*, *Errors*): which pages hold layers, their real sizes as they are
 * read, the draws one page at a time within the total canvas budget, the spinner on a page
 * slow to draw, and a page's own failure and retry.
 *
 * The viewer owns the pane, the layout and the zoom, and calls `sync` once per animation frame;
 * this owns every `PageView` and keeps the page holding focus alive wherever it is.
 */
export function usePdfPages(options: PdfPagesOptions) {
  const { t } = useI18n()
  const apiError = useApiError()

  const failures = shallowReactive(new Map<number, PdfPageFailure>())
  /** Pages in the render window that are still blank after `SPINNER_DELAY_MS`. */
  const slow = shallowReactive(new Set<number>())
  /** Some page has been drawn: the pane is no longer busy. */
  const drawn = ref(false)

  const views = new Map<number, PageView>()
  const infos = new Map<number, PdfPageInfo>()
  const measuring = new Set<number>()
  const hosts = new Map<number, HTMLElement>()
  const sections = new Map<number, HTMLElement>()
  /** What each page's base canvas was last drawn for, to tell whether a draw makes a new one. */
  const drawnFor = new Map<number, string>()
  const slowTimers = new Map<number, ReturnType<typeof setTimeout>>()
  let windowPages = new Set<number>()
  let pendingSizes: PageSizeChange[] = []
  let limits: CanvasLimits | null = null
  let pumping = false
  let pumpAgain = false

  const labels = {
    page: (page: number) => t('viewer.pdf.link.page', { page }),
    external: (host: string) => t('viewer.pdf.link.external', { host }),
  }

  function setSection(index: number, el: unknown) {
    if (el instanceof HTMLElement) {
      sections.set(index, el)
    } else {
      sections.delete(index)
    }
  }

  function setHost(index: number, el: unknown) {
    if (el instanceof HTMLElement) {
      hosts.set(index, el)
    } else {
      hosts.delete(index)
    }
  }

  function focusSection(index: number) {
    sections.get(index)?.focus({ preventScroll: true })
  }

  /** The page's user unit, which its layers are laid out with; 1 until its size is read. */
  function userUnit(index: number): number {
    return infos.get(index)?.userUnit ?? 1
  }

  /** Drops every page and its layers; `first` starts a document, with page 1 already read. */
  function reset(first: PdfPageInfo | null) {
    for (const view of views.values()) {
      view.destroy()
    }
    views.clear()
    infos.clear()
    measuring.clear()
    drawnFor.clear()
    for (const timer of slowTimers.values()) {
      clearTimeout(timer)
    }
    slowTimers.clear()
    slow.clear()
    failures.clear()
    windowPages = new Set()
    pendingSizes = []
    drawn.value = false
    if (first) {
      infos.set(0, first)
      limits = canvasLimits(navigator)
    }
  }

  /**
   * Applies the page sizes read since the last frame to `column`, in place, and returns how far
   * to move `scrollTop` in this same frame. With `hold` (a touch has the pane moving) a page
   * above the viewport keeps its guessed size until the pane settles; one at or below it
   * changes in place, which moves nothing in view.
   */
  function applySizes(column: Layout, scrollTop: number, hold: boolean): number | null {
    const now = hold ? pendingSizes.filter(({ index }) => column.tops[index]! >= scrollTop) : pendingSizes
    pendingSizes = hold ? pendingSizes.filter(({ index }) => column.tops[index]! < scrollTop) : []
    return now.length > 0 ? setPageSizes(column, now, scrollTop) : null
  }

  /** The page whose `<section>` holds focus, which keeps its layers wherever it is. */
  function focusedPage(): number | null {
    const active = document.activeElement
    const section = active instanceof Element && options.pane.value?.contains(active) ? active.closest('[data-page]') : null
    return section instanceof HTMLElement ? Number(section.dataset.page) : null
  }

  /**
   * One frame's page work: the layers that leave the render window go, a blank page in it gets
   * its spinner's clock, a failed page in view is announced, the pages near the view have their
   * sizes read, and the draws start.
   */
  function sync(column: Layout, scrollTop: number, height: number) {
    const range = renderWindow(column, scrollTop, height)
    const keep = new Set<number>()
    for (let index = range.first; index <= range.last; index++) {
      keep.add(index)
    }
    const focused = focusedPage()
    if (focused !== null) {
      keep.add(focused)
    }
    for (const [index, view] of views) {
      if (!keep.has(index)) {
        view.destroy()
        views.delete(index)
        drawnFor.delete(index)
      }
    }
    windowPages = keep
    timeSpinners()

    const visible = visibleRange(column, scrollTop, scrollTop + height)
    if (visible) {
      for (const [index, failure] of failures) {
        if (!failure.announced && index >= visible.first && index <= visible.last) {
          failures.set(index, { ...failure, announced: true })
          options.announce(`${t('viewer.pdf.pageError.title', { page: index + 1 })} ${options.failureText(failure.kind)}`)
        }
      }
    }

    for (const index of pagesToMeasure(column, scrollTop, height)) {
      measure(index)
    }
    void pump()
  }

  function timeSpinners() {
    for (const index of windowPages) {
      if (views.get(index)?.painted || failures.has(index) || slowTimers.has(index) || slow.has(index)) {
        continue
      }
      slowTimers.set(
        index,
        setTimeout(() => {
          slowTimers.delete(index)
          if (windowPages.has(index) && !views.get(index)?.painted && !failures.has(index)) {
            slow.add(index)
          }
        }, SPINNER_DELAY_MS),
      )
    }
    for (const [index, timer] of slowTimers) {
      if (!windowPages.has(index)) {
        clearTimeout(timer)
        slowTimers.delete(index)
      }
    }
    for (const index of slow) {
      if (!windowPages.has(index)) {
        slow.delete(index)
      }
    }
  }

  function stopSpinner(index: number) {
    clearTimeout(slowTimers.get(index))
    slowTimers.delete(index)
    slow.delete(index)
  }

  /** Reads a page's real size (`getPage`); it is applied on the next frame. */
  function measure(index: number) {
    const opened = options.handle.value
    const failure = failures.get(index)
    if (!opened || measuring.has(index) || infos.has(index) || (failure && !failure.retrying)) {
      return
    }
    measuring.add(index)
    opened.getPageInfo(index + 1).then(
      (info) => {
        if (opened !== options.handle.value) {
          return
        }
        measuring.delete(index)
        infos.set(index, info)
        pendingSizes.push({ index, size: { width: info.width, height: info.height } })
        options.schedule()
      },
      (error: unknown) => {
        if (opened !== options.handle.value) {
          return
        }
        measuring.delete(index)
        fail(index, error)
      },
    )
  }

  function fail(index: number, error: unknown) {
    const kind = classifyPageError(error, navigator.onLine === false)
    if (kind === 'cancelled') {
      return
    }
    // A page failure never moves focus: a retry button holding it is the same element after a
    // failed retry, and the page keeps its place at its known size.
    failures.set(index, { kind, retrying: false, announced: false })
    stopSpinner(index)
    if (kind === 'signed-out') {
      // An ended session is ended as everywhere else in the app (docs/web.md § Rules).
      void apiError.resolve(new ApiRequestError(401, 'auth', 'Not signed in'))
    }
    options.schedule()
  }

  function retry(index: number) {
    const failure = failures.get(index)
    if (failure && !failure.retrying) {
      failures.set(index, { ...failure, retrying: true })
      measure(index)
      options.schedule()
    }
  }

  /** Draws the render window one page at a time, visible pages first and nearest the pane's
   *  middle first. A frame that comes while a draw runs starts the queue over once it ends. */
  async function pump() {
    if (pumping) {
      pumpAgain = true
      return
    }
    pumping = true
    try {
      do {
        pumpAgain = false
        const column = options.layout.value
        const el = options.pane.value
        if (!column || !el || options.pinching()) {
          return
        }
        for (const index of renderQueue(column, el.scrollTop, el.clientHeight)) {
          if (pumpAgain || options.layout.value !== column) {
            pumpAgain = true
            break
          }
          await draw(index, column, el)
        }
      } while (pumpAgain)
    } finally {
      pumping = false
    }
  }

  async function draw(index: number, column: Layout, el: HTMLElement) {
    const failure = failures.get(index)
    const info = infos.get(index)
    const host = hosts.get(index)
    const opened = options.handle.value
    // A page waits for its real size, so it is never drawn into a box about to change.
    if ((failure && !failure.retrying) || !info || !host || !opened || !limits || !column.measured[index]) {
      return
    }
    let view = views.get(index)
    if (!view) {
      view = new PageView({ host, document: opened, page: info.page, limits, labels, onNavigate: options.onNavigate })
      views.set(index, view)
    }
    const dpr = window.devicePixelRatio || 1
    const width = host.clientWidth
    const height = host.clientHeight
    const key = `${column.scale}:${dpr}:${width}x${height}`
    if (drawnFor.get(index) !== key && width > 0 && height > 0) {
      // A new base canvas has to fit the total budget beside the others: the furthest pages
      // give theirs up first, and a page that would still not fit waits.
      const backing = backingSize(width, height, dpr, limits.cap, limits.maxDim)
      const live: LiveCanvas[] = [{ index, pixels: backing.width * backing.height }]
      for (const [other, otherView] of views) {
        if (other !== index && otherView.pixels > 0) {
          live.push({ index: other, pixels: otherView.pixels })
        }
      }
      const shown = visibleRange(column, el.scrollTop, el.scrollTop + el.clientHeight) ?? { first: index, last: index }
      const release = canvasesToRelease(live, shown, limits.total)
      if (release.some((canvas) => canvas.index === index)) {
        return
      }
      for (const canvas of release) {
        views.get(canvas.index)?.release()
        drawnFor.delete(canvas.index)
      }
    }
    // A capped page's detail canvas is drawn once the pane has settled.
    const part = options.settled()
      ? visibleRect(column, index, { left: el.scrollLeft, top: el.scrollTop }, options.paneSize.value)
      : null
    try {
      const outcome = await view.draw(column.scale, dpr, part)
      if (outcome === 'cancelled' || !view.painted) {
        return
      }
      drawnFor.set(index, key)
      drawn.value = true
      stopSpinner(index)
      if (failure?.retrying) {
        // A retry that worked moves focus from its button, which goes, to the page.
        const section = sections.get(index)
        if (section?.contains(document.activeElement)) {
          section.focus({ preventScroll: true })
        }
        failures.delete(index)
      }
    } catch (error) {
      view.release()
      drawnFor.delete(index)
      fail(index, error)
    }
  }

  onBeforeUnmount(() => reset(null))

  return { failures, slow, drawn, setSection, setHost, focusSection, userUnit, reset, applySizes, sync, pump, retry }
}
