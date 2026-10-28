/**
 * What a failed turn says (docs/web.md § The agent panel). A `turn_error`'s `error` is the
 * worker's, in English and written for the log; the thread shows a catalog line instead —
 * what happened and what to do next — and keeps the worker's text for the tooltip.
 */

/** The worker's exact errors (`jobs/chat_runner.py`, `jobs/queue.py`). */
const EXACT: Record<string, string> = {
  out_of_credits: 'chat.errors.outOfCredits',
  storage_full: 'chat.errors.storageFull',
  model_unpriced: 'chat.errors.modelUnavailable',
  'the turn timed out': 'chat.errors.timedOut',
  'the upstream reported exhausted credits': 'chat.errors.serviceUnavailable',
  'the upstream rate-limited': 'chat.errors.busy',
  'The answer reached the output limit before it was complete': 'chat.errors.outputLimit',
  'The model declined this request': 'chat.errors.declined',
  "the conversation's document no longer exists": 'chat.errors.documentGone',
  "the document's parse no longer exists": 'chat.errors.documentGone',
  'worker restarted': 'chat.errors.restarted',
  'internal error': 'chat.errors.internal',
  'Kano Proxy is not configured on this deployment': 'chat.errors.notConfigured',
  'No chat model is offered on this deployment': 'chat.errors.notConfigured',
  compact_empty: 'chat.errors.compactEmpty',
}

/** The ones whose text carries a number or the upstream's own words. */
const PREFIXES: [string, string][] = [
  ['The turn made more than', 'chat.errors.tooManySteps'],
  ["This conversation's history is too large", 'chat.errors.historyFull'],
]

/** The catalog key for a turn's `error`; anything unknown is the generic line. */
export function turnErrorKey(error: string): string {
  return EXACT[error] ?? PREFIXES.find(([prefix]) => error.startsWith(prefix))?.[1] ?? 'chat.errors.generic'
}

/** The errors the plan can lift: their line carries *See plans*. */
export function turnErrorNeedsPlan(error: string): boolean {
  return error === 'out_of_credits' || error === 'storage_full'
}
