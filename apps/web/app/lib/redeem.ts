import { ApiRequestError } from './api'

/**
 * What the redeem dialog says when a code is refused (docs/billing.md § Refusals, § Redeem
 * codes): the catalog key for each refusal the API names, so the line under the input is
 * worded in the reader's language rather than the backend's; anything else goes through
 * the usual error resolution.
 */
const REFUSALS: Record<string, string> = {
  code_invalid: 'redeem.errors.invalid',
  code_expired: 'redeem.errors.expired',
  code_exhausted: 'redeem.errors.exhausted',
  code_redeemed: 'redeem.errors.redeemed',
  code_plan_conflict: 'redeem.errors.planConflict',
}

export function redeemErrorKey(error: unknown): string | null {
  if (!(error instanceof ApiRequestError)) {
    return null
  }
  if (error.status === 429) {
    return 'redeem.errors.tooMany'
  }
  return REFUSALS[error.type] ?? null
}

/** What the input shows as the reader types: uppercase, so the code reads as printed; the
 *  server does the real reading (hyphens and spaces are its to drop). */
export function presentCode(typed: string): string {
  return typed.toUpperCase()
}
