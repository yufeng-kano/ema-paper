import { describe, expect, it } from 'vitest'
import en from '../../../i18n/locales/en'
import {
  PROBE_CHECKS,
  PROBE_COLORS,
  PROBE_NOTES,
  PROBE_SURFACES,
  errorText,
  pickLimits,
  pixelMatches,
  readVerdict,
  reportText,
  rgbaText,
  skipped,
  type ProbeReport,
} from './probe'

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
    secureContext: true,
    devicePixelRatio: 3,
    hardwareConcurrency: 6,
    canvasFormat: 'bgra8unorm',
    adapter: { vendor: 'apple', architecture: 'metal-3', device: '', description: '' },
    features: ['depth-clip-control', 'texture-compression-astc'],
    limits: { maxTextureDimension2D: 8192 },
    checks: [
      { id: 'gpu', status: 'pass' },
      { id: 'adapter', status: 'pass', ms: 12.4 },
      {
        id: 'canvases',
        status: 'fail',
        notes: [
          { code: 'drawn', params: { count: 3, total: 5 } },
          { code: 'wrongColor', params: { got: '0 0 0 255', want: '255 59 48 255' } },
        ],
        detail: 'Device is lost',
      },
      skipped('worker'),
    ],
  }

  it('lists the checks first, then the device facts, one per line and no name twice', () => {
    const names = reportText(report)
      .split('\n')
      .map((line) => line.slice(0, line.indexOf(':')))
    expect(names).toHaveLength(new Set(names).size)

    expect(reportText(report).split('\n')).toEqual([
      'gpu: pass',
      'adapter: pass, 12 ms',
      'canvases: fail, drawn(count=3, total=5), wrongColor(got=0 0 0 255, want=255 59 48 255), Device is lost',
      'worker: skipped',
      'userAgent: TestAgent/1.0',
      'secureContext: true',
      'devicePixelRatio: 3',
      'hardwareConcurrency: 6',
      'canvasFormat: bgra8unorm',
      'adapterInfo: apple / metal-3',
      'maxTextureDimension2D: 8192',
      'features: depth-clip-control texture-compression-astc',
    ])
  })

  it('leaves out what a browser without WebGPU cannot report', () => {
    const bare: ProbeReport = {
      userAgent: 'Old/1.0',
      secureContext: false,
      devicePixelRatio: 2,
      hardwareConcurrency: 4,
      features: [],
      limits: {},
      checks: [{ id: 'gpu', status: 'fail', notes: [{ code: 'insecureContext' }] }],
    }
    expect(reportText(bare).split('\n')).toEqual([
      'gpu: fail, insecureContext',
      'userAgent: Old/1.0',
      'secureContext: false',
      'devicePixelRatio: 2',
      'hardwareConcurrency: 4',
    ])
  })
})

describe('readVerdict', () => {
  const red = PROBE_COLORS[0]!

  it('passes a read of the color drawn, with nothing to add', () => {
    expect(readVerdict({ pixel: [255, 59, 48, 255] }, red)).toEqual({ status: 'pass' })
  })

  it('fails another color and says both, so a swap of red and blue reads as one', () => {
    expect(readVerdict({ pixel: [48, 59, 255, 255] }, red)).toEqual({
      status: 'fail',
      notes: [{ code: 'wrongColor', params: { got: '48 59 255 255', want: '255 59 48 255' } }],
    })
  })

  it('fails a read that has no pixel with its note or the browser’s message', () => {
    expect(readVerdict({ note: 'noContext' }, red)).toEqual({ status: 'fail', notes: [{ code: 'noContext' }], detail: undefined })
    expect(readVerdict({ error: 'usage is not allowed' }, red)).toEqual({
      status: 'fail',
      notes: undefined,
      detail: 'usage is not allowed',
    })
  })
})

describe('rgbaText', () => {
  it('prints a pixel’s four bytes, from an array or a typed array', () => {
    expect(rgbaText([0, 122, 255, 255])).toBe('0 122 255 255')
    expect(rgbaText(new Uint8ClampedArray([255, 204, 0, 255]))).toBe('255 204 0 255')
  })
})

describe('PROBE_SURFACES', () => {
  it('each has a color of its own to be drawn in, and one more is left for the device that is lost', () => {
    expect(PROBE_SURFACES.length).toBeLessThan(PROBE_COLORS.length)
  })
})

describe('errorText', () => {
  it("is an error's message, and anything else as a string", () => {
    expect(errorText(new TypeError('Device is lost'))).toBe('Device is lost')
    expect(errorText('plain')).toBe('plain')
  })
})

describe('the page’s words', () => {
  it('say every note, with the numbers and names the note carries', () => {
    for (const code of PROBE_NOTES) {
      expect(en.pdfProbe.notes[code]).toBeTruthy()
    }
    expect(en.pdfProbe.notes.drawn).toContain('{count}')
    expect(en.pdfProbe.notes.drawn).toContain('{total}')
    expect(en.pdfProbe.notes.lost).toContain('{reason}')
    for (const code of ['wrongColor', 'readNow', 'readLater', 'bitmapTransfer', 'bitmapCopy', 'bitmapAfterWork'] as const) {
      expect(en.pdfProbe.notes[code]).toContain('{got}')
    }
    expect(en.pdfProbe.notes.wrongColor).toContain('{want}')
  })

  it('name every way a canvas is drawn', () => {
    expect(en.pdfProbe.surfaces.once).toBeTruthy()
    for (const surface of PROBE_SURFACES) {
      expect(en.pdfProbe.surfaces[surface]).toBeTruthy()
    }
  })

  it('name every check and every status', () => {
    for (const id of PROBE_CHECKS) {
      expect(en.pdfProbe.checks[id]).toBeTruthy()
    }
    for (const status of ['pass', 'fail', 'skipped'] as const) {
      expect(en.pdfProbe.status[status]).toBeTruthy()
    }
  })
})
