import { describe, expect, it, vi } from 'vitest'
import { createBuildUpdate } from './build-update'

function setup(latest: string | null | Error = 'new') {
  const reload = vi.fn()
  const update = createBuildUpdate({
    currentId: 'old',
    fetchLatestId: async () => {
      if (latest instanceof Error) {
        throw latest
      }
      return latest
    },
    reload,
  })
  return { update, reload }
}

describe('createBuildUpdate', () => {
  it('reloads an idle page as soon as a newer build is seen', async () => {
    const { update, reload } = setup()
    await update.check()
    expect(reload).toHaveBeenCalledExactlyOnceWith(undefined)
  })

  it('leaves the page alone on the build it is already running', async () => {
    const { update, reload } = setup('old')
    await update.check()
    expect(update.isOutdated()).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it('treats a check that fails or answers nothing as silence', async () => {
    for (const latest of [null, new Error('offline')]) {
      const { update, reload } = setup(latest)
      await update.check()
      expect(update.isOutdated()).toBe(false)
      expect(reload).not.toHaveBeenCalled()
    }
  })

  it('waits for the last hold before reloading', async () => {
    const { update, reload } = setup()
    const uploads = update.hold()
    const viewer = update.hold()
    await update.check()
    expect(reload).not.toHaveBeenCalled()

    viewer()
    viewer() // releasing twice is one release
    expect(reload).not.toHaveBeenCalled()

    uploads()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('turns a navigation into a full load of its target, holds or not', () => {
    const { update, reload } = setup()
    expect(update.navigate('/keys')).toBe(false)

    update.hold()
    update.markOutdated()
    expect(reload).not.toHaveBeenCalled()
    expect(update.navigate('/keys')).toBe(true)
    expect(reload).toHaveBeenCalledExactlyOnceWith('/keys')
  })

  it('reloads once however many triggers arrive', async () => {
    const { update, reload } = setup()
    update.markOutdated()
    await update.check()
    update.navigate('/keys')
    expect(reload).toHaveBeenCalledOnce()
  })
})
