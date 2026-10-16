import { describe, expect, it } from 'vitest'
import type { UsageResponse } from './api'
import { cacheReadRatio, summarizeUsage } from './usage'

const billedOnly = {
  calls: 1,
  estimated_cost: 0,
  estimated_calls: 0,
  unpriced_calls: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  agents: ['parse'],
}

const usage: UsageResponse = {
  days: 30,
  per_day: [
    { date: '2026-08-18', prompt_tokens: 1000, completion_tokens: 200, cost: 0.02, ...billedOnly },
    { date: '2026-08-19', prompt_tokens: 3000, completion_tokens: 400, cost: 0.08, ...billedOnly },
    { date: '2026-08-20', prompt_tokens: 500, completion_tokens: 100, cost: 0.01, ...billedOnly },
  ],
  per_model: [
    { model: 'vendor/cheap', prompt_tokens: 500, completion_tokens: 100, cost: 0.01, ...billedOnly },
    { model: 'vendor/pricey', prompt_tokens: 4000, completion_tokens: 600, cost: 0.1, ...billedOnly },
  ],
  per_agent: [],
}

describe('summarizeUsage', () => {
  it('totals cost and tokens across the period', () => {
    const summary = summarizeUsage(usage)

    expect(summary.totalCost).toBeCloseTo(0.11)
    expect(summary.totalPromptTokens).toBe(4500)
    expect(summary.totalCompletionTokens).toBe(700)
    expect(summary.isEmpty).toBe(false)
  })

  it('lists the newest day first and the most expensive model first', () => {
    const summary = summarizeUsage(usage)

    expect(summary.days.map((row) => row.label)).toEqual(['2026-08-20', '2026-08-19', '2026-08-18'])
    expect(summary.models.map((row) => row.label)).toEqual(['vendor/pricey', 'vendor/cheap'])
  })

  it('scales bar shares against the largest cost in the same table', () => {
    const summary = summarizeUsage(usage)

    expect(summary.days[0]?.costShare).toBeCloseTo(0.125)
    expect(summary.days[1]?.costShare).toBe(1)
    expect(summary.models[0]?.costShare).toBe(1)
    expect(summary.models[1]?.costShare).toBeCloseTo(0.1)
  })

  it('reports zero shares rather than dividing by zero when nothing cost anything', () => {
    const summary = summarizeUsage({
      days: 7,
      per_day: [{ date: '2026-08-20', prompt_tokens: 10, completion_tokens: 0, cost: 0, ...billedOnly }],
      per_model: [],
      per_agent: [],
    })

    expect(summary.days[0]?.costShare).toBe(0)
    expect(summary.isEmpty).toBe(false)
  })

  it('is empty when the account has no usage at all', () => {
    const summary = summarizeUsage({ days: 30, per_day: [], per_model: [], per_agent: [] })

    expect(summary.isEmpty).toBe(true)
    expect(summary.totalCost).toBe(0)
  })

  it('keeps estimates and unpriced calls out of the billed cost', () => {
    const summary = summarizeUsage({
      days: 30,
      per_day: [
        {
          date: '2026-10-14',
          agents: ['chat', 'parse'],
          calls: 6,
          prompt_tokens: 60,
          completion_tokens: 6,
          cost: 0.01,
          estimated_cost: 0.5,
          estimated_calls: 3,
          unpriced_calls: 2,
          cache_read_tokens: 30,
          cache_write_tokens: 0,
        },
      ],
      per_model: [
        {
          model: 'proxy/model',
          calls: 5,
          prompt_tokens: 50,
          completion_tokens: 5,
          cost: 0,
          estimated_cost: 0.5,
          estimated_calls: 3,
          unpriced_calls: 2,
          cache_read_tokens: 30,
          cache_write_tokens: 0,
        },
      ],
      per_agent: [],
    })

    expect(summary.totalCost).toBe(0.01)
    expect(summary.totalBilledCalls).toBe(1)
    expect(summary.totalEstimatedCost).toBe(0.5)
    expect(summary.totalEstimatedCalls).toBe(3)
    expect(summary.totalUnpricedCalls).toBe(2)
    expect(summary.days[0]?.billedCalls).toBe(1)
    // A model nothing billed reads as "no bill", not as a $0 one — and ranks no bar.
    expect(summary.models[0]?.billedCalls).toBe(0)
    expect(summary.models[0]?.costShare).toBe(0)
    expect(summary.days[0]?.agents).toEqual(['chat', 'parse'])
    expect(summary.days[0]?.cacheReadTokens).toBe(30)
  })
})

describe('cacheReadRatio', () => {
  it('is the cache-read share of all input: uncached plus reads plus writes', () => {
    expect(cacheReadRatio(90, 10)).toBeCloseTo(0.9)
    expect(cacheReadRatio(30, 60, 10)).toBeCloseTo(0.3)
  })

  it('is null, not zero, when nothing was prompted', () => {
    expect(cacheReadRatio(0, 0)).toBeNull()
  })

  it('never exceeds one', () => {
    expect(cacheReadRatio(80, 0)).toBe(1)
  })
})
