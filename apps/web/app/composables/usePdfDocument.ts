import { classifyLoadError, type LoadErrorKind } from '~/lib/pdf/errors'
import { openPdf, type PdfDocumentHandle, type PdfOpening, type PdfPageInfo, type PdfPasswordRequest } from '~/lib/pdf/pdfjs'

/** Where opening a PDF stands: reading it, asking for its password, failed, or open with page
 *  1's size known. */
export type PdfDocumentStatus = 'opening' | 'password' | 'failed' | 'open'

/** The password prompt: why pdf.js asked, which ask this is (each one focuses the field
 *  again), and whether a password handed over is still being checked. */
export interface PdfPasswordPrompt {
  reason: 'need' | 'incorrect'
  attempt: number
  checking: boolean
}

/**
 * The Original view's document, apart from its pane (docs/pdf-viewer.md § Stage 1: the Canvas
 * 2D viewer, *Loading*, *Password*, *Errors*): opening the file through `app/lib/pdf/pdfjs.ts`,
 * the password prompt, the failure kind, and page 1, whose size the layout starts from.
 *
 * An opening that a newer one (a retry, another URL) or the unmount overtook settles into
 * nothing: every await checks it still belongs to the latest opening, since a destroyed
 * document can resolve late.
 */
export function usePdfDocument() {
  const config = useRuntimeConfig()
  const status = ref<PdfDocumentStatus>('opening')
  const prompt = ref<PdfPasswordPrompt | null>(null)
  const failure = ref<Exclude<LoadErrorKind, 'cancelled'> | null>(null)
  const handle = shallowRef<PdfDocumentHandle | null>(null)
  const firstPage = shallowRef<PdfPageInfo | null>(null)

  let opening: PdfOpening | null = null
  let generation = 0
  let request: PdfPasswordRequest | null = null

  /** Destroys whatever is open or opening; what it would still have said is dropped. */
  function close() {
    generation++
    request = null
    void opening?.destroy()
    opening = null
    handle.value = null
    firstPage.value = null
  }

  async function open(url: string) {
    close()
    const mine = generation
    status.value = 'opening'
    prompt.value = null
    failure.value = null
    const current = openPdf({
      url,
      assetBase: config.public.pdfjsBase,
      onPassword(asked) {
        if (mine !== generation) {
          return
        }
        request = asked
        prompt.value = { reason: asked.reason, attempt: (prompt.value?.attempt ?? 0) + 1, checking: false }
        status.value = 'password'
      },
    })
    opening = current
    try {
      const opened = await current.promise
      const first = await opened.getPageInfo(1)
      if (mine !== generation) {
        return
      }
      handle.value = opened
      firstPage.value = first
      prompt.value = null
      status.value = 'open'
    } catch (error) {
      if (mine !== generation) {
        return
      }
      const kind = classifyLoadError(error, navigator.onLine === false)
      if (kind === 'cancelled') {
        return
      }
      // A file that cannot be shown keeps no worker running behind its error.
      void current.destroy()
      opening = null
      prompt.value = null
      failure.value = kind
      status.value = 'failed'
    }
  }

  /** Hands the password to pdf.js alone; nothing here keeps it. */
  function submitPassword(password: string) {
    if (!request || !prompt.value) {
      return
    }
    prompt.value = { ...prompt.value, checking: true }
    const asked = request
    request = null
    asked.submit(password)
  }

  onBeforeUnmount(close)

  return { status, prompt, failure, handle, firstPage, open, close, submitPassword }
}
