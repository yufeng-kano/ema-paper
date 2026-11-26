/**
 * What the More menu of a file's viewer offers under the file's facts (docs/web.md § Files,
 * the file viewer): Rename, Duplicate, Show in Files and Move to Trash, each handed to the
 * Files page by its key. The file viewer's, and the result viewer's for a markdown file, which
 * is a file however it is read (§ Result viewer, *A markdown file opens on its text*).
 */
import type { MenuItem } from '~/lib/menu'

export type FileMenuKey = 'rename' | 'duplicate' | 'show' | 'trash'

export function useFileMenu() {
  const { t } = useI18n()
  return computed<(MenuItem & { key: FileMenuKey })[]>(() => [
    { key: 'rename', label: t('files.rename'), icon: 'edit', separated: true },
    { key: 'duplicate', label: t('files.duplicate'), icon: 'copy-plus' },
    { key: 'show', label: t('viewer.file.showInFiles'), icon: 'folder-open' },
    { key: 'trash', label: t('files.moveToTrash'), icon: 'trash', danger: true, separated: true },
  ])
}
