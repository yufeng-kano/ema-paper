import type { ApiErrorType } from './api'

/**
 * The payment refusals a reader meets — on the Usage section, in the top-up dialog, on the
 * pricing page and when deleting the account — and the catalog key that words each in the
 * reader's language, the backend's sentence being English (docs/web.md, `/settings/usage`).
 */
export const PAYMENT_REFUSALS: Partial<Record<ApiErrorType, string>> = {
  pay_unavailable: 'errors.payUnavailable',
  checkout_unavailable: 'errors.checkoutUnavailable',
  portal_unavailable: 'errors.portalUnavailable',
  plan_required: 'errors.planRequired',
}
