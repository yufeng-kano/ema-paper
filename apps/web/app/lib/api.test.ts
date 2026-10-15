import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiRequestError,
  createKey,
  getMe,
  isDevLoginAvailable,
  kanoConnectUrl,
  listJobs,
  putAgentSettings,
  revokeKey,
} from './api'

function stubFetch(...responses: Response[]) {
  const fetchMock = vi.fn<typeof fetch>()
  for (const response of responses) {
    fetchMock.mockResolvedValueOnce(response)
  }
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function lastInit(fetchMock: ReturnType<typeof stubFetch>): RequestInit {
  return fetchMock.mock.calls.at(-1)?.[1] as RequestInit
}

function headersOf(fetchMock: ReturnType<typeof stubFetch>): Record<string, string> {
  return (lastInit(fetchMock).headers ?? {}) as Record<string, string>
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('request', () => {
  it('sends session credentials and no CSRF header on reads', async () => {
    const fetchMock = stubFetch(Response.json({ jobs: [] }))

    await listJobs(50)

    expect(fetchMock).toHaveBeenCalledWith('/api/jobs?limit=50', expect.anything())
    expect(lastInit(fetchMock).credentials).toBe('include')
    expect(lastInit(fetchMock).method).toBe('GET')
    expect(headersOf(fetchMock)['X-Requested-With']).toBeUndefined()
  })

  it('pairs mutations with the X-Requested-With header and a JSON body', async () => {
    const fetchMock = stubFetch(Response.json({ id: 1, name: 'ci', prefix: 'sr_…', created_at: '', key: 'sr_x' }))

    await createKey('ci')

    expect(lastInit(fetchMock).method).toBe('POST')
    expect(headersOf(fetchMock)['X-Requested-With']).toBe('fetch')
    expect(headersOf(fetchMock)['Content-Type']).toBe('application/json')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ name: 'ci' }))
  })

  it('accepts an empty 204 body', async () => {
    stubFetch(new Response(null, { status: 204 }))

    await expect(revokeKey(7)).resolves.toBeUndefined()
  })

  it('raises the error envelope type and message', async () => {
    stubFetch(
      Response.json(
        { error: { type: 'invalid_request', message: "No such agent 'nope'" } },
        { status: 404 },
      ),
    )

    const error = await putAgentSettings('nope', { model: null }).catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiRequestError)
    expect((error as ApiRequestError).status).toBe(404)
    expect((error as ApiRequestError).type).toBe('invalid_request')
    expect((error as ApiRequestError).message).toBe("No such agent 'nope'")
  })

  it('flags a 401 as unauthorized', async () => {
    stubFetch(Response.json({ error: { type: 'auth', message: 'Not signed in' } }, { status: 401 }))

    const error = (await getMe().catch((thrown: unknown) => thrown)) as ApiRequestError

    expect(error.isUnauthorized).toBe(true)
  })

  it('reports a non-JSON failure without leaking the body', async () => {
    stubFetch(new Response('<html>502</html>', { status: 502, statusText: 'Bad Gateway' }))

    const error = (await getMe().catch((thrown: unknown) => thrown)) as ApiRequestError

    expect(error.status).toBe(502)
    expect(error.type).toBe('internal')
    expect(error.message).toBe('Bad Gateway')
  })

  it('reports a transport failure as status 0', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetchMock)

    const error = (await getMe().catch((thrown: unknown) => thrown)) as ApiRequestError

    expect(error.isOffline).toBe(true)
    expect(error.message).toBe('Failed to fetch')
  })
})

describe('isDevLoginAvailable', () => {
  it('is false when the route does not exist', async () => {
    stubFetch(Response.json({ error: { type: 'invalid_request', message: 'Not Found' } }, { status: 404 }))

    await expect(isDevLoginAvailable()).resolves.toBe(false)
  })

  it('probes without the CSRF header so no session can be created', async () => {
    const fetchMock = stubFetch(
      Response.json({ error: { type: 'auth', message: 'Missing X-Requested-With header' } }, { status: 403 }),
    )

    await expect(isDevLoginAvailable()).resolves.toBe(true)
    expect(lastInit(fetchMock).method).toBe('POST')
    expect(lastInit(fetchMock).headers).toBeUndefined()
  })

  it('is false when the backend cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockRejectedValue(new TypeError('offline')))

    await expect(isDevLoginAvailable()).resolves.toBe(false)
  })
})

describe('kanoConnectUrl', () => {
  it('carries the locale so the callback returns to the same language', () => {
    expect(kanoConnectUrl('zh-TW')).toBe('/api/kano/connect?locale=zh-TW')
  })

  it('cannot be steered by a crafted locale', () => {
    // The value is a query parameter, never a path: the server allowlists it.
    expect(kanoConnectUrl('../../evil?x=1&y')).toBe(
      '/api/kano/connect?locale=..%2F..%2Fevil%3Fx%3D1%26y',
    )
  })

  it('omits the parameter when there is no locale', () => {
    expect(kanoConnectUrl('')).toBe('/api/kano/connect')
  })
})
