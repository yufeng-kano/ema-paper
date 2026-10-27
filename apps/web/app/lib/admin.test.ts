import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'
import {
  ADMIN_AREAS,
  AUDIT_FILTER_AREAS,
  CONSOLE_PAGES,
  adminFailure,
  areaOf,
  auditAreaOf,
  can,
  canDeleteGroup,
  canManageMembers,
  canRenameGroup,
  canToggle,
  changedPermissions,
  conflictKey,
  consoleDestination,
  firstReadableArea,
  grantNumber,
  idOf,
  isGroupName,
  navEntries,
  pageOf,
  PAGE_MAX,
  toggledPermissions,
  visibleAreas,
} from './admin'
import { ApiRequestError } from './api'

/** The API's `ALL_PERMISSIONS`, as `GET /api/admin/groups` sends it. */
const ALL = ADMIN_AREAS.flatMap((area) => area.levels.map((level) => `${area.key}.${level}`))

describe('the areas', () => {
  it("list the API's permissions in its order, audit with a read only", () => {
    expect(ALL).toEqual([
      'users.read',
      'users.write',
      'codes.read',
      'codes.write',
      'jobs.read',
      'jobs.write',
      'groups.read',
      'groups.write',
      'audit.read',
      'settings.read',
      'settings.write',
    ])
  })

  it('offer the areas with a write to the audit filter', () => {
    expect(AUDIT_FILTER_AREAS).toEqual(['users', 'codes', 'jobs', 'groups', 'settings'])
  })
})

describe('what a permission list shows', () => {
  it('reads a permission string, never a near one', () => {
    expect(can(['groups.read'], 'groups', 'read')).toBe(true)
    expect(can(['groups.read'], 'groups', 'write')).toBe(false)
    expect(can(['groups.write'], 'groups', 'read')).toBe(false)
    expect(can([], 'audit', 'read')).toBe(false)
  })

  it('shows the areas it can read, in area order', () => {
    expect(visibleAreas(['audit.read', 'codes.read', 'codes.write'])).toEqual(['codes', 'audit'])
    expect(visibleAreas(['users.write'])).toEqual([])
  })

  it("lists only the console pages whose area it can read, in nav order", () => {
    expect(navEntries(ALL).map((page) => page.area)).toEqual(['users', 'groups', 'audit'])
    expect(navEntries(['audit.read']).map((page) => page.path)).toEqual(['/admin/audit'])
    expect(navEntries(['users.read', 'users.write']).map((page) => page.path)).toEqual(['/admin/users'])
    expect(navEntries(['codes.read', 'jobs.read'])).toEqual([])
  })

  it('sends /admin to the first page it can read', () => {
    expect(firstReadableArea(ALL)?.path).toBe('/admin/users')
    expect(firstReadableArea(['audit.read'])?.path).toBe('/admin/audit')
    expect(consoleDestination(['audit.read', 'groups.read'], null)).toBe('/admin/groups')
  })

  it('keeps a readable page, replaces an unreadable one, and finds nothing for no page', () => {
    expect(consoleDestination(['audit.read', 'groups.read'], 'audit')).toBe('/admin/audit')
    expect(consoleDestination(['audit.read'], 'groups')).toBe('/admin/audit')
    // An account in no group, and one whose areas' pages come in later steps.
    expect(consoleDestination([], null)).toBeNull()
    expect(consoleDestination(['codes.read', 'users.write'], 'groups')).toBeNull()
  })

  it("reads a page's area meta, and nothing else", () => {
    expect(areaOf('groups')).toBe('groups')
    expect(areaOf('owner')).toBeNull()
    expect(areaOf(undefined)).toBeNull()
  })
})

describe('the permission matrix', () => {
  it('ticks the read with a write, and keeps the API order', () => {
    expect(toggledPermissions(['audit.read'], 'codes', 'write', true, ALL)).toEqual([
      'codes.read',
      'codes.write',
      'audit.read',
    ])
  })

  it('unticks the write with its read, and the write alone', () => {
    expect(toggledPermissions(['codes.read', 'codes.write'], 'codes', 'read', false, ALL)).toEqual([])
    expect(toggledPermissions(['codes.read', 'codes.write'], 'codes', 'write', false, ALL)).toEqual(['codes.read'])
  })

  it('names what a change hands out or takes away', () => {
    expect(changedPermissions(['codes.read'], ['codes.read', 'codes.write'])).toEqual(['codes.write'])
    expect(changedPermissions(['codes.read', 'codes.write'], [])).toEqual(['codes.read', 'codes.write'])
  })

  const support = { builtin: false, permissions: ['users.read', 'codes.read', 'codes.write'] }
  const operator = ['users.read', 'codes.read', 'codes.write', 'groups.read', 'groups.write']

  it('lets an operator tick and untick only what they hold', () => {
    expect(canToggle(operator, support, 'users', 'read', ALL)).toBe(true)
    expect(canToggle(operator, support, 'codes', 'write', ALL)).toBe(true)
    // Not held: ticking it would hand it out.
    expect(canToggle(operator, support, 'jobs', 'read', ALL)).toBe(false)
    expect(canToggle(operator, support, 'users', 'write', ALL)).toBe(false)
  })

  it("refuses an untick that would take away a write the operator lacks along with the read", () => {
    const group = { builtin: false, permissions: ['jobs.read', 'jobs.write'] }
    expect(canToggle(['jobs.read', 'groups.write'], group, 'jobs', 'read', ALL)).toBe(false)
    expect(canToggle(['jobs.read', 'jobs.write', 'groups.write'], group, 'jobs', 'read', ALL)).toBe(true)
  })

  it('leaves a group stronger than the operator alone, even a permission they hold', () => {
    const stronger = { builtin: false, permissions: ['users.read', 'audit.read'] }
    const deputy = ['groups.read', 'groups.write', 'audit.read']
    expect(canToggle(deputy, stronger, 'audit', 'read', ALL)).toBe(false)
    expect(canToggle(deputy, stronger, 'groups', 'read', ALL)).toBe(false)
    expect(canToggle(deputy, { builtin: false, permissions: ['audit.read'] }, 'groups', 'read', ALL)).toBe(true)
  })

  it('touches nothing without groups.write, and never the built-in group', () => {
    expect(canToggle(['users.read', 'groups.read'], support, 'users', 'read', ALL)).toBe(false)
    expect(canToggle(ALL, { builtin: true, permissions: ALL }, 'users', 'read', ALL)).toBe(false)
  })
})

describe('managing a group', () => {
  const owner = { builtin: true, permissions: ALL }
  const finance = { builtin: false, permissions: ['codes.read', 'codes.write'] }

  it("manages members only for someone holding every permission of the group", () => {
    expect(canManageMembers(ALL, owner)).toBe(true)
    expect(canManageMembers(['codes.read', 'codes.write', 'groups.write'], finance)).toBe(true)
    expect(canManageMembers(['codes.read', 'codes.write', 'groups.write'], owner)).toBe(false)
    expect(canManageMembers(['codes.read', 'groups.write'], finance)).toBe(false)
    expect(canManageMembers(['codes.read', 'codes.write', 'groups.read'], finance)).toBe(false)
  })

  it('deletes a group on the same terms, never the built-in one', () => {
    expect(canDeleteGroup(['codes.read', 'codes.write', 'groups.write'], finance)).toBe(true)
    expect(canDeleteGroup(['codes.read', 'groups.write'], finance)).toBe(false)
    expect(canDeleteGroup(ALL, owner)).toBe(false)
  })

  it('renames with groups.write alone, never the built-in group', () => {
    expect(canRenameGroup(['groups.write'], finance)).toBe(true)
    expect(canRenameGroup(['groups.read'], finance)).toBe(false)
    expect(canRenameGroup(ALL, owner)).toBe(false)
  })

  it('takes a name of 1–32 lowercase letters, digits, - and _, not starting with - or _', () => {
    for (const name of ['support', 'finance-2', 'a', '0ps_team', 'x'.repeat(32)]) {
      expect(isGroupName(name)).toBe(true)
    }
    for (const name of ['', 'Support', '-ops', '_ops', 'two words', 'café', 'x'.repeat(33)]) {
      expect(isGroupName(name)).toBe(false)
    }
  })
})

describe('the route query', () => {
  it('reads a page from 1, anything else as 1', () => {
    expect(pageOf('3')).toBe(3)
    // Past the API's bound: the bound, which the list then replaces with its last page.
    expect(pageOf('3000000000')).toBe(PAGE_MAX)
    for (const value of [undefined, '', '0', '-2', '1.5', 'x', ['2']]) {
      expect(pageOf(value)).toBe(1)
    }
  })

  it('reads a group id, or none', () => {
    expect(idOf('12')).toBe(12)
    expect(idOf('0')).toBeNull()
    expect(idOf('abc')).toBeNull()
    expect(idOf(undefined)).toBeNull()
  })

  it('reads an audit filter only for an area the filter offers', () => {
    expect(auditAreaOf('codes')).toBe('codes')
    expect(auditAreaOf('audit')).toBeNull()
    expect(auditAreaOf('nope')).toBeNull()
  })
})

describe("a grant's numbers", () => {
  it('takes a whole number from 1 to the cap, typed with spaces around it', () => {
    expect(grantNumber('500', 1000)).toBe(500)
    expect(grantNumber(' 1000 ', 1000)).toBe(1000)
    expect(grantNumber('1', 1)).toBe(1)
  })

  it('refuses nothing, zero, a fraction, a sign, an exponent and anything past the cap', () => {
    for (const typed of ['', ' ', '0', '1.5', '-3', '+3', '1e3', '12a', '1001', '99999999999999999999']) {
      expect(grantNumber(typed, 1000)).toBeNull()
    }
  })
})

describe('a failed console request', () => {
  const failed = (status: number, type: ConstructorParameters<typeof ApiRequestError>[1], details = {}) =>
    adminFailure(new ApiRequestError(status, type, 'message', details))

  it('maps each refusal to the state the page takes', () => {
    expect(failed(401, 'auth')).toEqual({ kind: 'signedOut' })
    expect(failed(403, 'reauth_required')).toEqual({ kind: 'reauth' })
    expect(failed(403, 'permission_denied')).toEqual({ kind: 'denied' })
    expect(failed(409, 'admin_conflict', { reason: 'last_owner' })).toEqual({ kind: 'conflict', reason: 'last_owner' })
    expect(failed(409, 'admin_conflict')).toEqual({ kind: 'conflict', reason: null })
    expect(failed(404, 'invalid_request')).toEqual({ kind: 'notFound' })
  })

  it("leaves a missing CSRF header's 403, a validation error, an outage and a non-API error to their message", () => {
    expect(failed(403, 'auth')).toEqual({ kind: 'other' })
    expect(failed(422, 'invalid_request')).toEqual({ kind: 'other' })
    expect(failed(0, 'internal')).toEqual({ kind: 'other' })
    expect(adminFailure(new Error('boom'))).toEqual({ kind: 'other' })
  })

  it("words the reasons it knows and leaves the rest to the server's message", () => {
    expect(conflictKey('name_taken')).toBe('admin.conflicts.name_taken')
    expect(conflictKey('something_new')).toBeNull()
    expect(conflictKey(null)).toBeNull()
  })
})

describe('the console catalog', () => {
  /** Every leaf key under an object, dotted. */
  const keys = (node: unknown, prefix = ''): string[] =>
    node && typeof node === 'object'
      ? Object.entries(node).flatMap(([key, value]) => keys(value, prefix ? `${prefix}.${key}` : key))
      : [prefix]

  const lookup = (catalog: unknown, path: string): unknown =>
    path.split('.').reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], catalog)

  it('has the same keys in English and Traditional Chinese', () => {
    expect(keys(zhTW.admin).sort()).toEqual(keys(en.admin).sort())
    expect(typeof zhTW.nav.admin).toBe('string')
    expect(typeof en.nav.admin).toBe('string')
  })

  it("words every area, every level of it, every console page and every known conflict, in both", () => {
    for (const catalog of [en, zhTW]) {
      for (const area of ADMIN_AREAS) {
        expect(typeof lookup(catalog, `admin.areas.${area.key}`)).toBe('string')
        for (const level of area.levels) {
          expect(typeof lookup(catalog, `admin.permissions.${area.key}.${level}`)).toBe('string')
        }
      }
      for (const page of CONSOLE_PAGES) {
        expect(typeof lookup(catalog, page.label)).toBe('string')
      }
      for (const reason of ['name_taken', 'builtin', 'last_owner', 'email_ambiguous', 'stale']) {
        expect(typeof lookup(catalog, conflictKey(reason)!)).toBe('string')
      }
    }
  })

  it('writes no Simplified-only character in the Traditional Chinese console copy', () => {
    const text = keys(zhTW.admin)
      .map((key) => String(lookup(zhTW.admin, key)))
      .join('')
    // Simplified forms of characters this copy uses: 后台组权限录删设户证 …
    expect(text).not.toMatch(/[后组权录删设户证们这个时为对应开关们]/)
  })
})
