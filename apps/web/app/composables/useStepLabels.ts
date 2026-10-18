import type { IconName } from '~/lib/icons'
import { basename, countKinds, splitDuration, type Step, type StepKind } from '~/lib/steps'
import type { StepItem } from '~/lib/thread'

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

/**
 * A step's words, through the catalog (docs/web.md § The agent panel): its verb, the detail
 * after its target, a group's summary and the line a live group shows for the step in
 * flight. The reading of the call is `lib/steps.ts`; this only says it.
 */
export function useStepLabels() {
  const { t } = useI18n()

  function icon(kind: StepKind): IconName {
    return ICONS[kind]
  }

  /** The reader's word for one of the document's own files; null for any other file. */
  function ownFile(path: string): string | null {
    const key = OWN_FILES[basename(path)]
    return key ? t(key) : null
  }

  /** A step's target as the line draws it; `own` is a word, not a name, so not in mono. */
  function target(step: Step): { text: string; own: boolean } {
    const own = FILE_KINDS.has(step.kind) ? ownFile(step.target) : null
    return { text: own ?? step.target, own: own !== null }
  }

  function verb(step: Step): string {
    if (step.kind === 'page' || step.kind === 'figure') {
      return t(`chat.steps.${step.kind}`, { page: step.page ?? '?' })
    }
    return t(`chat.steps.${step.kind}`)
  }

  function detail(step: Step): string {
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
      return t('chat.stepInFile', { file: ownFile(step.file) ?? step.file })
    }
    return ''
  }

  /** What a group did, counted by kind: "Read 5 times, searched once, edited 7 times". */
  function summary(steps: readonly StepItem[]): string {
    const kinds = steps.flatMap((step): StepKind[] =>
      step.type === 'tool' ? [step.step.kind] : step.type === 'skill' ? ['skill'] : [],
    )
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
