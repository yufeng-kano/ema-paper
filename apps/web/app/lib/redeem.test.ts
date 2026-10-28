import { describe, expect, it } from 'vitest'
import { ApiRequestError } from './api'
import { presentCode, redeemErrorKey } from './redeem'

describe('redeemErrorKey', () => {
  it('words every refusal the API names', () => {
    expect(redeemErrorKey(new ApiRequestError(404, 'code_invalid', 'x'))).toBe('redeem.errors.invalid')
    expect(redeemErrorKey(new ApiRequestError(409, 'code_expired', 'x'))).toBe('redeem.errors.expired')
    expect(redeemErrorKey(new ApiRequestError(409, 'code_exhausted', 'x'))).toBe('redeem.errors.exhausted')
    expect(redeemErrorKey(new ApiRequestError(409, 'code_redeemed', 'x'))).toBe('redeem.errors.redeemed')
    expect(redeemErrorKey(new ApiRequestError(409, 'code_plan_conflict', 'x'))).toBe(
      'redeem.errors.planConflict',
    )
    expect(redeemErrorKey(new ApiRequestError(429, 'rate_limit', 'x'))).toBe('redeem.errors.tooMany')
  })

  it('leaves every other failure to the usual resolution', () => {
    expect(redeemErrorKey(new ApiRequestError(503, 'upstream', 'x'))).toBeNull()
    expect(redeemErrorKey(new ApiRequestError(0, 'internal', 'offline'))).toBeNull()
    expect(redeemErrorKey(new Error('x'))).toBeNull()
  })
})

describe('presentCode', () => {
  it('shows the code as printed while the server reads it', () => {
    expect(presentCode('ema-7k3q wx9m-4htb')).toBe('EMA-7K3Q WX9M-4HTB')
  })
})
