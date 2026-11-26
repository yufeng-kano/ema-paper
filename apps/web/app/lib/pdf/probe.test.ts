import { describe, expect, it } from 'vitest'
import en from '../../../i18n/locales/en'
import { PROBE_CHECKS, PROBE_COLORS, pickLimits, pixelMatches, reportText, skipped, type ProbeReport } from './probe'

describe('pickLimits', () => {
  it('keeps the listed limits that are numbers and drops the rest', () => {
    const picked = pickLimits({ maxTextureDimension2D: 8192, maxBufferSize: 268435456, maxBindGroups: 'four', other: 1 })
    expect(picked).toEqual({ maxTextureDimension2D: 8192, maxBufferSize: 268435456 })
  })

  it('reads limits that live on a prototype, as GPUSupportedLimits holds them', () => {
    class Limits {
      get maxTextureDimension2D() {
        return 16384
      }
    }
    expect(pickLimits(new Limits())).toEqual({ maxTextureDimension2D: 16384 })
  })
})

describe('pixelMatches', () => {
  it('accepts a pixel within the tolerance on every channel', () => {
    expect(pixelMatches([254, 60, 47, 255], PROBE_COLORS[0]!)).toBe(true)
  })

  it('refuses a pixel one channel away, a cleared canvas, and a short read', () => {
    expect(pixelMatches([255, 59, 60, 255], PROBE_COLORS[0]!)).toBe(false)
    expect(pixelMatches([0, 0, 0, 0], PROBE_COLORS[0]!)).toBe(false)
    expect(pixelMatches([255, 59, 48], PROBE_COLORS[0]!)).toBe(false)
  })
})

describe('PROBE_COLORS', () => {
  it('are told apart by the tolerance, so one canvas never passes for another', () => {
    for (const [i, a] of PROBE_COLORS.entries()) {
      for (const [j, b] of PROBE_COLORS.entries()) {
        expect(pixelMatches(a, b)).toBe(i === j)
      }
    }
  })
})

describe('reportText', () => {
  const report: ProbeReport = {
    userAgent: 'TestAgent/1.0',
    devicePixelRatio: 3,
    hardwareConcurrency: 6,
    canvasFormat: 'bgra8unorm',
    adapter: { vendor: 'apple', architecture: 'metal-3', device: '', description: '' },
    features: ['depth-clip-control', 'texture-compression-astc'],
    limits: { maxTextureDimension2D: 8192 },
    checks: [
      { id: 'gpu', status: 'pass' },
      { id: 'adapter', status: 'pass', ms: 12.4 },
      { id: 'canvases', status: 'fail', detail: '3 of 5 drawn' },
      skipped('worker'),
    ],
  }

  it('lists the checks first, then the device facts, one per line', () => {
    expect(reportText(report).split('\n')).toEqual([
      'gpu: pass',
      'adapter: pass, 12 ms',
      'canvases: fail, 3 of 5 drawn',
      'worker: skipped',
      'userAgent: TestAgent/1.0',
      'devicePixelRatio: 3',
      'hardwareConcurrency: 6',
      'canvasFormat: bgra8unorm',
      'adapter: apple / metal-3',
      'maxTextureDimension2D: 8192',
      'features: depth-clip-control texture-compression-astc',
    ])
  })

  it('leaves out what a browser without WebGPU cannot report', () => {
    const bare: ProbeReport = {
      userAgent: 'Old/1.0',
      devicePixelRatio: 2,
      hardwareConcurrency: 4,
      features: [],
      limits: {},
      checks: [{ id: 'gpu', status: 'fail' }],
    }
    expect(reportText(bare)).toBe('gpu: fail\nuserAgent: Old/1.0\ndevicePixelRatio: 2\nhardwareConcurrency: 4')
  })
})

describe('the page’s words', () => {
  it('name every check and every status', () => {
    for (const id of PROBE_CHECKS) {
      expect(en.pdfProbe.checks[id]).toBeTruthy()
    }
    for (const status of ['pass', 'fail', 'skipped'] as const) {
      expect(en.pdfProbe.status[status]).toBeTruthy()
    }
  })
})
