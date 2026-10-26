/**
 * What a row is, in words (docs/web.md § Files, *Four columns*): a PDF or an image reads
 * the same converted or not, a markdown document is one, and any other file is named by
 * its type — `fileKind` picks the catalog key and the format's name, and the catalog says
 * it in the reader's language.
 */
import type { LibraryDocument } from '~/lib/api'
import { fileKind, imageFormat } from '~/lib/library'

export function useKindLabel() {
  const { t } = useI18n()
  return (document: Pick<LibraryDocument, 'kind' | 'name' | 'media_type'>): string => {
    if (document.kind === 'markdown') {
      return t('files.kinds.markdown')
    }
    if (document.kind === 'pdf') {
      return t('files.kinds.pdf')
    }
    if (document.kind === 'image') {
      // A parsed image's row carries no media type: its extension names the format.
      return t('files.kinds.image', { format: imageFormat(document.name) }).trim()
    }
    const kind = fileKind(document)
    return t(`files.kinds.${kind.key}`, { format: kind.format })
  }
}
