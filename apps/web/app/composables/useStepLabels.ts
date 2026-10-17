import type { IconName } from '~/lib/icons'
import { countKinds, splitDuration, type Step, type StepKind } from '~/lib/steps'
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
      return t('chat.stepInFile', { file: step.file })
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
    return t(`chat.doing.${step.kind}`, { target: step.target, page: step.page ?? '?' })
  }

  function duration(ms: number): string {
    const { minutes, seconds } = splitDuration(ms)
    return minutes ? t('chat.durationMinutes', { m: minutes, s: seconds }) : t('chat.durationSeconds', { s: seconds })
  }

  return { icon, verb, detail, summary, doing, duration }
}
