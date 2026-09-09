// @vitest-environment jsdom

// The client is the only place in the app that talks to the network, so the three things it
// guarantees are worth pinning: the CSRF header on unsafe methods and nowhere else, cookies on
// every request, and a typed error carrying the backend's own code.
//
// Every token in this file is synthetic. Nothing here is or resembles a real credential.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  api,
  apiRequest,
  apiUrl,
  buildQuery,
  isTerminalApiError,
  onApiEvent,
  readCsrfToken,
} from '@/lib/api/client'

const SYNTHETIC_CSRF = 'test-csrf-token-not-a-real-secret'

function setCookie(value: string): void {
  document.cookie = 'em_csrf=' + value + '; path=/'
}

function clearCookies(): void {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim()
    if (name) {
      document.cookie = name + '=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    }
  }
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

function errorResponse(
  status: number,
  code: string,
  extra: { correlation_id?: string; detail?: Record<string, unknown> } = {},
  headers: Record<string, string> = {},
): Response {
  return new Response(
    JSON.stringify({
      error: { code, message: 'safe human text', ...extra },
    }),
    { status, headers: { 'Content-Type': 'application/json', ...headers } },
  )
}

/** A fresh Response per call. A Response body can only be read once, so a mock that resolves
 *  to one shared object fails the second time it is used. */
function mockFetch(factory: () => Response): void {
  vi.mocked(globalThis.fetch).mockImplementation(() => Promise.resolve(factory()))
}

function lastRequest(): { url: string; init: RequestInit } {
  const mock = vi.mocked(globalThis.fetch)
  const call = mock.mock.calls.at(-1)
  if (!call) {
    throw new Error('fetch was not called')
  }
  return { url: String(call[0]), init: (call[1] ?? {}) as RequestInit }
}

function headerOf(name: string): string | undefined {
  const headers = lastRequest().init.headers as Record<string, string> | undefined
  return headers?.[name]
}

beforeEach(() => {
  clearCookies()
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('csrf header', () => {
  it('sends X-CSRF-Token from the em_csrf cookie on every unsafe method', async () => {
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ ok: true }))

    for (const call of [
      () => api.post('/broker/fyers/connect'),
      () => api.patch('/underlyings/1', { body: { is_active: false } }),
      () => api.put('/system/settings', { body: {} }),
      () => api.delete('/exports/abc'),
    ]) {
      await call()
      expect(headerOf('X-CSRF-Token')).toBe(SYNTHETIC_CSRF)
    }
  })

  it('does not send X-CSRF-Token on a safe method', async () => {
    setCookie(SYNTHETIC_CSRF)
    mockFetch(() => jsonResponse({ provisioned: true }))

    await api.get('/bootstrap')

    expect(headerOf('X-CSRF-Token')).toBeUndefined()
  })

  it('reads the cookie fresh on every call, because login rotates it', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    setCookie('first-synthetic-token')
    await api.post('/auth/logout')
    expect(headerOf('X-CSRF-Token')).toBe('first-synthetic-token')

    setCookie('second-synthetic-token')
    await api.post('/auth/logout')
    expect(headerOf('X-CSRF-Token')).toBe('second-synthetic-token')
  })

  it('url decodes the cookie value', async () => {
    setCookie(encodeURIComponent('token with spaces'))
    expect(readCsrfToken()).toBe('token with spaces')
  })

  it('still sends the request when the cookie is absent, and lets the server decide', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')

    // Short circuiting here would report a CSRF failure for what is really an expired session.
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1)
    expect(headerOf('X-CSRF-Token')).toBeUndefined()
  })

  it('sends cookies on every request, including safe ones', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.get('/bootstrap')
    expect(lastRequest().init.credentials).toBe('same-origin')

    await api.post('/auth/logout')
    expect(lastRequest().init.credentials).toBe('same-origin')
  })

  it('sets a json content type only when there is a body', async () => {
    mockFetch(() => jsonResponse({ ok: true }))

    await api.post('/auth/logout')
    expect(headerOf('Content-Type')).toBeUndefined()

    await api.post('/auth/login', { body: { username: 'tester' } })
    expect(headerOf('Content-Type')).toBe('application/json')
  })
})

describe('urls', () => {
  it('prefixes every path with the api version', () => {
    expect(apiUrl('/bootstrap')).toBe('/api/v1/bootstrap')
    expect(apiUrl('bootstrap')).toBe('/api/v1/bootstrap')
  })

  it('repeats a key for an array and drops null and undefined', () => {
    expect(buildQuery({ res: ['1', '5'], limit: 100, cursor: null, kind: undefined })).toBe(
      '?res=1&res=5&limit=100',
    )
  })

  it('returns an empty string rather than a bare question mark', () => {
    expect(buildQuery(undefined)).toBe('')
    expect(buildQuery({ cursor: null })).toBe('')
  })
})

describe('errors', () => {
  it('parses the envelope into a typed error carrying the code and correlation id', async () => {
    mockFetch(() =>
      errorResponse(409, 'plan_changed', {
        correlation_id: '8f2c0000',
        detail: { requests_estimated: 4820 },
      })
    )

    const error = await api.post('/downloads').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.code).toBe('plan_changed')
    expect(apiError.status).toBe(409)
    expect(apiError.message).toBe('safe human text')
    expect(apiError.correlationId).toBe('8f2c0000')
    expect(apiError.detail).toEqual({ requests_estimated: 4820 })
  })

  it('reads Retry-After as seconds on a 429', async () => {
    mockFetch(() => errorResponse(429, 'rate_limited', {}, { 'Retry-After': '42' }))

    const error = (await api.get('/bars').catch((thrown: unknown) => thrown)) as ApiError

    expect(error.retryAfterSeconds).toBe(42)
    expect(error.isRateLimited).toBe(true)
  })

  it('publishes a 401 so the shell can route to login', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('unauthenticated', (error) => seen.push(error))
    mockFetch(() => errorResponse(401, 'not_authenticated'))

    await api.get('/auth/me').catch(() => undefined)
    unsubscribe()

    expect(seen).toHaveLength(1)
    expect(seen[0].isUnauthenticated).toBe(true)
  })

  it('publishes a 409 needs_reauth so the banner can be raised', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('needs_reauth', (error) => seen.push(error))
    mockFetch(() => errorResponse(409, 'needs_reauth'))

    await api.post('/downloads/plan').catch(() => undefined)
    unsubscribe()

    expect(seen).toHaveLength(1)
    expect(seen[0].isNeedsReauth).toBe(true)
  })

  it('stops publishing once the listener unsubscribes', async () => {
    const seen: ApiError[] = []
    const unsubscribe = onApiEvent('unauthenticated', (error) => seen.push(error))
    unsubscribe()
    mockFetch(() => errorResponse(401, 'not_authenticated'))

    await api.get('/auth/me').catch(() => undefined)

    expect(seen).toHaveLength(0)
  })

  it('reports an unreachable backend as a network error rather than a server error', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(new TypeError('Failed to fetch'))

    const error = (await api.get('/bootstrap').catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(0)
    expect(error.code).toBe('network_error')
    expect(error.isNetworkError).toBe(true)
  })

  it('rethrows an abort unchanged, because a cancelled query is not a failure', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(
      new DOMException('The operation was aborted', 'AbortError'),
    )

    const error = await api.get('/bars').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(DOMException)
    expect(error).not.toBeInstanceOf(ApiError)
  })

  it('falls back to a status code when the body is not the documented envelope', async () => {
    mockFetch(() => new Response('gateway exploded', { status: 502 }))

    const error = (await api.get('/bootstrap').catch((thrown: unknown) => thrown)) as ApiError

    expect(error.code).toBe('http_502')
    expect(error.correlationId).toBeNull()
  })

  it('treats a client error as terminal and a rate limit or server error as retryable', () => {
    expect(isTerminalApiError(new ApiError({ status: 401, code: 'x', message: 'm' }))).toBe(true)
    expect(isTerminalApiError(new ApiError({ status: 422, code: 'x', message: 'm' }))).toBe(true)
    expect(isTerminalApiError(new ApiError({ status: 429, code: 'x', message: 'm' }))).toBe(false)
    expect(isTerminalApiError(new ApiError({ status: 503, code: 'x', message: 'm' }))).toBe(false)
    expect(isTerminalApiError(new Error('not an api error'))).toBe(false)
  })
})

describe('responses', () => {
  it('returns undefined for a 204 rather than failing to parse an empty body', async () => {
    mockFetch(() => new Response(null, { status: 204 }))

    await expect(apiRequest<void>('POST', '/auth/logout')).resolves.toBeUndefined()
  })

  it('parses a json body', async () => {
    mockFetch(() => jsonResponse({ provisioned: true }))

    await expect(api.get<{ provisioned: boolean }>('/bootstrap')).resolves.toEqual({
      provisioned: true,
    })
  })
})
