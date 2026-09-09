import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useForm } from 'react-hook-form'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type {
  BrokerConnectResponse,
  BrokerPlan,
  BrokerStatus,
  BrokerTestResponse,
} from '@/lib/api/types'
import { formatDateTime, formatLatency, formatRelative } from '@/lib/format'

// Everything about the Fyers connection lives here, and both screens that need it read it from
// this file: the Settings Broker tab, and steps 2 and 3 of the first run wizard. They are the
// same two jobs (store the registration, complete the OAuth login) shown in a different frame,
// and a second copy of either would be the copy that goes stale.
//
// Three things this file is careful about.
//
//  1. The app secret is write only. It is typed once, sent once, and never read back. There is no
//     mask, because a mask confirms a guess character by character and tempts the form into
//     round tripping it on the next save, at which point the mask becomes the stored secret.
//  2. The redirect URL is a constant, not a field. Fyers matches it against the registration
//     character for character, so a typo does not fail at save time, it fails much later with an
//     error that reads like a wrong app id.
//  3. The pasted callback URL is treated as a credential. It carries an auth code, so it is
//     never logged, never echoed back into the page, and the inspection below returns booleans
//     about it rather than any part of its contents.

/** The one registered value. Must stay character identical to the backend's DEFAULT_REDIRECT_URI
 *  and to what is registered on the Fyers dashboard. */
export const REDIRECT_URI = 'http://127.0.0.1:8000/fyers/callback'

/** Where the certificate warning is expected, said the same way everywhere it is said. */
export const CERTIFICATE_NOTE =
  'The app serves https on 127.0.0.1 with a certificate it generated itself, because that is ' +
  'what the Fyers redirect URL requires. The browser calls that certificate untrusted and shows ' +
  'a warning the first time. That is expected. Choose the advanced option and continue to ' +
  '127.0.0.1 once, and the warning does not come back.'

// ---------------------------------------------------------------------------
// The pasted redirect URL
// ---------------------------------------------------------------------------

export type RedirectProblem =
  | 'empty'
  | 'not_a_callback'
  | 'authorize_url'
  | 'broker_error'
  | 'no_auth_code'
  | 'no_state'

export interface RedirectInspection {
  /** True when this is worth sending to the backend, which does the real verification. */
  ok: boolean
  problem: RedirectProblem | null
  /** Booleans, never the values. The auth code and the state are credentials. */
  hasAuthCode: boolean
  hasState: boolean
  /** The status and code Fyers put in the query when it reported a failure. */
  brokerStatus: string | null
  brokerCode: string | null
  /** Fyers' own message. Safe to render: it is written by the broker about the login attempt. */
  brokerMessage: string | null
  /** What to send. Equal to the trimmed input, except that a bare query string is completed
   *  into the callback URL it obviously came from. */
  url: string
}

const QUERY_KEY_PATTERN = /(^|&)(auth_code|state|s|code|message)=/

/** Pulls the query string out of whatever was pasted.
 *
 *  People paste three things here: the whole URL, the URL without the scheme, and just the query
 *  string after the question mark. Only the first is what was asked for, and rejecting the other
 *  two for a punctuation difference is a bad way to end a login. */
function queryOf(input: string): string | null {
  const mark = input.indexOf('?')
  if (mark >= 0) {
    const tail = input.slice(mark + 1)
    const hash = tail.indexOf('#')
    return hash >= 0 ? tail.slice(0, hash) : tail
  }
  return QUERY_KEY_PATTERN.test(input) ? input : null
}

function firstParam(params: URLSearchParams, name: string): string | null {
  const value = params.get(name)
  return value === null || value === '' ? null : value
}

/**
 * Reads a pasted redirect URL well enough to say what is wrong with it, without sending it.
 *
 * The backend runs the real check: the state has to match a row it minted, bound to this
 * session, unused and unexpired. This exists so the three mistakes that are obvious from the
 * text alone get a sentence that names the mistake, rather than one round trip that comes back
 * as the same deliberately generic state failure the backend gives a forged URL.
 */
export function inspectRedirectedUrl(raw: string): RedirectInspection {
  const input = raw.trim()
  const base: RedirectInspection = {
    ok: false,
    problem: 'empty',
    hasAuthCode: false,
    hasState: false,
    brokerStatus: null,
    brokerCode: null,
    brokerMessage: null,
    url: input,
  }

  if (input === '') {
    return base
  }

  const query = queryOf(input)
  if (query === null || query === '') {
    return { ...base, problem: 'not_a_callback' }
  }

  const params = new URLSearchParams(query)
  const authCode = firstParam(params, 'auth_code')
  const state = firstParam(params, 'state')
  const status = firstParam(params, 's')
  const code = firstParam(params, 'code')
  const message = firstParam(params, 'message')

  // A bare query string is completed rather than refused. The scheme and host are fixed, so
  // there is nothing to guess.
  const url = input.includes('?') ? input : REDIRECT_URI + '?' + query

  const result: RedirectInspection = {
    ...base,
    hasAuthCode: authCode !== null,
    hasState: state !== null,
    brokerStatus: status,
    brokerCode: code,
    brokerMessage: message,
    url,
  }

  // The authorize URL is the page the user was sent to, not the page they landed on. It carries
  // a state and a client_id and no auth code, so without this check it reads as "no auth code"
  // and the user is told to start again when they simply copied the wrong tab.
  if (params.has('client_id') || params.has('response_type') || input.includes('generate-authcode')) {
    return { ...result, problem: 'authorize_url' }
  }

  if ((status !== null && status !== 'ok') || (authCode === null && (code !== null || message !== null))) {
    return { ...result, problem: 'broker_error' }
  }

  if (authCode === null) {
    return { ...result, problem: 'no_auth_code' }
  }

  if (state === null) {
    return { ...result, problem: 'no_state' }
  }

  return { ...result, ok: true, problem: null }
}

/** The sentence shown for each problem. Every one of them ends with what to do next. */
export function redirectProblemMessage(inspection: RedirectInspection): string | null {
  switch (inspection.problem) {
    case null:
      return null
    case 'empty':
      return 'Paste the whole URL from the address bar of the tab Fyers sent you to.'
    case 'not_a_callback':
      return (
        'That does not look like the URL Fyers returned. It begins with ' +
        REDIRECT_URI +
        '? and has a long auth_code in it. Copy the whole address bar, not the page text.'
      )
    case 'authorize_url':
      return (
        'That is the Fyers login page you were sent to, not the page you landed on afterwards. ' +
        'Finish signing in to Fyers, then copy the address bar of the page it returns you to.'
      )
    case 'broker_error':
      return (
        'Fyers reported that the login did not complete' +
        (inspection.brokerMessage ? ': ' + inspection.brokerMessage : '.') +
        ' Start the connection again from this screen.'
      )
    case 'no_auth_code':
      return (
        'That URL carries no auth_code, so there is nothing to exchange. It is usually the ' +
        'wrong tab. Start the connection again from this screen if you cannot find the right one.'
      )
    case 'no_state':
      return (
        'That URL carries no state parameter, so it cannot be matched to the login this app ' +
        'started. Start the connection again from this screen.'
      )
  }
}

// ---------------------------------------------------------------------------
// Small shared pieces
// ---------------------------------------------------------------------------

/** Copy to clipboard with an honest failure. The clipboard API needs a secure context, which
 *  this origin is, but it is still refused when the document is not focused. */
function useCopyToClipboard() {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) {
        clearTimeout(timer.current)
      }
    },
    [],
  )

  const copy = useCallback((value: string) => {
    const settle = (next: 'copied' | 'failed') => {
      setState(next)
      if (timer.current !== null) {
        clearTimeout(timer.current)
      }
      timer.current = setTimeout(() => setState('idle'), 2500)
    }
    try {
      void navigator.clipboard.writeText(value).then(
        () => settle('copied'),
        () => settle('failed'),
      )
    } catch {
      settle('failed')
    }
  }, [])

  return { state, copy }
}

export interface CopyFieldProps {
  value: string
  label: string
  className?: string
}

/** A read-only value with a copy button. Read-only because the value is fixed, and rendered in
 *  an input rather than as text so it can be selected and copied by hand when the clipboard
 *  refuses. */
export function CopyField({ value, label, className }: CopyFieldProps) {
  const { state, copy } = useCopyToClipboard()
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <div className="flex items-center gap-2">
        <Input
          readOnly
          aria-label={label}
          value={value}
          spellCheck={false}
          onFocus={(event) => event.currentTarget.select()}
          className="font-mono text-xs"
        />
        <Button type="button" size="sm" variant="outline" onClick={() => copy(value)}>
          {state === 'copied' ? 'Copied' : 'Copy'}
        </Button>
      </div>
      {state === 'failed' ? (
        <p className="text-xs text-muted-foreground">
          The browser refused the clipboard. Select the text above and copy it by hand.
        </p>
      ) : null}
    </div>
  )
}

/** One label and value line. Used for every read-only fact in the settings panels. */
export function StatusRow({
  label,
  children,
  className,
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 py-1.5', className)}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right text-sm break-words">{children}</span>
    </div>
  )
}

/** The one place an ApiError becomes a sentence a user can act on. A bare code with no next step
 *  is the failure mode this whole screen exists to avoid. */
export function apiErrorMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) {
    return error ? 'Something went wrong. Try again.' : null
  }
  if (error.isNetworkError) {
    return (
      'The ExpiryManager backend did not answer. Check that it is still running, then try again.'
    )
  }
  if (error.status === 401) {
    return 'The session expired. Sign in again, then repeat this.'
  }
  if (error.isRateLimited) {
    const wait = error.retryAfterSeconds
    return (
      'Too many attempts in a short window. Wait ' +
      (wait && wait > 0 ? String(wait) + ' seconds' : 'a minute') +
      ' and try again.'
    )
  }
  const reference = error.correlationId ? ' Reference ' + error.correlationId + '.' : ''
  return error.message + reference
}

/** True when the backend does not serve this route at all.
 *
 *  The System routes land in a later work item than the ones this login path needs, and a panel
 *  that renders a red error for a route nobody has written yet teaches the user to ignore red
 *  errors. A missing route is reported as missing. */
export function isRouteMissing(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

function tokenTone(status: BrokerStatus | undefined): 'good' | 'bad' | 'idle' {
  if (!status) {
    return 'idle'
  }
  if (status.connected && status.token_state === 'active') {
    return 'good'
  }
  return status.app_secret_configured ? 'bad' : 'idle'
}

export function ConnectionBadge({ status }: { status: BrokerStatus | undefined }) {
  const tone = tokenTone(status)
  const label = !status
    ? 'Unknown'
    : status.connected && status.token_state === 'active'
      ? 'Connected'
      : status.token_state === 'expired'
        ? 'Token expired'
        : status.token_state === 'revoked'
          ? 'Token revoked'
          : status.app_secret_configured
            ? 'Not connected'
            : 'No credentials'
  return (
    <Badge variant={tone === 'good' ? 'secondary' : tone === 'bad' ? 'destructive' : 'outline'}>
      {label}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// The credentials form
// ---------------------------------------------------------------------------

interface CredentialsFormValues {
  label: string
  app_id: string
  app_secret: string
  plan: BrokerPlan
}

export interface BrokerCredentialsFormProps {
  status: BrokerStatus | undefined
  /** Called after the save lands, with the fresh status the write returned. */
  onSaved?: (status: BrokerStatus) => void
  submitLabel?: string
}

/**
 * Stores the Fyers app registration.
 *
 * Saving revokes any existing token on the backend, because a token issued to one app id cannot
 * be used with another. That is said out loud on the form when a token exists, so replacing a
 * credential to fix a typo does not silently end a working session.
 */
export function BrokerCredentialsForm({
  status,
  onSaved,
  submitLabel = 'Save credentials',
}: BrokerCredentialsFormProps) {
  const client = useQueryClient()
  const form = useForm<CredentialsFormValues>({
    defaultValues: {
      label: status?.label ?? 'Fyers',
      app_id: status?.app_id ?? '',
      app_secret: '',
      plan: status?.plan ?? 'standard',
    },
  })

  const save = useMutation({
    mutationFn: (values: CredentialsFormValues) =>
      api.post<BrokerStatus>('/broker/fyers/credentials', {
        body: {
          label: values.label.trim() || 'Fyers',
          app_id: values.app_id.trim(),
          app_secret: values.app_secret,
          // Always the constant. The field is not editable, so a stored value that drifted is
          // corrected by the next save rather than carried forward.
          redirect_uri: REDIRECT_URI,
          plan: values.plan,
        },
      }),
    onSuccess: (fresh) => {
      // The plaintext lives in this component for exactly as long as the request takes.
      form.reset({
        label: fresh.label ?? 'Fyers',
        app_id: fresh.app_id ?? '',
        app_secret: '',
        plan: fresh.plan,
      })
      client.setQueryData(queryKeys.broker.fyers(), fresh)
      void client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
      toast.success('Fyers credentials saved')
      onSaved?.(fresh)
    },
  })

  const plan = form.watch('plan')
  const hadToken = Boolean(status?.connected)
  const errorMessage = apiErrorMessage(save.error)

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={form.handleSubmit((values) => save.mutate(values))}
      // The secret is never restored by the browser's own form memory.
      autoComplete="off"
    >
      <FieldGroup className="gap-4">
        <Field>
          <FieldLabel htmlFor="broker-app-id">Fyers app id</FieldLabel>
          <Input
            id="broker-app-id"
            spellCheck={false}
            autoComplete="off"
            placeholder="ABCD1234-100"
            aria-invalid={Boolean(form.formState.errors.app_id)}
            {...form.register('app_id', {
              required: 'The app id is on the Fyers dashboard next to your app.',
              validate: (value) =>
                value.includes(':')
                  ? 'That looks like the app id and the secret joined by a colon. Paste only the part before the colon here.'
                  : true,
            })}
          />
          <FieldDescription>
            Copied from your app on the Fyers dashboard. It usually ends in a dash and three
            digits.
          </FieldDescription>
          <FieldError errors={[form.formState.errors.app_id]} />
        </Field>

        <Field>
          <FieldLabel htmlFor="broker-app-secret">Fyers app secret</FieldLabel>
          <Input
            id="broker-app-secret"
            type="password"
            spellCheck={false}
            autoComplete="new-password"
            aria-invalid={Boolean(form.formState.errors.app_secret)}
            {...form.register('app_secret', {
              required: 'The app secret is required to exchange the login for a token.',
            })}
          />
          <FieldDescription>
            Encrypted with AES-256-GCM before it reaches the database, under a key held in a 0600
            file outside it. It is never displayed again, not even masked, so keep your own copy
            on the Fyers dashboard.
            {hadToken
              ? ' Saving a new secret revokes the token you already have, and you will need to connect again.'
              : ''}
          </FieldDescription>
          <FieldError errors={[form.formState.errors.app_secret]} />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field>
            <FieldLabel htmlFor="broker-label">Label</FieldLabel>
            <Input id="broker-label" autoComplete="off" {...form.register('label')} />
            <FieldDescription>Your own name for this registration.</FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="broker-plan">Fyers plan</FieldLabel>
            <Select
              value={plan}
              onValueChange={(value) => form.setValue('plan', value as BrokerPlan)}
            >
              <SelectTrigger id="broker-plan" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="standard">Standard</SelectItem>
                <SelectItem value="prime">Prime</SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription>
              Sets the outbound request budget. Standard is 10 per second, 200 per minute and
              100,000 per day.
            </FieldDescription>
          </Field>
        </div>

        <Field>
          <FieldLabel htmlFor="broker-redirect">Redirect URL, fixed</FieldLabel>
          <CopyField value={REDIRECT_URI} label="Redirect URL" />
          <FieldDescription>
            Register exactly this string on the Fyers dashboard, including the scheme, the port
            and the path. Fyers matches it character for character, and a mismatch does not fail
            here, it fails during the login with a message that reads like a wrong app id.
          </FieldDescription>
        </Field>

        {status && status.redirect_uri !== REDIRECT_URI && status.app_secret_configured ? (
          <p className="text-sm text-destructive">
            The stored redirect URL is {status.redirect_uri}, which is not the value this app
            serves. Saving this form corrects it.
          </p>
        ) : null}
      </FieldGroup>

      {errorMessage ? (
        <p role="alert" className="text-sm text-destructive">
          {errorMessage}
        </p>
      ) : null}

      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={save.isPending}>
          {save.isPending ? 'Saving' : submitLabel}
        </Button>
        {status?.app_secret_configured ? (
          <span className="text-xs text-muted-foreground">
            A secret is already stored. Saving replaces it.
          </span>
        ) : null}
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// The OAuth login
// ---------------------------------------------------------------------------

export interface FyersConnectPanelProps {
  status: BrokerStatus | undefined
  className?: string
}

/**
 * Sends the user to Fyers and handles the return.
 *
 * The return normally lands on /settings by itself, in the tab that was opened here. When it
 * does not, which happens when the certificate warning is declined in that tab, the paste box
 * below runs the identical verification: same single use state, same session binding.
 */
export function FyersConnectPanel({ status, className }: FyersConnectPanelProps) {
  const client = useQueryClient()
  const [loginStarted, setLoginStarted] = useState(false)
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null)
  const [pasted, setPasted] = useState('')
  const [showPaste, setShowPaste] = useState(false)
  // A live window handle. Written to, never rendered, so a ref rather than state.
  const pendingTabRef = useRef<Window | null>(null)

  const connected = Boolean(status?.connected && status.token_state === 'active')
  const hasCredentials = Boolean(status?.app_secret_configured)
  // Derived rather than stored. A token that arrives ends the wait by definition, and an effect
  // that turned a flag off when the token appeared would be a second source of truth for the
  // same fact.
  const awaitingReturn = loginStarted && !connected

  // A second observer on the broker status, polling only while a login is in flight. The
  // callback lands in the other tab, so this one has no other way to find out.
  useQuery({
    queryKey: queryKeys.broker.fyers(),
    queryFn: () => api.get<BrokerStatus>('/broker/fyers'),
    enabled: awaitingReturn,
    refetchInterval: awaitingReturn ? 3000 : false,
    refetchOnWindowFocus: awaitingReturn,
  })

  const connect = useMutation({
    mutationFn: () => api.post<BrokerConnectResponse>('/broker/fyers/connect'),
    onSuccess: (data) => {
      setAuthorizeUrl(data.authorize_url)
      setLoginStarted(true)
      const tab = pendingTabRef.current
      if (tab && !tab.closed) {
        tab.location.href = data.authorize_url
      }
      pendingTabRef.current = null
    },
    onError: () => {
      pendingTabRef.current?.close()
      pendingTabRef.current = null
    },
  })

  const start = useCallback(() => {
    setAuthorizeUrl(null)
    // Opened synchronously inside the click. A window.open after an await has lost the user
    // gesture and is blocked by every browser.
    pendingTabRef.current = window.open('', '_blank', 'noopener,noreferrer')
    connect.mutate()
  }, [connect])

  const inspection = inspectRedirectedUrl(pasted)
  const inspectionMessage = pasted.trim() === '' ? null : redirectProblemMessage(inspection)

  const finish = useMutation({
    mutationFn: (url: string) =>
      api.post<BrokerStatus>('/broker/fyers/callback/manual', { body: { redirected_url: url } }),
    onSuccess: (fresh) => {
      setPasted('')
      setShowPaste(false)
      setLoginStarted(false)
      setAuthorizeUrl(null)
      client.setQueryData(queryKeys.broker.fyers(), fresh)
      void client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
      toast.success('Fyers login completed')
    },
  })

  const test = useMutation({
    mutationFn: () => api.post<BrokerTestResponse>('/broker/fyers/test'),
    onSuccess: (result) => {
      toast.success(
        result.ok
          ? 'Fyers answered in ' + formatLatency(result.latency_ms)
          : 'Fyers did not answer that request',
      )
      void client.invalidateQueries({ queryKey: queryKeys.system.budget() })
    },
  })

  const disconnect = useMutation({
    mutationFn: () => api.post<void>('/broker/fyers/disconnect'),
    onSuccess: () => {
      // Clears the started flag as well, so losing the token does not read as a login still in
      // flight.
      setLoginStarted(false)
      void client.invalidateQueries({ queryKey: queryKeys.broker.fyers() })
      void client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
      toast.success('The stored Fyers token was destroyed')
    },
  })

  const connectError = apiErrorMessage(connect.error)
  const finishError = apiErrorMessage(finish.error)
  const testError = apiErrorMessage(test.error)

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      {!hasCredentials ? (
        <p className="text-sm text-muted-foreground">
          Save the app id and app secret first. The login cannot start without them.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={start} disabled={!hasCredentials || connect.isPending}>
          {connect.isPending
            ? 'Starting'
            : connected
              ? 'Log in to Fyers again'
              : 'Connect to Fyers'}
        </Button>
        {awaitingReturn ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void client.invalidateQueries({ queryKey: queryKeys.broker.fyers() })}
          >
            Check now
          </Button>
        ) : null}
        {connected ? (
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={() => test.mutate()}
              disabled={test.isPending}
            >
              {test.isPending ? 'Testing' : 'Test connection'}
            </Button>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="outline" disabled={disconnect.isPending}>
                  Disconnect
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Destroy the stored Fyers token</AlertDialogTitle>
                  <AlertDialogDescription>
                    The app id and secret stay. Downloads in flight park and wait for the next
                    login, and nothing already downloaded is touched. You will need to complete
                    the Fyers login again before any new request can be sent.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Keep the token</AlertDialogCancel>
                  <AlertDialogAction onClick={() => disconnect.mutate()}>
                    Destroy the token
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </>
        ) : null}
      </div>

      {awaitingReturn && !connected ? (
        <div className="rounded-lg border border-dashed px-3 py-2.5 text-sm">
          <p className="font-medium">Waiting for the Fyers tab</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Sign in there with your Fyers password and TOTP. You are returned to this app
            automatically, and this screen notices within a few seconds. {CERTIFICATE_NOTE}
          </p>
          {authorizeUrl ? (
            <p className="mt-1 text-xs text-muted-foreground">
              If no tab opened, your browser blocked it.{' '}
              <a
                className="underline underline-offset-4"
                href={authorizeUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open the Fyers login page
              </a>
              .
            </p>
          ) : null}
        </div>
      ) : null}

      {connectError ? (
        <p role="alert" className="text-sm text-destructive">
          {connectError}
        </p>
      ) : null}
      {testError ? (
        <p role="alert" className="text-sm text-destructive">
          {testError}
        </p>
      ) : null}

      <div className="rounded-lg border px-3 py-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="text-sm font-medium">The return did not land</p>
            <p className="text-xs text-muted-foreground">
              Most often because the certificate warning was declined in the Fyers tab, so the
              browser never loaded the callback. Paste the URL from that tab instead.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setShowPaste((open) => !open)}
          >
            {showPaste ? 'Hide' : 'Paste the URL'}
          </Button>
        </div>

        {showPaste ? (
          <div className="mt-3 flex flex-col gap-2">
            <label className="text-xs text-muted-foreground" htmlFor="broker-redirected-url">
              The whole address from the tab Fyers returned you to. It starts with{' '}
              {REDIRECT_URI} and carries a long auth_code.
            </label>
            <Textarea
              id="broker-redirected-url"
              rows={3}
              spellCheck={false}
              autoComplete="off"
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              placeholder={REDIRECT_URI + '?s=ok&code=200&auth_code=...&state=...'}
              className="font-mono text-xs"
            />
            {inspectionMessage ? (
              <p className="text-xs text-destructive">{inspectionMessage}</p>
            ) : null}
            {finishError ? (
              <p role="alert" className="text-sm text-destructive">
                {finishError}
              </p>
            ) : null}
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                disabled={!inspection.ok || finish.isPending}
                onClick={() => finish.mutate(inspection.url)}
              >
                {finish.isPending ? 'Completing' : 'Complete the login'}
              </Button>
              <span className="text-xs text-muted-foreground">
                The pasted URL is verified against the login this app started, and is never
                logged.
              </span>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The settings tab
// ---------------------------------------------------------------------------

export interface BrokerPanelProps {
  status: BrokerStatus | undefined
  isLoading?: boolean
  error?: unknown
}

export function BrokerPanel({ status, isLoading, error }: BrokerPanelProps) {
  const [editing, setEditing] = useState(false)
  const configured = Boolean(status?.app_secret_configured)
  const message = apiErrorMessage(error)

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            Fyers connection
            <ConnectionBadge status={status} />
          </CardTitle>
          <CardDescription>
            The one interactive step in the system. Everything after it, including the scheduler,
            runs without you until the daily logout at 03:00 IST.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col">
          {message ? <p className="pb-2 text-sm text-destructive">{message}</p> : null}
          {isLoading && !status ? (
            <p className="text-sm text-muted-foreground">Reading the credential status.</p>
          ) : (
            <div className="divide-y">
              <StatusRow label="Label">{status?.label ?? 'not set'}</StatusRow>
              <StatusRow label="App id">
                <span className="font-mono text-xs">{status?.app_id ?? 'not set'}</span>
              </StatusRow>
              <StatusRow label="App secret">
                {configured
                  ? 'Stored and encrypted. Never displayed again.'
                  : 'Not stored yet.'}
              </StatusRow>
              <StatusRow label="Redirect URL">
                <span className="font-mono text-xs">{status?.redirect_uri ?? REDIRECT_URI}</span>
              </StatusRow>
              <StatusRow label="Plan">{status?.plan ?? 'standard'}</StatusRow>
              <StatusRow label="Token">{status?.token_state ?? 'none'}</StatusRow>
              <StatusRow label="Token expires">
                {status?.token_expires_at
                  ? formatDateTime(status.token_expires_at) +
                    ' (' +
                    formatRelative(status.token_expires_at) +
                    ')'
                  : 'no token stored'}
              </StatusRow>
              <StatusRow label="Token fingerprint">
                <span className="font-mono text-xs">{status?.token_fingerprint ?? 'not set'}</span>
              </StatusRow>
              {status?.last_error ? (
                <StatusRow label="Last error">
                  <span className="text-destructive">{status.last_error}</span>
                </StatusRow>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Login</CardTitle>
          <CardDescription>
            Sends you to Fyers to authenticate with your password and TOTP, and brings you back
            here. Parked downloads resume by themselves once a login lands.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FyersConnectPanel status={status} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>App registration</CardTitle>
          <CardDescription>
            The app id and secret from your Fyers app, and the redirect URL that must be
            registered against it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {editing || !configured ? (
            <div className="flex flex-col gap-3">
              <BrokerCredentialsForm status={status} onSaved={() => setEditing(false)} />
              {configured ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="self-start"
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </Button>
              ) : null}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <CopyField value={REDIRECT_URI} label="Redirect URL" />
              <p className="text-xs text-muted-foreground">
                Register exactly this string on the Fyers dashboard. Fyers matches it character
                for character.
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="self-start"
                onClick={() => setEditing(true)}
              >
                Replace the credentials
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export default BrokerPanel
