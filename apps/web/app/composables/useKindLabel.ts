/**
 * What a row is, in words (docs/web.md § Files, *Four columns*): a document by what it was
 * converted from, a file by its type — `fileKind` picks the catalog key and the format's
 * name, and the catalog says it in the reader's language.
 */
import type { LibraryDocument } from '~/lib/api'
import { fileKind } from '~/lib/library'

export function useKindLabel() {
  const { t } = useI18n()
  return (document: Pick<LibraryDocument, 'kind' | 'name' | 'media_type'>): string => {
    if (document.kind === 'file') {
      const kind = fileKind(document)
      return t(`files.kinds.${kind.key}`, { format: kind.format })
    }
    if (document.kind === 'markdown') {
      return t('files.kinds.documentMarkdown')
    }
    return t(document.kind === 'image' ? 'files.kinds.documentImage' : 'files.kinds.documentPdf')
  }
}
