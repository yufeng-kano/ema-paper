import { afterEach, describe, expect, it, vi } from 'vitest'
import { injectedModel, loadParseModels, modelLabel } from './models'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('modelLabel', () => {
  it('names a model by its catalog name and id', () => {
    expect(modelLabel({ id: 'gpt-x', name: 'GPT X' })).toBe('GPT X · gpt-x')
  })

  it('falls back to the id when the catalog gives no name', () => {
    expect(modelLabel({ id: 'gpt-x', name: null })).toBe('gpt-x')
  })
})

describe('injectedModel', () => {
  const models = [
    { id: 'a', name: null },
    { id: 'b', name: 'B' },
  ]

  it('adds a current choice the catalog does not list', () => {
    expect(injectedModel('gone', models)).toBe('gone')
  })

  it('adds the current choice while the catalog has not loaded', () => {
    expect(injectedModel('a', null)).toBe('a')
  })

  it('adds nothing for a listed choice or no choice', () => {
    expect(injectedModel('b', models)).toBeNull()
    expect(injectedModel(null, models)).toBeNull()
    expect(injectedModel(undefined, models)).toBeNull()
    expect(injectedModel('', models)).toBeNull()
  })
})

describe('loadParseModels', () => {
  it('reads the registry and the parse catalog, sorted by id', async () => {
    const agents = [{ name: 'parse', settings: { model: 'b' } }]
    const fetchMock = vi.fn<typeof fetch>((input) =>
      Promise.resolve(
        String(input) === '/api/agents'
          ? Response.json({ agents })
          : Response.json({
              data: [
                { id: 'c', name: null },
                { id: 'a', name: 'A' },
                { id: 'b', name: null },
              ],
            }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const loaded = await loadParseModels()

    expect(fetchMock.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      '/api/agents',
      '/api/agents/parse/models',
    ])
    expect(loaded.agents).toEqual(agents)
    expect(loaded.models.map((model) => model.id)).toEqual(['a', 'b', 'c'])
  })
})
