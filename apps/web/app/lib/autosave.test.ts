import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Autosave, type Box, type SaveState } from './autosave'

class Conflict extends Error {}

interface Put {
  text: string
  base: string
  resolve: (revision: string) => void
  reject: (error: unknown) => void
}

/** An autosave over a text the test edits, its PUTs held until the test answers them. */
function harness(documentId: number | null = 7) {
  const state: Box<SaveState> = { value: 'saved' }
  const error: Box<string | null> = { value: null }
  const revision: Box<string> = { value: 'r0' }
  const file = { text: 'one' }
  const puts: Put[] = []
  const onConflict = vi.fn(async () => {})
  const onFailure = vi.fn()
  const reload = vi.fn()
  const saves = new Autosave({
    state,
    error,
    revision,
    documentId: () => documentId,
    snapshot: () => file.text,
    put: (_id, text, base) =>
      new Promise((resolve, reject) => {
        puts.push({ text, base, resolve: (next) => resolve({ revision: next }), reject })
      }),
    isConflict: (failure) => failure instanceof Conflict,
    onConflict,
    describe: async (failure) => `failed: ${(failure as Error).message}`,
    onFailure,
    reload,
    delay: 1000,
  })
  return { saves, state, error, revision, file, puts, onConflict, onFailure, reload }
}

/** Lets the awaited PUT and the steps after it run. */
async function settled() {
  await vi.advanceTimersByTimeAsync(0)
}

describe('autosave', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('sends a change once the debounce passes with no other', async () => {
    const { saves, state, revision, file, puts } = harness()
    file.text = 'two'
    saves.schedule()
    expect(state.value).toBe('editing')
    await vi.advanceTimersByTimeAsync(999)
    file.text = 'three'
    saves.schedule()
    await vi.advanceTimersByTimeAsync(999)
    expect(puts).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(puts.map((put) => [put.text, put.base])).toEqual([['three', 'r0']])
    expect(state.value).toBe('saving')
    puts[0]!.resolve('r1')
    await settled()
    expect(state.value).toBe('saved')
    expect(revision.value).toBe('r1')
  })

  it('saves nothing without a document', async () => {
    const { saves, puts } = harness(null)
    await saves.save()
    expect(puts).toHaveLength(0)
  })

  it('sends a save asked for mid-flight after it, from the revision it produced', async () => {
    const { saves, state, file, puts } = harness()
    file.text = 'two'
    const first = saves.save()
    file.text = 'three'
    state.value = 'editing'
    expect(saves.save()).toBe(first)
    expect(puts).toHaveLength(1)
    puts[0]!.resolve('r1')
    await settled()
    expect(puts.map((put) => [put.text, put.base])).toEqual([
      ['two', 'r0'],
      ['three', 'r1'],
    ])
    puts[1]!.resolve('r2')
    await settled()
    expect(state.value).toBe('saved')
  })

  it('schedules a change made during a save that nothing asked to send', async () => {
    const { saves, state, file, puts } = harness()
    file.text = 'two'
    void saves.save()
    file.text = 'three'
    puts[0]!.resolve('r1')
    await settled()
    expect(state.value).toBe('editing')
    expect(puts).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(puts[1]?.text).toBe('three')
  })

  it('takes a conflict as saved, hands it over, and settles false', async () => {
    const { saves, state, puts, onConflict, onFailure } = harness()
    void saves.save()
    const settling = saves.settle()
    puts[0]!.reject(new Conflict('409'))
    expect(await settling).toBe(false)
    expect(state.value).toBe('saved')
    expect(onConflict).toHaveBeenCalledOnce()
    expect(onFailure).not.toHaveBeenCalled()
    expect(await saves.settle()).toBe(true)
  })

  it('keeps any other failure as unsaved work, with its reason', async () => {
    const { saves, state, error, puts, onConflict, onFailure } = harness()
    void saves.save()
    puts[0]!.reject(new Error('offline'))
    await settled()
    expect(state.value).toBe('failed')
    expect(error.value).toBe('failed: offline')
    expect(onFailure).toHaveBeenCalledOnce()
    expect(onConflict).not.toHaveBeenCalled()
    void saves.save()
    expect(error.value).toBeNull()
  })

  it('gives a failed save one more try when settled, and settles false if it fails again', async () => {
    const { saves, state, file, puts } = harness()
    file.text = 'two'
    void saves.save()
    puts[0]!.reject(new Error('offline'))
    await settled()
    expect(state.value).toBe('failed')
    const settling = saves.settle()
    expect(puts.map((put) => put.text)).toEqual(['two', 'two'])
    puts[1]!.reject(new Error('offline'))
    expect(await settling).toBe(false)
    expect(state.value).toBe('failed')
    expect(puts).toHaveLength(2)
  })

  it('settles true once the retried save lands, so the reader can choose the action again', async () => {
    const { saves, state, revision, file, puts } = harness()
    file.text = 'two'
    void saves.save()
    puts[0]!.reject(new Error('offline'))
    await settled()
    const settling = saves.settle()
    puts[1]!.resolve('r1')
    expect(await settling).toBe(true)
    expect(state.value).toBe('saved')
    expect(revision.value).toBe('r1')
  })

  it('makes a reload deferred behind the save once it settles', async () => {
    const { saves, puts, reload } = harness()
    void saves.save()
    saves.reloadAfterSave = true
    expect(reload).not.toHaveBeenCalled()
    puts[0]!.resolve('r1')
    await settled()
    expect(reload).toHaveBeenCalledOnce()
    expect(saves.reloadAfterSave).toBe(false)
  })

  it('sends a pending change at once on close, and retries a failed save', async () => {
    const { saves, state, file, puts } = harness()
    saves.flush()
    expect(puts).toHaveLength(0)
    file.text = 'two'
    saves.schedule()
    saves.flush()
    expect(puts.map((put) => put.text)).toEqual(['two'])
    puts[0]!.reject(new Error('offline'))
    await settled()
    expect(state.value).toBe('failed')
    saves.flush()
    expect(puts).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1000)
    expect(puts).toHaveLength(2)
  })
})
