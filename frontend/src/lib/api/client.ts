// The one place a network request is made.
//
// Nothing else in the app calls fetch. Three invariants live here and would otherwise have to be
// remembered at every call site:
//
//  1. credentials: 'same-origin'. The session is a cookie, and fetch does not send cookies by
//     default even to its own origin.
//  2. X-CSRF-Token on every unsafe method, read from the em_csrf cookie. The backend compares it
//     in constant time against the token on the server-side session row, so a request without it
//     is a 403 and not a mysterious 401.
//  3. The error envelope is parsed into a typed ApiError before it is thrown, so a screen can
//     branch on error.code rather than on substrings of a message.

const API_BASE = '/api/v1'

/** Readable by JavaScript on purpose. It is the only cookie here that is not HttpOnly, because
 *  the synchronizer token has to be echoed in a header this code sets. */
const CSRF_COOKIE = 'em_csrf'
const CSRF_HEADER = 'X-CSRF-Token'

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

export type QueryValue = string | number | boolean | null | undefined | Array<string | number>
export type QueryParams = Record<string, QueryValue>

export interface RequestOptions {
  query?: QueryParams
  body?: unknown
  signal?: AbortSignal
  headers?: Record<string, string>
}

/** The backend error envelope, identical on every non-2xx. */
interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    correlation_id?: string
    detail?: Record<string, unknown>
  }
}

/** A request that never reached the backend. Distinguished from a backend error so a screen can
 *  say "the app is not running" instead of inventing a server-side reason. */
export const NETWORK_ERROR_CODE = 'network_error'

/** The backend returned a body this client could not read as the documented envelope. */
export const MALFORMED_RESPONSE_CODE = 'malformed_response'

export class ApiError extends Error {
  /** HTTP status, or 0 when the request never reached the backend. */
  readonly status: number
  /** The stable machine-readable code from the envelope. Screens branch on this. */
  readonly code: string
  /** Present on every backend error. The only identifier worth quoting in a bug report, because
   *  the matching log line carries the full detail that was deliberately kept out of the body. */
  readonly correlationId: string | null
  readonly detail: Record<string, unknown> | null
  /** Parsed from the Retry-After header on a 429 or a 423, in seconds. */
  readonly retryAfterSeconds: number | null

  constructor(init: {
    status: number
    code: string
    message: string
    correlationId?: string | null
    detail?: Record<string, unknown> | null
    retryAfterSeconds?: number | null
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.correlationId = init.correlationId ?? null
    this.detail = init.detail ?? null
    this.retryAfterSeconds = init.retryAfterSeconds ?? null
  }

  /** No session, or it expired. The shell sends the user to /login. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** The broker token is parked. The work is not lost, it is waiting for a login, which is why
   *  this raises a banner rather than an error screen. */
  get isNeedsReauth(): boolean {
    return this.code === 'needs_reauth'
  }

  get isRateLimited(): boolean {
    return this.status === 429
  }

  get isNetworkError(): boolean {
    return this.status === 0
  }
}

// ---------------------------------------------------------------------------
// Cross-cutting responses, published rather than acted on here
// ---------------------------------------------------------------------------

export type ApiEventName = 'unauthenticated' | 'needs_reauth'

type ApiEventListener = (error: ApiError) => void

const listeners: Record<ApiEventName, Set<ApiEventListener>> = {
  unauthenticated: new Set(),
  needs_reauth: new Set(),
}

/**
 * Subscribe to the two error codes that no single screen owns.
 *
 * The client publishes rather than navigating, because a module that imports the router cannot
 * be unit tested and cannot be reused from a worker. The shell subscribes and decides.
 *
 * Returns the unsubscribe function.
 */
export function onApiEvent(event: ApiEventName, listener: ApiEventListener): () => void {
  listeners[event].add(listener)
  return () => {
    listeners[event].delete(listener)
  }
}

function publish(event: ApiEventName, error: ApiError): void {
  for (const listener of [...listeners[event]]) {
    try {
      listener(error)
    } catch {
      // A listener that throws must not turn a handled 401 into an unhandled rejection in the
      // request path. It has already failed; the request result still has to be delivered.
    }
  }
}

// ---------------------------------------------------------------------------
// Cookies and URLs
// ---------------------------------------------------------------------------

/** Reads one cookie by name. Exported because the setup wizard has to prove the cookie landed
 *  before it can send its first unsafe request. */
export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const prefix = name + '='
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim()
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length))
    }
  }
  return null
}

export function readCsrfToken(): string | null {
  return readCookie(CSRF_COOKIE)
}

/** Serialises query parameters. Arrays repeat the key, which is what FastAPI reads back into a
 *  list. null and undefined are dropped rather than sent as the string "null". */
export function buildQuery(params: QueryParams | undefined): string {
  if (!params) {
    return ''
  }
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) {
      continue
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        search.append(key, String(item))
      }
      continue
    }
    search.append(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? '?' + encoded : ''
}

/** Absolute-on-this-origin URL for an API path. Used directly for the few places the browser
 *  navigates rather than fetches, such as an export file download. */
export function apiUrl(path: string, query?: QueryParams): string {
  const suffix = path.startsWith('/') ? path : '/' + path
  return API_BASE + suffix + buildQuery(query)
}

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

function parseRetryAfter(response: Response): number | null {
  const header = response.headers.get('Retry-After')
  if (!header) {
    return null
  }
  const seconds = Number(header)
  if (Number.isFinite(seconds)) {
    return seconds
  }
  // The header also allows an HTTP date. Convert it to a remaining span so the caller only ever
  // deals with seconds.
  const at = Date.parse(header)
  return Number.isNaN(at) ? null : Math.max(0, Math.round((at - Date.now()) / 1000))
}

async function toApiError(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope | null = null
  try {
    envelope = (await response.json()) as ErrorEnvelope
  } catch {
    envelope = null
  }
  const error = envelope?.error
  return new ApiError({
    status: response.status,
    code: error?.code ?? 'http_' + String(response.status),
    // The backend guarantees message is safe to render. When it is missing the status text is
    // the only thing left, and it is generic by construction.
    message: error?.message ?? response.statusText ?? 'Request failed',
    correlationId: error?.correlation_id ?? null,
    detail: error?.detail ?? null,
    retryAfterSeconds: parseRetryAfter(response),
  })
}

/**
 * Performs one API request and returns the parsed body.
 *
 * `path` is relative to /api/v1. The one route outside that prefix, /fyers/callback, is a
 * browser navigation and is never fetched from here.
 */
export async function apiRequest<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }

  const hasBody = options.body !== undefined
  if (hasBody) {
    headers['Content-Type'] = 'application/json'
  }

  if (!SAFE_METHODS.has(method)) {
    const token = readCsrfToken()
    if (token !== null) {
      headers[CSRF_HEADER] = token
    }
    // A missing cookie is not turned into a client-side error. The server is the authority on
    // whether the session is valid, and short-circuiting here would report csrf_invalid for
    // what is really an expired session.
  }

  let response: Response
  try {
    response = await fetch(apiUrl(path, options.query), {
      method,
      // The session and CSRF cookies are host-only on this origin. Without this they are not
      // sent at all and every call is a 401.
      credentials: 'same-origin',
      headers,
      body: hasBody ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      // An aborted request is a cancelled query, not a failure. Rethrow it unchanged so
      // TanStack Query recognises it.
      throw cause
    }
    throw new ApiError({
      status: 0,
      code: NETWORK_ERROR_CODE,
      message: 'Could not reach the ExpiryManager backend on 127.0.0.1:8000.',
    })
  }

  if (!response.ok) {
    const error = await toApiError(response)
    if (error.isUnauthenticated) {
      publish('unauthenticated', error)
    } else if (error.isNeedsReauth) {
      publish('needs_reauth', error)
    }
    throw error
  }

  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return undefined as T
  }

  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError({
      status: response.status,
      code: MALFORMED_RESPONSE_CODE,
      message: 'The backend returned a response this client could not read.',
    })
  }
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => apiRequest<T>('GET', path, options),
  post: <T>(path: string, options?: RequestOptions) => apiRequest<T>('POST', path, options),
  patch: <T>(path: string, options?: RequestOptions) => apiRequest<T>('PATCH', path, options),
  put: <T>(path: string, options?: RequestOptions) => apiRequest<T>('PUT', path, options),
  delete: <T>(path: string, options?: RequestOptions) => apiRequest<T>('DELETE', path, options),
}

/** True for the errors that retrying cannot help. TanStack Query `retry` predicates use this so
 *  a 401 or a validation failure is not attempted three times. */
export function isTerminalApiError(error: unknown): boolean {
  if (!(error instanceof ApiError)) {
    return false
  }
  if (error.status === 429) {
    return false
  }
  return error.status >= 400 && error.status < 500
}
