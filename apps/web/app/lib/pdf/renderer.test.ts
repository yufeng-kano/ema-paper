import { describe, expect, it } from 'vitest'
import { notUsedLine, openOptions, readRendererSwitch } from './renderer'

describe('the developer switch', () => {
  it('is on only for pdfRenderer=webgpu, among other parameters too', () => {
    expect(readRendererSwitch('')).toBeNull()
    expect(readRendererSwitch('?pdfRenderer=webgpu')).toBe('webgpu')
    expect(readRendererSwitch('?document=4&pdfRenderer=webgpu')).toBe('webgpu')
  })

  it('is off for any other value', () => {
    expect(readRendererSwitch('?pdfRenderer=WebGPU')).toBeNull()
    expect(readRendererSwitch('?pdfRenderer=')).toBeNull()
    expect(readRendererSwitch('?pdfRenderer')).toBeNull()
    expect(readRendererSwitch('?pdfRenderer=canvas')).toBeNull()
  })
})

describe('the open options', () => {
  it('add nothing without the switch, so the default path opens as before', () => {
    const options = openOptions(null)
    expect(options).toEqual({})
    expect(Reflect.ownKeys(options)).toEqual([])
    expect(Object.isFrozen(options)).toBe(true)
  })

  it('ask for glyph outlines and no system fonts under the switch', () => {
    const options = openOptions('webgpu')
    expect(options).toEqual({ disableFontFace: true, useSystemFonts: false })
    expect(Object.isFrozen(options)).toBe(true)
  })
})

describe('the not-used line', () => {
  it('carries the report prefix and the reason', () => {
    expect(notUsedLine('no adapter')).toBe('[pdf webgpu] not used: no adapter')
  })
})
