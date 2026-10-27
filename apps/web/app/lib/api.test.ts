import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiRequestError,
  addAdminGroupMember,
  createAdminCodes,
  createAdminGroup,
  createKey,
  creditsRefusalOf,
  deleteAccount,
  deleteAdminGroup,
  disableAdminCode,
  getAdminMe,
  getLibrary,
  getMe,
  isDevLoginAvailable,
  listAdminAudit,
  listAdminCodes,
  putAgentSettings,
  putDocumentContent,
  removeAdminGroupMember,
  renameConversation,
  revertRevision,
  revokeKey,
  sendMessage,
  updateAdminGroup,
  conversationEventsUrl,
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
    const fetchMock = stubFetch(Response.json({ folders: [], documents: [], trash: { folders: [], documents: [] } }))

    await getLibrary()

    expect(fetchMock).toHaveBeenCalledWith('/api/library', expect.anything())
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

  it('deletes the account as a mutation, with the CSRF header and no body', async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }))

    await expect(deleteAccount()).resolves.toBeUndefined()

    expect(fetchMock).toHaveBeenCalledWith('/api/account', expect.anything())
    expect(lastInit(fetchMock).method).toBe('DELETE')
    expect(headersOf(fetchMock)['X-Requested-With']).toBe('fetch')
    expect(lastInit(fetchMock).body).toBeUndefined()
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

  it('keeps the envelope’s other fields, which a 402 fills with the refusal', async () => {
    stubFetch(
      Response.json(
        { error: { type: 'insufficient_credits', message: 'Not enough credits', need: 21, remaining: 4, period_end: '2026-11-01T00:00:00+00:00' } },
        { status: 402 },
      ),
    )
    let caught: unknown
    try {
      await getLibrary()
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(ApiRequestError)
    expect(creditsRefusalOf(caught)).toEqual({ need: 21, remaining: 4, period_end: '2026-11-01T00:00:00+00:00' })
    // Any other failure, or a 402 whose fields are not the refusal's, is not one.
    expect(creditsRefusalOf(new ApiRequestError(500, 'internal', 'boom'))).toBeNull()
    expect(creditsRefusalOf(new ApiRequestError(402, 'insufficient_credits', 'short', { need: '21' }))).toBeNull()
    expect(creditsRefusalOf(new Error('x'))).toBeNull()
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

describe('workspace and chat routes', () => {
  it('sends a save with the revision it was made from', async () => {
    const fetchMock = stubFetch(Response.json({ revision: 'b' }))

    await expect(putDocumentContent(7, { markdown: '# x', base_revision: 'a' })).resolves.toEqual({
      revision: 'b',
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/library/documents/7/content', expect.anything())
    expect(lastInit(fetchMock).method).toBe('PUT')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ markdown: '# x', base_revision: 'a' }))
  })

  it('surfaces a stale save as a 409', async () => {
    stubFetch(Response.json({ error: { type: 'invalid_request', message: 'Stale' } }, { status: 409 }))

    const error = (await putDocumentContent(7, { markdown: '', base_revision: 'old' }).catch(
      (thrown: unknown) => thrown,
    )) as ApiRequestError

    expect(error.status).toBe(409)
  })

  it('posts a message with its selection and reads the queued turn', async () => {
    const fetchMock = stubFetch(
      Response.json({ turn_id: 'a3f1c2d4-0000-4000-8000-000000000012', message_id: 5 }, { status: 202 }),
    )

    await expect(
      sendMessage(3, { text: 'Fix table 1', selection: '| 3450 |', locale: 'zh-TW' }),
    ).resolves.toEqual({ turn_id: 'a3f1c2d4-0000-4000-8000-000000000012', message_id: 5 })
    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/3/messages', expect.anything())
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string).locale).toBe('zh-TW')
    expect(headersOf(fetchMock)['X-Requested-With']).toBe('fetch')
  })

  it('addresses a revert by its commit, escaped into the path', async () => {
    const fetchMock = stubFetch(Response.json({ revision: 'c' }))

    await revertRevision(7, 'abc/def')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/library/documents/7/revisions/abc%2Fdef/revert',
      expect.anything(),
    )
    expect(lastInit(fetchMock).method).toBe('POST')
  })

  it('renames a conversation with a PUT of its title alone', async () => {
    const saved = { id: 3, title: 'Table 2', created_at: 'a', updated_at: 'b' }
    const fetchMock = stubFetch(Response.json(saved))

    await expect(renameConversation(3, 'Table 2')).resolves.toEqual(saved)
    expect(fetchMock).toHaveBeenCalledWith('/api/conversations/3', expect.anything())
    expect(lastInit(fetchMock).method).toBe('PUT')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ title: 'Table 2' }))
    expect(headersOf(fetchMock)['X-Requested-With']).toBe('fetch')
  })

  it('builds the event stream URL from the last seen sequence', () => {
    expect(conversationEventsUrl(3, 41)).toBe('/api/conversations/3/events?after=41')
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


describe('the operator console', () => {
  it('reads its own view of the account as a plain GET', async () => {
    const me = {
      user: { id: 1, email: 'a@example.com', name: null, picture: null },
      permissions: [],
      reauth_required: false,
      reauth_hours: 12,
    }
    const fetchMock = stubFetch(Response.json(me))

    await expect(getAdminMe()).resolves.toEqual(me)
    expect(fetchMock).toHaveBeenCalledWith('/api/admin/me', expect.anything())
    expect(headersOf(fetchMock)['X-Requested-With']).toBeUndefined()
  })

  it('sends every group change as a mutation, with the CSRF header', async () => {
    const group = {
      id: 4,
      name: 'support',
      builtin: false,
      permissions: [],
      stored_permissions: [],
      member_count: 0,
      updated_at: '',
      updated_by: null,
    }
    const member = { user_id: 9, email: 'b@example.com', name: null, added_at: '', added_by: null }
    const fetchMock = stubFetch(
      Response.json(group, { status: 201 }),
      Response.json(group),
      Response.json(member, { status: 201 }),
      new Response(null, { status: 204 }),
      new Response(null, { status: 204 }),
    )

    await createAdminGroup({ name: 'support', permissions: [] })
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/groups', expect.anything())
    expect(lastInit(fetchMock).method).toBe('POST')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ name: 'support', permissions: [] }))

    await updateAdminGroup(4, { permissions: ['codes.read'], based_on: [] })
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/groups/4', expect.anything())
    expect(lastInit(fetchMock).method).toBe('PATCH')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ permissions: ['codes.read'], based_on: [] }))

    await expect(addAdminGroupMember(4, { email: 'b@example.com' })).resolves.toEqual(member)
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/groups/4/members', expect.anything())
    expect(lastInit(fetchMock).body).toBe(JSON.stringify({ email: 'b@example.com' }))

    await expect(removeAdminGroupMember(4, 9)).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/groups/4/members/9', expect.anything())
    expect(lastInit(fetchMock).method).toBe('DELETE')

    await expect(deleteAdminGroup(4)).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/groups/4', expect.anything())
    expect(lastInit(fetchMock).method).toBe('DELETE')

    for (const [, init] of fetchMock.mock.calls) {
      expect((init?.headers as Record<string, string>)['X-Requested-With']).toBe('fetch')
    }
  })

  it('pages the audit log, sending only the filters that are set, encoded', async () => {
    const page = { items: [], total: 0, page: 2, page_size: 50 }
    const fetchMock = stubFetch(Response.json(page), Response.json(page))

    await listAdminAudit({ page: 2 })
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/audit?page=2', expect.anything())

    await listAdminAudit({ page: 1, area: 'groups', q: 'a+b@example.com' })
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/admin/audit?page=1&area=groups&q=a%2Bb%40example.com',
      expect.anything(),
    )
  })

  it('pages the redeem codes, sending only the filters that are set, encoded', async () => {
    const page = { items: [], total: 0, page: 1, page_size: 50 }
    const fetchMock = stubFetch(Response.json(page), Response.json(page))

    await listAdminCodes({ page: 1 })
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/codes?page=1', expect.anything())

    await listAdminCodes({ page: 3, status: 'all', q: 'launch & vip' })
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/codes?page=3&status=all&q=launch+%26+vip', expect.anything())
    expect(headersOf(fetchMock)['X-Requested-With']).toBeUndefined()
  })

  it('creates and disables codes as mutations, with the CSRF header', async () => {
    const body = { kind: 'credits' as const, credits: 500, valid_days: 365, count: 2, max_uses: 1, label: 'launch' }
    const fetchMock = stubFetch(Response.json({ codes: [] }, { status: 201 }), Response.json({ id: 7 }))

    await createAdminCodes(body)
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/codes', expect.anything())
    expect(lastInit(fetchMock).method).toBe('POST')
    expect(lastInit(fetchMock).body).toBe(JSON.stringify(body))

    await disableAdminCode(7)
    expect(fetchMock).toHaveBeenLastCalledWith('/api/admin/codes/7/disable', expect.anything())
    expect(lastInit(fetchMock).method).toBe('POST')

    for (const [, init] of fetchMock.mock.calls) {
      expect((init?.headers as Record<string, string>)['X-Requested-With']).toBe('fetch')
    }
  })

  it('keeps the reason of a console conflict beside its message', async () => {
    stubFetch(
      Response.json(
        { error: { type: 'admin_conflict', message: 'The last member stays', reason: 'last_owner' } },
        { status: 409 },
      ),
    )

    const error = await removeAdminGroupMember(1, 2).catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiRequestError)
    expect((error as ApiRequestError).type).toBe('admin_conflict')
    expect((error as ApiRequestError).details).toEqual({ reason: 'last_owner' })
  })
})
