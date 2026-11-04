import type { InjectionKey } from 'vue'
import type { IconName } from '~/lib/icons'
import { basename, countKinds, ownFileDocument, splitDuration, type Step, type StepKind } from '~/lib/steps'
import type { StepItem } from '~/lib/thread'

/** Provided by the agent panel in the Files page's card: its steps are about many documents. */
export const libraryStepsKey: InjectionKey<boolean> = Symbol('library-steps')

/** The library batch kinds, whose summary counts the items a call took. */
const ITEM_COUNTED: ReadonlySet<StepKind> = new Set(['move', 'duplicate', 'trash', 'putBack'])

/** The glyph each kind of step is drawn with. */
const ICONS: Record<StepKind, IconName> = {
  read: 'file-text',
  search: 'search',
  list: 'folder',
  count: 'hash',
  run: 'terminal',
  restart: 'refresh',
  edit: 'edit',
  create: 'file-plus',
  page: 'image',
  figure: 'crop',
  skill: 'book-open',
  subagent: 'agent',
  web: 'globe',
  papers: 'book-open',
  fetch: 'link',
  download: 'arrow-down',
  convert: 'scan-text',
  retry: 'rotate-ccw',
  image: 'image',
  browse: 'folder-open',
  folder: 'folder-plus',
  move: 'folder-input',
  rename: 'edit',
  duplicate: 'copy-plus',
  trash: 'trash',
  putBack: 'undo-2',
  newDocument: 'file-plus',
  tool: 'terminal',
}

/** The document's own files, named for what they are: the workspace's file names are the
 *  agent's business, not the reader's (docs/web.md § The agent panel). */
const OWN_FILES: Record<string, string> = {
  'doc.md': 'chat.files.document',
  'notes.md': 'chat.files.notes',
}

/** The kinds whose target is a file. */
const FILE_KINDS: ReadonlySet<StepKind> = new Set(['read', 'count', 'edit', 'create'])

/** The library tools' kinds: their targets are Files paths, the reader's own names. */
const LIBRARY_KINDS: ReadonlySet<StepKind> = new Set([
  'browse',
  'folder',
  'move',
  'rename',
  'duplicate',
  'trash',
  'putBack',
  'newDocument',
])

/**
 * A step's words, through the catalog (docs/web.md § The agent panel): its verb, the detail
 * after its target, a group's summary and the line a live group shows for the step in
 * flight. The reading of the call is `lib/steps.ts`; this only says it.
 */
export function useStepLabels() {
  const { t } = useI18n()
  const library = inject(libraryStepsKey, false)

  function icon(kind: StepKind): IconName {
    return ICONS[kind]
  }

  /** The reader's word for one of the document's own files; null for any other file. In the
   *  Files page's card, where every document has a `doc.md`, the file is named by its
   *  document's Files path (`document`, read off a whole path), the word only when no path
   *  said which. */
  function ownFile(path: string, document = ownFileDocument(path)): string | null {
    const key = OWN_FILES[basename(path)]
    if (!key) {
      return null
    }
    if (library && document) {
      return key === 'chat.files.document' ? document : t('chat.files.notesOf', { document })
    }
    return t(key)
  }

  /** A step's target as the line draws it; `own` is a word, not a name, so not in mono. */
  function target(step: Step): { text: string; own: boolean } {
    if (LIBRARY_KINDS.has(step.kind)) {
      if (step.items !== undefined) {
        return { text: t('chat.stepItems', { n: step.items }, step.items), own: true }
      }
      return { text: step.trash ? t('files.tabTrash') : step.target || t('files.allFiles'), own: true }
    }
    const own = FILE_KINDS.has(step.kind) ? ownFile(step.target, step.document) : null
    return { text: own ?? step.target, own: own !== null }
  }

  function verb(step: Step): string {
    if (step.kind === 'page' || step.kind === 'figure') {
      return t(`chat.steps.${step.kind}`, { page: step.page ?? '?' })
    }
    return t(`chat.steps.${step.kind}`)
  }

  function detail(step: Step): string {
    if (step.place !== undefined) {
      return t('chat.stepTo', { place: step.place || t('files.allFiles') })
    }
    if (step.name) {
      return t('chat.stepRenameTo', { name: step.name })
    }
    if (step.lines) {
      const { from, to } = step.lines
      if (to === null) {
        return t('chat.stepFromLine', { from })
      }
      return to === from ? t('chat.stepLine', { from }) : t('chat.stepLines', { from, to })
    }
    if (step.last) {
      return t('chat.stepLastLines', { n: step.last })
    }
    if (step.file) {
      return t('chat.stepInFile', { file: ownFile(step.file, step.document) ?? step.file })
    }
    return ''
  }

  /** What a group did, counted by kind: "Read 5 times, searched once, edited 7 times". A
   *  library batch counts its items: "moved 3 items", not "moved once". */
  function summary(steps: readonly StepItem[]): string {
    const kinds = steps.flatMap((step): StepKind[] => {
      if (step.type === 'skill') {
        return ['skill']
      }
      if (step.type !== 'tool') {
        return []
      }
      const count = ITEM_COUNTED.has(step.step.kind) ? Math.max(1, step.step.items ?? 1) : 1
      return Array<StepKind>(count).fill(step.step.kind)
    })
    const parts = countKinds(kinds).map(({ kind, count }) => t(`chat.summary.${kind}`, { n: count }, count))
    const text = parts.join(t('chat.summarySeparator'))
    return text.charAt(0).toUpperCase() + text.slice(1)
  }

  /** The step in flight, as the live group's summary says it. */
  function doing(step: Step): string {
    return t(`chat.doing.${step.kind}`, { target: target(step).text, page: step.page ?? '?' })
  }

  function duration(ms: number): string {
    const { minutes, seconds } = splitDuration(ms)
    return minutes ? t('chat.durationMinutes', { m: minutes, s: seconds }) : t('chat.durationSeconds', { s: seconds })
  }

  return { icon, ownFile, target, verb, detail, summary, doing, duration }
}
