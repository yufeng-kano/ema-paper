/**
 * Selection and copy over the Original view's text layers (docs/pdf-viewer.md § Stage 1: the
 * Canvas 2D viewer, *Text*): pdf.js's own fix for a selection dragged past the last span of a
 * line, which otherwise jumps to the rest of the page, and a copy as plain text.
 *
 * Ported from `TextLayerBuilder` in pdfjs-dist 6.5.438, web/pdf_viewer.mjs:6201-6419
 * (`#bindMouse`, `#enableGlobalSelectionListener`, `#removeGlobalSelectionListener`), and its
 * `removeNullCharacters` (web/pdf_viewer.mjs:278-284). Copyright 2024 Mozilla Foundation,
 * Apache-2.0 (THIRD_PARTY_NOTICES § 1). What is left out: the find highlighter and the
 * accessibility manager (neither is built in stage 1), the copy permission (not enforced) and
 * the builder's `tabIndex = 0` on the layer, because the pane is the Tab stop.
 */

/** Every live text layer and its end-of-content element. */
const layers = new Map<HTMLElement, HTMLElement>()

/** The document-wide listeners, there while at least one layer is. */
let shared: AbortController | null = null

/** pdf.js's `removeNullCharacters` with its default `replaceInvisible = false`: only NUL goes. */
function removeNullCharacters(text: string): string {
  return text.includes('\0') ? text.replaceAll('\0', '') : text
}

/** Puts a layer's end-of-content element back at its end, as pdf.js's `reset`. */
function reset(end: HTMLElement, layer: HTMLElement): void {
  layer.append(end)
  end.style.width = ''
  end.style.height = ''
  layer.classList.remove('selecting')
}

interface NavigatorWithBrands {
  userAgentData?: { brands: { brand: string; version: string }[] }
}

function enableSharedListeners(): void {
  if (shared) {
    return
  }
  shared = new AbortController()
  const { signal } = shared
  let pointerDown = false
  document.addEventListener(
    'pointerdown',
    () => {
      pointerDown = true
    },
    { signal },
  )
  document.addEventListener(
    'pointerup',
    () => {
      pointerDown = false
      layers.forEach(reset)
    },
    { signal },
  )
  window.addEventListener(
    'blur',
    () => {
      pointerDown = false
      layers.forEach(reset)
    },
    { signal },
  )
  document.addEventListener(
    'keyup',
    () => {
      if (!pointerDown) {
        layers.forEach(reset)
      }
    },
    { signal },
  )

  // Firefox and Chromium 148 and later keep a selection steady on their own; every other
  // engine, Safari among them, needs the end-of-content element moved beside the selection's
  // moving end. Decided once, on the first selection change, as pdf.js does.
  let selfSteadying: boolean | undefined
  let previous: Range | undefined
  document.addEventListener(
    'selectionchange',
    () => {
      const selection = document.getSelection()
      if (!selection || selection.rangeCount === 0) {
        layers.forEach(reset)
        return
      }
      const active = new Set<HTMLElement>()
      for (let index = 0; index < selection.rangeCount; index++) {
        const range = selection.getRangeAt(index)
        if (range.collapsed && !pointerDown) {
          continue
        }
        for (const layer of layers.keys()) {
          if (!active.has(layer) && range.intersectsNode(layer)) {
            active.add(layer)
          }
        }
      }
      for (const [layer, end] of layers) {
        if (active.has(layer)) {
          layer.classList.add('selecting')
        } else {
          reset(end, layer)
        }
      }

      if (selfSteadying === undefined) {
        const first = layers.keys().next().value
        selfSteadying = !!first && getComputedStyle(first).getPropertyValue('-moz-user-select') === 'none'
        if (!selfSteadying) {
          const { userAgentData } = navigator as Navigator & NavigatorWithBrands
          const chromium = userAgentData
            ? userAgentData.brands.find(({ brand }) => brand === 'Chromium')?.version
            : /\bChrome\/(\d+)\b/.exec(navigator.userAgent)?.[1]
          selfSteadying = !!chromium && Number.parseInt(chromium, 10) >= 148
        }
      }
      if (selfSteadying) {
        return
      }

      const range = selection.getRangeAt(0)
      const modifyStart =
        !!previous &&
        (range.compareBoundaryPoints(Range.END_TO_END, previous) === 0 ||
          range.compareBoundaryPoints(Range.START_TO_END, previous) === 0)
      let anchor: Node | null = modifyStart ? range.startContainer : range.endContainer
      if (anchor.nodeType === Node.TEXT_NODE) {
        anchor = anchor.parentNode
      }
      if (!anchor) {
        return
      }
      if (!modifyStart && range.endOffset === 0) {
        // Back to the last node before the selection's end that holds anything.
        do {
          while (!anchor.previousSibling) {
            anchor = anchor.parentNode
            if (!anchor) {
              return
            }
          }
          anchor = anchor.previousSibling
        } while (!anchor.childNodes.length)
      }
      const layer = anchor.parentElement?.closest<HTMLElement>('.textLayer')
      const end = layer ? layers.get(layer) : undefined
      if (layer && end && anchor.parentElement) {
        end.style.width = layer.style.width
        end.style.height = layer.style.height
        end.style.userSelect = 'text'
        anchor.parentElement.insertBefore(end, modifyStart ? anchor : anchor.nextSibling)
      }
      previous = range.cloneRange()
    },
    { signal },
  )
}

/**
 * Takes part in the shared selection handling, once `layer` (a rendered `.textLayer`) has
 * `end` (its `.endOfContent` element) as its last child, and copies a selection that starts in
 * it as plain text: pdf.js's Unicode normalisation (`normalize`, pdf.js's `normalizeUnicode`)
 * with NUL characters removed. The returned function unbinds it; the shared listeners go with
 * the last layer.
 */
export function bindTextLayer(layer: HTMLElement, end: HTMLElement, normalize: (text: string) => string): () => void {
  const own = new AbortController()
  const { signal } = own
  layer.addEventListener(
    'mousedown',
    () => {
      layer.classList.add('selecting')
    },
    { signal },
  )
  layer.addEventListener(
    'copy',
    (event) => {
      if (!event.clipboardData) {
        return
      }
      const text = document.getSelection()?.toString() ?? ''
      event.clipboardData.setData('text/plain', removeNullCharacters(normalize(text)))
      // The copy is this text and nothing else: no rich copy of the spans, and no handler
      // further up (the Markdown view's serializer) writing over it.
      event.preventDefault()
      event.stopPropagation()
    },
    { signal },
  )
  layers.set(layer, end)
  enableSharedListeners()
  return () => {
    own.abort()
    layers.delete(layer)
    if (layers.size === 0) {
      shared?.abort()
      shared = null
    }
  }
}
