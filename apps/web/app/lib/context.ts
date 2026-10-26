/**
 * What the composer's context ring and its card read off a conversation (docs/web.md § The
 * agent panel, *The model pill and the context ring*): the ring's fill and hue, the tokens
 * as a person reads them, and the cache's state at a given moment. Pure, so the edges — a
 * compaction that left the size unknown, a model that caches nothing, a clock that just
 * passed — are unit tested; the components own the drawing and the half-minute tick.
 */
import type { Conversation } from './api'

export type RingLevel = 'accent' | 'warning' | 'danger'

/** The ring's fill: the context over the window, 0 while no context is known (the draft,
 *  and a compacted conversation until its next call reports one), never past 1. */
export function contextFill(
  conversation: Pick<Conversation, 'context_tokens' | 'context_window'> | null | undefined,
): number {
  const tokens = conversation?.context_tokens ?? null
  const window = conversation?.context_window ?? 0
  if (tokens === null || window <= 0) {
    return 0
  }
  return Math.min(1, Math.max(0, tokens / window))
}

/** The accent to 70 %, the warning hue to 90 %, the danger hue past it. */
export function ringLevel(fill: number): RingLevel {
  return fill > 0.9 ? 'danger' : fill > 0.7 ? 'warning' : 'accent'
}

/** Tokens as a person reads them: under a thousand as they are, then `1.5K`, `62K`, `1.2M`. */
export function formatTokens(tokens: number): string {
  const scaled = (value: number, unit: string): string =>
    `${value >= 10 ? Math.round(value) : Math.round(value * 10) / 10}${unit}`
  if (tokens >= 1_000_000) {
    return scaled(tokens / 1_000_000, 'M')
  }
  if (tokens >= 1000) {
    return scaled(tokens / 1000, 'K')
  }
  return String(Math.max(0, Math.round(tokens)))
}

/** The ring's tooltip: `62K / 200K · 31 %`; nothing while no context is known. */
export function contextLine(
  conversation: Pick<Conversation, 'context_tokens' | 'context_window'> | null | undefined,
): string | null {
  const tokens = conversation?.context_tokens ?? null
  const window = conversation?.context_window ?? 0
  if (tokens === null || window <= 0) {
    return null
  }
  return `${formatTokens(tokens)} / ${formatTokens(window)} · ${Math.round(contextFill(conversation) * 100)} %`
}

/**
 * The cache as the card says it (docs/chat.md § Compaction, *The cache clock and the
 * context*): `warm` with the minutes left while the clock has not passed, `expired` with
 * what a cold next message re-reads once it has, `none` on a conversation that has run on a
 * model whose usage names no cache; null while there is nothing to say — the draft, and a
 * compacted conversation whose next call has not reported yet.
 */
export type CacheState =
  | { kind: 'warm'; minutesLeft: number }
  | { kind: 'expired'; recacheCredits: number | null }
  | { kind: 'none' }

export function cacheState(
  conversation: Pick<Conversation, 'context_tokens' | 'cache_warm_until' | 'recache_credits'> | null | undefined,
  now: number,
): CacheState | null {
  if (!conversation) {
    return null
  }
  const until = conversation.cache_warm_until ? Date.parse(conversation.cache_warm_until) : Number.NaN
  if (!Number.isNaN(until)) {
    const left = until - now
    return left > 0
      ? { kind: 'warm', minutesLeft: Math.max(1, Math.ceil(left / 60_000)) }
      : { kind: 'expired', recacheCredits: conversation.recache_credits ?? null }
  }
  return conversation.context_tokens == null ? null : { kind: 'none' }
}
