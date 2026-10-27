import { describe, expect, it } from 'vitest'
import { areaPath, chartPoints, labelIndexes, linePath, nearestIndex, niceTicks, tickY, type ChartBox } from './chart'

const BOX: ChartBox = { width: 440, height: 200, left: 40, right: 0, top: 0, bottom: 20 }

describe('the gridlines', () => {
  it('land on round amounts from 0 up to the first at or above the highest value', () => {
    expect(niceTicks(4.12)).toEqual([0, 2, 4, 6])
    expect(niceTicks(104.54)).toEqual([0, 50, 100, 150])
    expect(niceTicks(10)).toEqual([0, 2.5, 5, 7.5, 10])
    expect(niceTicks(0.37)).toEqual([0, 0.1, 0.2, 0.3, 0.4])
    expect(niceTicks(0.0042)).toEqual([0, 0.002, 0.004, 0.006])
  })

  it('keep a chart of nothing on its floor', () => {
    expect(niceTicks(0)).toEqual([0, 1])
    expect(niceTicks(Number.NaN)).toEqual([0, 1])
  })
})

describe('the points', () => {
  it('spread the days across the plot, 0 on the floor and the ceiling at the top', () => {
    expect(chartPoints([0, 5, 10], BOX, 10)).toEqual([
      { x: 40, y: 180 },
      { x: 240, y: 90 },
      { x: 440, y: 0 },
    ])
    expect(tickY(5, BOX, 10)).toBe(90)
  })

  it('put a lone day in the middle, and a ceiling of 0 on the floor', () => {
    expect(chartPoints([3], BOX, 0)).toEqual([{ x: 240, y: 180 }])
  })

  it('draw the line and close the wash down to the floor', () => {
    const points = chartPoints([0, 10], BOX, 10)
    expect(linePath(points)).toBe('M40 180 L440 0')
    expect(areaPath(points, 180)).toBe('M40 180 L440 0 L440 180 L40 180 Z')
    expect(areaPath([], 180)).toBe('')
  })

  it('snap the pointer to the nearest day', () => {
    const points = chartPoints([1, 2, 3, 4, 5], BOX, 5)
    expect(nearestIndex(0, points)).toBe(0)
    expect(nearestIndex(180, points)).toBe(1)
    expect(nearestIndex(500, points)).toBe(4)
    expect(nearestIndex(10, [])).toBe(-1)
  })
})

describe('the dates under the axis', () => {
  it('label every day of a short range', () => {
    expect(labelIndexes(3)).toEqual([0, 1, 2])
  })

  it('label a few evenly spaced days of a long one, the first and the last among them', () => {
    expect(labelIndexes(30)).toEqual([0, 7, 15, 22, 29])
    expect(labelIndexes(7, 3)).toEqual([0, 3, 6])
    expect(labelIndexes(0)).toEqual([])
  })
})
