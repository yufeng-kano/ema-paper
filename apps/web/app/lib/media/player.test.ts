import { describe, expect, it } from 'vitest'
import { FALLBACK_ASPECT, seekTime, videoBox } from './player'

const DESK = 16

describe('videoBox', () => {
  it('fits a landscape video to the pane’s width less the gutters', () => {
    expect(videoBox({ width: 1920, height: 1080 }, { width: 1000, height: 800 }, DESK)).toEqual({ width: 968, height: 544 })
  })

  it('fits a portrait video to the pane’s height, the surround at its sides', () => {
    expect(videoBox({ width: 1080, height: 1920 }, { width: 1000, height: 800 }, DESK)).toEqual({ width: 432, height: 768 })
  })

  it('lets the width limit a portrait video on a phone, with no gutter', () => {
    // At the full height it would be 393px wide, wider than the 390px pane.
    expect(videoBox({ width: 1080, height: 1920 }, { width: 390, height: 700 }, 0)).toEqual({ width: 390, height: 693 })
  })

  it('grows a small video to the pane', () => {
    expect(videoBox({ width: 320, height: 240 }, { width: 1000, height: 800 }, DESK)).toEqual({ width: 968, height: 726 })
  })

  it('has no box while the size is not known', () => {
    expect(videoBox({ width: 0, height: 0 }, { width: 1000, height: 800 }, DESK)).toBeNull()
    expect(videoBox({ width: 1920, height: 0 }, { width: 1000, height: 800 }, DESK)).toBeNull()
  })

  it('never goes negative in a pane smaller than its gutters', () => {
    expect(videoBox({ width: 1920, height: 1080 }, { width: 20, height: 20 }, DESK)).toEqual({ width: 0, height: 0 })
  })

  it('brings the limiting side to the available size exactly, and keeps the other inside it', () => {
    const height = 800
    for (let paneWidth = 300; paneWidth <= 2000; paneWidth += 7) {
      for (let width = 160; width <= 3840; width += 37) {
        const video = { width, height: Math.round((width * 9) / 16) }
        const box = videoBox(video, { width: paneWidth, height }, DESK)!
        const availWidth = paneWidth - 2 * DESK
        const availHeight = height - 2 * DESK
        if (availWidth / video.width <= availHeight / video.height) {
          expect(box.width).toBe(availWidth)
          expect(box.height).toBeLessThanOrEqual(availHeight)
        } else {
          expect(box.height).toBe(availHeight)
          expect(box.width).toBeLessThanOrEqual(availWidth)
        }
      }
    }
  })

  it('draws the no-preload box at 16:9', () => {
    expect(videoBox(FALLBACK_ASPECT, { width: 1000, height: 800 }, DESK)).toEqual({ width: 968, height: 544 })
  })
})

describe('seekTime', () => {
  it('stops at the start', () => {
    expect(seekTime(3, -5, 60)).toBe(0)
  })

  it('stops at the end of a video of known length', () => {
    expect(seekTime(58, 5, 60)).toBe(60)
  })

  it('has no end while the length is infinite or unknown', () => {
    expect(seekTime(10, 5, Infinity)).toBe(15)
    expect(seekTime(10, 5, NaN)).toBe(15)
  })

  it('counts a time the element cannot give yet as 0', () => {
    expect(seekTime(NaN, 5, 60)).toBe(5)
  })
})
