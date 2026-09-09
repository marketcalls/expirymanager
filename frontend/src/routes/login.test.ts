// A failed sign in has four distinct causes and only one of them means "you typed it wrong".
//
// The other three (the account is locked, the rate limiter refused the attempt, the backend is
// not running) all present as a failure with a message, and a screen that renders them the same
// way leaves the user typing into a form that cannot succeed. This pins the mapping, including
// which of them carry a wait to count down.
//
// Nothing here is a real credential.

import { describe, expect, it } from 'vitest'

import { ApiError } from '@/lib/api/client'
import { describeLoginFailure } from '@/routes/login'

const NOW = 1_780_000_000_000

describe('describeLoginFailure', () => {
  it('shows the backend sentence unchanged for a rejected passcode', () => {
    // The backend answers one identical body whether the username or the passcode was wrong.
    // Rewording it here would be guessing at which half failed.
    const message = 'That username and passcode combination was not accepted.'
    const result = describeLoginFailure(
      new ApiError({ status: 401, code: 'invalid_credentials', message }),
      NOW,
    )
    expect(result.kind).toBe('rejected')
    expect(result.message).toBe(message)
    expect(result.deadline).toBeNull()
  })

  it('turns a 423 into a lockout with a deadline', () => {
    const result = describeLoginFailure(
      new ApiError({
        status: 423,
        code: 'account_locked',
        message: 'Too many failed attempts.',
        retryAfterSeconds: 900,
      }),
      NOW,
    )
    expect(result.kind).toBe('locked')
    expect(result.deadline).toBe(NOW + 900_000)
    expect(result.message).toContain('unlocks by')
  })

  it('turns a 429 into a wait rather than a wrong passcode', () => {
    const result = describeLoginFailure(
      new ApiError({
        status: 429,
        code: 'rate_limited',
        message: 'Too many requests.',
        retryAfterSeconds: 60,
      }),
      NOW,
    )
    expect(result.kind).toBe('rate_limited')
    expect(result.deadline).toBe(NOW + 60_000)
  })

  it('still names the lockout when the backend sent no Retry-After', () => {
    // The countdown is the nice half. The sentence saying the account is locked is the half that
    // has to survive a missing header.
    const result = describeLoginFailure(
      new ApiError({ status: 423, code: 'account_locked', message: 'Locked.' }),
      NOW,
    )
    expect(result.kind).toBe('locked')
    expect(result.deadline).toBeNull()
  })

  it('says the backend is not running rather than inventing a server side reason', () => {
    const result = describeLoginFailure(
      new ApiError({ status: 0, code: 'network_error', message: 'Could not reach the backend.' }),
      NOW,
    )
    expect(result.kind).toBe('unreachable')
    expect(result.message).toContain('127.0.0.1:8000')
    expect(result.deadline).toBeNull()
  })

  it('quotes the correlation id on an unexpected failure, because the log line carries the rest', () => {
    const result = describeLoginFailure(
      new ApiError({
        status: 500,
        code: 'internal_error',
        message: 'Something failed.',
        correlationId: 'abc123',
      }),
      NOW,
    )
    expect(result.kind).toBe('other')
    expect(result.message).toContain('abc123')
  })

  it('does not count a past Retry-After as a wait', () => {
    const result = describeLoginFailure(
      new ApiError({
        status: 429,
        code: 'rate_limited',
        message: 'Too many requests.',
        retryAfterSeconds: 0,
      }),
      NOW,
    )
    expect(result.deadline).toBeNull()
  })

  it('survives a thrown value that is not an ApiError', () => {
    const result = describeLoginFailure(new TypeError('boom'), NOW)
    expect(result.kind).toBe('other')
    expect(result.deadline).toBeNull()
  })
})
