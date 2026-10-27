/**
 * The operator console's permission logic, framework-free (docs/admin.md § Permissions,
 * § Groups, § Pages): the areas and their levels, the console's pages in nav order, what a
 * permission list lets an account see and touch, the permission matrix's rules, the group
 * name rule, and what a failed console request means for the page.
 *
 * Every rule here is the server's too, and the server is what enforces it: these only decide
 * what a page offers, so an operator is not handed a control the API would refuse. Code
 * compares permission strings and the `builtin` flag — never a group's name.
 */
import { ApiRequestError, type AdminCodesCreated } from './api'
import type { CostDay } from './chart'
import type { IconName } from './icons'

export type AdminLevel = 'read' | 'write'

/**
 * The areas, in the order the API's `ALL_PERMISSIONS` lists them, with their levels: two
 * strings per area, `<area>.read` and `<area>.write`, except `audit`, whose records are never
 * changed. A new area is a line here, its two strings in the API and its words in both
 * catalogs (`admin.areas`, `admin.permissions`).
 */
export const ADMIN_AREAS = [
  { key: 'users', levels: ['read', 'write'] },
  { key: 'codes', levels: ['read', 'write'] },
  { key: 'jobs', levels: ['read', 'write'] },
  { key: 'groups', levels: ['read', 'write'] },
  { key: 'audit', levels: ['read'] },
  { key: 'settings', levels: ['read', 'write'] },
] as const satisfies readonly { key: string; levels: readonly AdminLevel[] }[]

export type AdminArea = (typeof ADMIN_AREAS)[number]['key']

/** The areas the audit log's filter offers: those with a write, the only ones that change
 *  anything (docs/admin.md § Pages, *Audit*). */
export const AUDIT_FILTER_AREAS: readonly AdminArea[] = ADMIN_AREAS.filter((area) =>
  (area.levels as readonly AdminLevel[]).includes('write'),
).map((area) => area.key)

export interface ConsolePage {
  /** The area whose `read` shows the page. */
  area: AdminArea
  /** Without the locale prefix: callers pass it through `localePath`. */
  path: string
  icon: IconName
  /** Catalog key of the nav entry. */
  label: string
}

/**
 * The console's pages in nav order — the order the sidebar lists them and `/admin` picks the
 * first readable one from. Later steps add theirs in their places (docs/admin.md § Roadmap).
 */
export const CONSOLE_PAGES: readonly ConsolePage[] = [
  // `/admin` itself is the Overview, `jobs.read`'s like Jobs and Usage (docs/admin.md § Pages).
  { area: 'jobs', path: '/admin', icon: 'layout-grid', label: 'admin.nav.overview' },
  { area: 'users', path: '/admin/users', icon: 'user', label: 'admin.nav.users' },
  { area: 'codes', path: '/admin/codes', icon: 'ticket', label: 'admin.nav.codes' },
  { area: 'jobs', path: '/admin/jobs', icon: 'list-checks', label: 'admin.nav.jobs' },
  { area: 'jobs', path: '/admin/usage', icon: 'dashboard', label: 'admin.nav.usage' },
  { area: 'groups', path: '/admin/groups', icon: 'users-round', label: 'admin.nav.groups' },
  { area: 'audit', path: '/admin/audit', icon: 'scroll-text', label: 'admin.nav.audit' },
]

/** Whether a permission list holds `<area>.<level>`. */
export function can(permissions: readonly string[], area: AdminArea, level: AdminLevel): boolean {
  return permissions.includes(`${area}.${level}`)
}

/** The areas whose `read` the list holds, in area order. */
export function visibleAreas(permissions: readonly string[]): AdminArea[] {
  return ADMIN_AREAS.filter((area) => can(permissions, area.key, 'read')).map((area) => area.key)
}

/** The sidebar's console entries: the pages whose area the list can read. */
export function navEntries(permissions: readonly string[]): ConsolePage[] {
  return CONSOLE_PAGES.filter((page) => can(permissions, page.area, 'read'))
}

/** The first console page, in nav order, whose area the list can read; null when it reads
 *  none — an account in no group, or one whose areas' pages come later. */
export function firstReadableArea(permissions: readonly string[]): ConsolePage | null {
  return navEntries(permissions)[0] ?? null
}

/** The area a page's `adminArea` meta names; null for anything else (`/admin` itself). */
export function areaOf(value: unknown): AdminArea | null {
  return ADMIN_AREAS.find((area) => area.key === value)?.key ?? null
}

/**
 * Where a visit to a console page lands (docs/admin.md § Pages): the area's first page when
 * its area is readable — the `admin` middleware then leaves the page where it is — else the
 * first readable page, and null — the Not Found page — when the account can read none. `/admin`
 * is the Overview, a `jobs` page, so an account without `jobs.read` is sent on from it; a page
 * with no area always is.
 */
export function consoleDestination(permissions: readonly string[], area: AdminArea | null): string | null {
  const first = firstReadableArea(permissions)
  if (!first) {
    return null
  }
  const own = area ? CONSOLE_PAGES.find((page) => page.area === area) : undefined
  return own && can(permissions, own.area, 'read') ? own.path : first.path
}

// --- groups ---------------------------------------------------------------------------

/** A group's name (docs/admin.md § Groups): 1–32 of `a`–`z`, `0`–`9`, `_` and `-`, starting
 *  with a letter or a digit. Uniqueness is the server's to say. */
export const GROUP_NAME_MAX = 32

export function isGroupName(name: string): boolean {
  return name.length <= GROUP_NAME_MAX && /^[a-z0-9][a-z0-9_-]*$/.test(name)
}

/**
 * A group's list after one tick or untick, as the matrix sends it: ticking a write ticks its
 * read, unticking a read unticks its write — write includes read — and the list is kept in
 * the API's permission order (`order`), as the server stores it.
 */
export function toggledPermissions(
  current: readonly string[],
  area: string,
  level: AdminLevel,
  on: boolean,
  order: readonly string[],
): string[] {
  const next = new Set(current)
  if (on) {
    next.add(`${area}.${level}`)
    if (level === 'write') {
      next.add(`${area}.read`)
    }
  } else {
    next.delete(`${area}.${level}`)
    if (level === 'read') {
      next.delete(`${area}.write`)
    }
  }
  return order.filter((permission) => next.has(permission))
}

/** What a change hands out or takes away: the permissions in one list and not the other. */
export function changedPermissions(before: readonly string[], after: readonly string[]): string[] {
  return [...before.filter((p) => !after.includes(p)), ...after.filter((p) => !before.includes(p))]
}

interface GroupFacts {
  builtin: boolean
  /** The effective list: every permission for the built-in group. */
  permissions: readonly string[]
}

/**
 * Whether the operator may tick or untick one cell of a group's matrix: `groups.write`, never
 * on the built-in group, never on a group that gives a permission the operator does not hold —
 * a stronger group keeps its list — and only when everything the click would change — the
 * cell, and the read or write it brings along — is held by the operator (docs/admin.md
 * § Permissions, *No escalation*).
 */
export function canToggle(
  held: readonly string[],
  group: GroupFacts,
  area: string,
  level: AdminLevel,
  order: readonly string[],
): boolean {
  if (group.builtin || !can(held, 'groups', 'write')) {
    return false
  }
  if (!group.permissions.every((permission) => held.includes(permission))) {
    return false
  }
  const on = !group.permissions.includes(`${area}.${level}`)
  const changed = changedPermissions(group.permissions, toggledPermissions(group.permissions, area, level, on, order))
  return changed.length > 0 && changed.every((permission) => held.includes(permission))
}

/** Adding and removing members: `groups.write` and every permission the group gives — so the
 *  built-in group's members are managed only by someone holding every permission. */
export function canManageMembers(held: readonly string[], group: GroupFacts): boolean {
  return can(held, 'groups', 'write') && group.permissions.every((permission) => held.includes(permission))
}

/** Deleting: never the built-in group, and only by someone holding everything it gives. */
export function canDeleteGroup(held: readonly string[], group: GroupFacts): boolean {
  return !group.builtin && canManageMembers(held, group)
}

/** Renaming needs `groups.write` alone — a name grants nothing — and never the built-in group. */
export function canRenameGroup(held: readonly string[], group: GroupFacts): boolean {
  return !group.builtin && can(held, 'groups', 'write')
}

// --- redeem codes -----------------------------------------------------------------------

/** The created codes' CSV columns (docs/admin.md § Pages, *Redeem codes*): the stored
 *  fields by name, so a plan code's row keeps its plan and no column means two things. */
export const CODES_CSV_COLUMNS = [
  'code',
  'kind',
  'credits',
  'valid_days',
  'plan_id',
  'days',
  'max_uses',
  'redeem_by',
  'label',
] as const

/**
 * The created codes as a CSV file's text: a header row, then a row per code with the batch's
 * fields — `redeem_by` as its UTC date, empty for none — fields quoted where they hold a
 * comma, a quote or a line break (RFC 4180, CRLF rows). It begins with a byte-order mark, so
 * a spreadsheet reads a label in Chinese as UTF-8. Built in the browser from the create
 * answer; nothing is stored.
 */
export function codesCsv(created: AdminCodesCreated): string {
  const field = (value: string | number | null) => {
    const text = value === null ? '' : String(value)
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
  }
  const rows = created.codes.map((item) =>
    [
      item.code,
      created.kind,
      created.credits,
      created.valid_days,
      created.plan_id,
      created.days,
      created.max_uses,
      created.redeem_by ? created.redeem_by.slice(0, 10) : null,
      created.label,
    ]
      .map(field)
      .join(','),
  )
  return `\uFEFF${[CODES_CSV_COLUMNS.join(','), ...rows].join('\r\n')}\r\n`
}

/** A code's redeem-by date as the pages print it: the UTC day, which is the one it counts
 *  to the end of — local time would print the next day east of Greenwich. */
export function formatRedeemBy(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(iso))
}

/** How many days ahead an active code's last day comes for the list to say so. */
export const CODE_CLOSING_DAYS = 7

/** Whole UTC days from today to a code's last day: 0 on that day itself, negative after. */
export function daysUntil(iso: string, now: Date): number {
  const end = new Date(iso)
  const last = Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate())
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return Math.round((last - today) / 86_400_000)
}

// --- the route query ------------------------------------------------------------------

/** The highest `?page=` the API takes (docs/admin.md § Routes). */
export const PAGE_MAX = 2 ** 31 - 1

/** A page number from the query: a whole number from 1, else 1. */
export function pageOf(value: unknown): number {
  const page = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN
  if (!Number.isSafeInteger(page) || page < 1) return 1
  // A page past the API's bound asks for the bound, which the list then replaces with its
  // last page, as for any page past the end.
  return Math.min(page, PAGE_MAX)
}

/** A row id from the query (`?group=`): a positive whole number, else null. */
export function idOf(value: unknown): number | null {
  const id = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN
  return Number.isSafeInteger(id) && id >= 1 ? id : null
}

/** The audit filter's area from the query: one the filter offers, else null (all). */
export function auditAreaOf(value: unknown): AdminArea | null {
  return AUDIT_FILTER_AREAS.find((area) => area === value) ?? null
}

// --- users and grants -----------------------------------------------------------------

/** A grant's number as typed (docs/admin.md § Users and grants): a whole number from 1 to
 *  `max`, the API's cap, else null — and the dialog's button stays off. */
export function grantNumber(text: string, max: number): number | null {
  const typed = text.trim()
  const value = /^\d+$/.test(typed) ? Number(typed) : NaN
  return Number.isSafeInteger(value) && value >= 1 && value <= max ? value : null
}

// --- refusals -------------------------------------------------------------------------

/**
 * What a failed console request means for the page (docs/admin.md § Pages, *Errors*):
 * `signedOut` — the session is gone, handled as everywhere; `reauth` — the full-panel
 * *Sign in again* state; `denied` — permissions changed, re-read them and say so where the
 * action was taken; `conflict` — a rule of the console, worded from its `reason`;
 * `notFound` — what the path or the address named does not exist, or the console is off;
 * `other` — the backend's own message.
 */
export type AdminFailure =
  | { kind: 'signedOut' }
  | { kind: 'reauth' }
  | { kind: 'denied' }
  | { kind: 'conflict'; reason: string | null }
  | { kind: 'notFound' }
  | { kind: 'other' }

export function adminFailure(error: unknown): AdminFailure {
  if (!(error instanceof ApiRequestError)) {
    return { kind: 'other' }
  }
  if (error.status === 401) {
    return { kind: 'signedOut' }
  }
  if (error.type === 'reauth_required') {
    return { kind: 'reauth' }
  }
  if (error.type === 'permission_denied') {
    return { kind: 'denied' }
  }
  if (error.type === 'admin_conflict') {
    return { kind: 'conflict', reason: typeof error.details.reason === 'string' ? error.details.reason : null }
  }
  if (error.status === 404) {
    return { kind: 'notFound' }
  }
  return { kind: 'other' }
}

/** The reasons this step's `admin_conflict` names; a page says any other in the server's
 *  words (docs/admin.md § Refusals). */
const CONFLICT_REASONS = ['name_taken', 'builtin', 'last_owner', 'email_ambiguous', 'stale'] as const

/** The catalog key for a conflict's reason, or null for one the catalog does not word. */
export function conflictKey(reason: string | null): string | null {
  return CONFLICT_REASONS.find((known) => known === reason) ? `admin.conflicts.${reason}` : null
}

// --- jobs and usage -------------------------------------------------------------------

/** A job's statuses, in the order the Jobs filter offers them (docs/admin.md § Routes). */
export const JOB_STATUSES = ['queued', 'running', 'failed', 'succeeded'] as const

export type JobStatus = (typeof JOB_STATUSES)[number]

/** The Jobs filter's status from the query (`?status=`): one of the four, else null (all). */
export function jobStatusOf(value: unknown): JobStatus | null {
  return JOB_STATUSES.find((status) => status === value) ?? null
}

/** The reasons a requeue's `admin_conflict` names (docs/admin.md § Refusals). */
const REQUEUE_REASONS = ['not_running', 'not_stuck', 'worker_unknown'] as const

/** The catalog key for a requeue refusal's reason, or null for one the catalog does not word. */
export function requeueRefusalKey(reason: string | null): string | null {
  return REQUEUE_REASONS.find((known) => known === reason) ? `admin.jobs.refusals.${reason}` : null
}

/**
 * How long since something, in its largest whole unit — seconds under a minute, minutes under
 * an hour, hours under two days, days after — in the reader's locale: *4 min*, *3 hr*. Never
 * negative: a clock a little ahead of the server's reads as 0 seconds.
 */
export function formatElapsed(milliseconds: number, locale: string): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  const [unit, n] =
    seconds < 60
      ? (['second', seconds] as const)
      : seconds < 3600
        ? (['minute', Math.floor(seconds / 60)] as const)
        : seconds < 48 * 3600
          ? (['hour', Math.floor(seconds / 3600)] as const)
          : (['day', Math.floor(seconds / 86400)] as const)
  return new Intl.NumberFormat(locale, { style: 'unit', unit, unitDisplay: 'short' }).format(n)
}

/**
 * A cost in US dollars, as `usage_log` records it: two decimals, and up to four for an amount
 * under a cent, so a day of cheap calls does not read as $0.00.
 */
export function formatUsd(value: number, locale: string): string {
  const small = value !== 0 && Math.abs(value) < 0.01
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: small ? 4 : 2,
  }).format(value)
}

/**
 * The Usage page's ranges, in UTC days up to today, and the one it opens on. The API refuses
 * a range over `ADMIN_USAGE_MAX_DAYS`, which it never lets under 90, so each one fits.
 */
export const USAGE_PRESETS = [7, 30, 90] as const
export const USAGE_DEFAULT_DAYS = 30

export interface DayRange {
  /** `YYYY-MM-DD`, UTC, both included. */
  from: string
  to: string
}

/** A moment's UTC day, `YYYY-MM-DD`. */
export function utcDay(at: Date): string {
  return at.toISOString().slice(0, 10)
}

/** A `YYYY-MM-DD` that names a real day, else null: what the query's `from` and `to` take. */
export function dayOf(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return null
  }
  const at = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(at.getTime()) && utcDay(at) === value ? value : null
}

/** The day `n` days after `day` (before it for a negative `n`). */
export function addDays(day: string, n: number): string {
  const at = new Date(`${day}T00:00:00Z`)
  at.setUTCDate(at.getUTCDate() + n)
  return utcDay(at)
}

/** The `days` UTC days that end with `today`. */
export function presetRange(days: number, today: string): DayRange {
  return { from: addDays(today, -(days - 1)), to: today }
}

/**
 * The Usage range from the query (`?from=&to=`): both as given when both are days — a range
 * the API refuses, backwards or too long, is the API's to say — else the last
 * `USAGE_DEFAULT_DAYS` days.
 */
export function usageRangeOf(query: { from?: unknown; to?: unknown }, today: string): DayRange {
  const from = dayOf(query.from)
  const to = dayOf(query.to)
  return from && to ? { from, to } : presetRange(USAGE_DEFAULT_DAYS, today)
}

/** The preset a range is, or null — a range from a link selects no segment. */
export function presetOf(range: DayRange, today: string): number | null {
  return USAGE_PRESETS.find((days) => {
    const preset = presetRange(days, today)
    return preset.from === range.from && preset.to === range.to
  }) ?? null
}

/** Every day from `from` to `to`, oldest first: the chart's days, one a point, a day without
 *  calls at $0. Empty for a backwards range. */
export function daysOf(range: DayRange): string[] {
  const days: string[] = []
  for (let day = range.from; day <= range.to; day = addDays(day, 1)) {
    days.push(day)
  }
  return days
}

/** A UTC day as the reader's locale writes it — the medium date, or month and day alone for
 *  the chart's axis — read in UTC, so no timezone moves it to the day before. */
export function formatUtcDay(day: string, locale: string, short = false): string {
  const options: Intl.DateTimeFormatOptions = short
    ? { month: 'numeric', day: 'numeric', timeZone: 'UTC' }
    : { dateStyle: 'medium', timeZone: 'UTC' }
  return new Intl.DateTimeFormat(locale, options).format(new Date(`${day}T00:00:00Z`))
}

/**
 * The chart's days from the usage rows: every day of `days`, its estimated cost summed over
 * its models — a day without a row at $0 — with the model that cost most and the rest
 * together. The estimate, as the page's *Cost* is (docs/admin.md § Overview, jobs and usage).
 */
export function costDays(
  days: readonly string[],
  rows: readonly { date: string; model: string; estimated_cost: number }[],
): CostDay[] {
  return days.map((day) => {
    const models = rows.filter((row) => row.date === day && row.estimated_cost > 0)
    const cost = models.reduce((sum, row) => sum + row.estimated_cost, 0)
    const top = models.reduce<(typeof models)[number] | null>(
      (best, row) => (!best || row.estimated_cost > best.estimated_cost ? row : best),
      null,
    )
    return {
      day,
      cost,
      top: top ? { model: top.model, cost: top.estimated_cost } : null,
      others: top ? cost - top.estimated_cost : 0,
    }
  })
}
