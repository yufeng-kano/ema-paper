import type { UsageResponse, UsageTotals } from './api'

export interface UsageRow {
  /** A date (`YYYY-MM-DD`) for the per-day table, a model id for the per-model table. */
  label: string
  /** Billing only (the historical OpenRouter rows) — estimates and unpriced calls are never
   *  folded in. */
  cost: number
  /** Billed calls. 0 means "nothing here was billed", so `cost` is no figure. */
  billedCalls: number
  estimatedCost: number
  estimatedCalls: number
  unpricedCalls: number
  promptTokens: number
  completionTokens: number
  /** Prompt-cache reads — chat rows only; 0 for a parse-only row. */
  cacheReadTokens: number
  cacheWriteTokens: number
  /** The agents that ran in this bucket (`parse`, `chat`); empty for a model row. */
  agents: string[]
  /** 0..1 — this row's billed cost against the largest in the same table, for the inline bar. */
  costShare: number
}

export interface UsageSummary {
  totalCost: number
  totalBilledCalls: number
  totalEstimatedCost: number
  totalEstimatedCalls: number
  totalUnpricedCalls: number
  totalPromptTokens: number
  totalCompletionTokens: number
  days: UsageRow[]
  models: UsageRow[]
  isEmpty: boolean
}

/**
 * Display transform for `GET /api/usage`: totals, plus per-row bar shares. Days come back
 * from the backend oldest first and are flipped here so the newest is on top, matching the
 * job history. Billed, estimated and unpriced stay apart all the way to the screen
 * (docs/api.md § Usage aggregates).
 */
export function summarizeUsage(usage: UsageResponse): UsageSummary {
  const days = withCostShares(
    usage.per_day.map((entry) => toRow(entry.date, entry, entry.agents)),
  ).reverse()
  const models = withCostShares(usage.per_model.map((entry) => toRow(entry.model, entry))).sort(
    (left, right) => right.cost - left.cost,
  )

  return {
    totalCost: sum(usage.per_day.map((entry) => entry.cost)),
    totalBilledCalls: sum(days.map((row) => row.billedCalls)),
    totalEstimatedCost: sum(usage.per_day.map((entry) => entry.estimated_cost)),
    totalEstimatedCalls: sum(usage.per_day.map((entry) => entry.estimated_calls)),
    totalUnpricedCalls: sum(usage.per_day.map((entry) => entry.unpriced_calls)),
    totalPromptTokens: sum(usage.per_day.map((entry) => entry.prompt_tokens)),
    totalCompletionTokens: sum(usage.per_day.map((entry) => entry.completion_tokens)),
    days,
    models,
    isEmpty: usage.per_day.length === 0 && usage.per_model.length === 0,
  }
}

function toRow(label: string, entry: UsageTotals, agents: string[] = []): UsageRow {
  return {
    label,
    cost: entry.cost,
    billedCalls: entry.calls - entry.estimated_calls - entry.unpriced_calls,
    estimatedCost: entry.estimated_cost,
    estimatedCalls: entry.estimated_calls,
    unpricedCalls: entry.unpriced_calls,
    promptTokens: entry.prompt_tokens,
    completionTokens: entry.completion_tokens,
    cacheReadTokens: entry.cache_read_tokens ?? 0,
    cacheWriteTokens: entry.cache_write_tokens ?? 0,
    agents: [...agents].sort(),
    costShare: 0,
  }
}

/**
 * Cache-read tokens over prompt tokens, 0..1 — the figure that says whether the prompt
 * cache is working (docs/chat.md § Prompt caching). Null when nothing was prompted: a
 * ratio of nothing is not 0%.
 */
/**
 * Cache reads over all input: the uncached input tokens the call reported plus what it
 * read from and wrote to the cache — the three are disjoint, and their sum is the prompt
 * (docs/chat.md § Prompt caching).
 */
export function cacheReadRatio(
  cacheReadTokens: number,
  promptTokens: number,
  cacheWriteTokens = 0,
): number | null {
  const input = promptTokens + cacheReadTokens + cacheWriteTokens
  return input > 0 ? Math.max(0, Math.min(1, cacheReadTokens / input)) : null
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0)
}

function withCostShares(rows: UsageRow[]): UsageRow[] {
  const largest = Math.max(0, ...rows.map((row) => row.cost))
  return rows.map((row) => ({ ...row, costShare: largest > 0 ? row.cost / largest : 0 }))
}
