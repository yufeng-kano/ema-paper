import type { UsageResponse, UsageTotals } from './api'

export interface UsageRow {
  /** A date (`YYYY-MM-DD`) for the per-day table, a model id for the per-model table. */
  label: string
  /** OpenRouter's billing only — estimates and unpriced calls are never folded in. */
  cost: number
  /** Calls OpenRouter billed. 0 means "nothing here was billed", so `cost` is no figure. */
  billedCalls: number
  estimatedCost: number
  estimatedCalls: number
  unpricedCalls: number
  promptTokens: number
  completionTokens: number
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
  const days = withCostShares(usage.per_day.map((entry) => toRow(entry.date, entry))).reverse()
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

function toRow(label: string, entry: UsageTotals): UsageRow {
  return {
    label,
    cost: entry.cost,
    billedCalls: entry.calls - entry.estimated_calls - entry.unpriced_calls,
    estimatedCost: entry.estimated_cost,
    estimatedCalls: entry.estimated_calls,
    unpricedCalls: entry.unpriced_calls,
    promptTokens: entry.prompt_tokens,
    completionTokens: entry.completion_tokens,
    costShare: 0,
  }
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0)
}

function withCostShares(rows: UsageRow[]): UsageRow[] {
  const largest = Math.max(0, ...rows.map((row) => row.cost))
  return rows.map((row) => ({ ...row, costShare: largest > 0 ? row.cost / largest : 0 }))
}
