import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { LoginResponse } from '@/lib/api/types'
import { formatDuration } from '@/lib/format'

// The local passcode screen. It guards the app, not the Fyers account.
//
// The one thing this screen has to get right beyond the obvious is the locked state. Ten failed
// attempts lock the account for fifteen minutes, and two rate limits sit in front of that: five
// attempts per fifteen minutes for one username from one address, and twenty per fifteen minutes
// from one address whatever username is tried. All three answer with Retry-After, and all three
// are indistinguishable from "wrong passcode" unless the wait is actually shown. A user who is
// locked out and told only "not accepted" will keep typing, and every attempt they make while
// locked is another attempt that does not count as a success.
//
// So the wait is surfaced as a live countdown, the submit button is disabled while it runs, and
// the text says the lock clears by itself.

/** Codes the backend can answer with here. Matching on these rather than on message text. */
const CODE_ACCOUNT_LOCKED = 'account_locked'
const CODE_INVALID_CREDENTIALS = 'invalid_credentials'

interface LoginFormValues {
  username: string
  password: string
}

/** Seconds left until `deadline`, recomputed once a second while there are any.
 *
 *  A deadline rather than a counter, so a slow render or a backgrounded tab cannot make the
 *  countdown drift away from the wall clock the server is measuring against. */
function useSecondsRemaining(deadline: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  const [lastDeadline, setLastDeadline] = useState(deadline)

  // Adjusting state during render, which is the sanctioned way to react to a changed input. An
  // effect would work too, but it would render the stale count once first, and a countdown whose
  // first frame is wrong is exactly the frame a user reads.
  if (deadline !== lastDeadline) {
    setLastDeadline(deadline)
    setNow(Date.now())
  }

  useEffect(() => {
    if (deadline === null) {
      return
    }
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [deadline])

  return deadline === null ? 0 : Math.max(0, Math.ceil((deadline - now) / 1000))
}

interface Refusal {
  kind: 'locked' | 'rate_limited' | 'rejected' | 'unreachable' | 'other'
  message: string
  /** Epoch milliseconds, when the backend said how long to wait. */
  deadline: number | null
}

/** Turns one failed login into the sentence to show and the wait to count down. */
export function describeLoginFailure(error: unknown, now: number = Date.now()): Refusal {
  if (!(error instanceof ApiError)) {
    return {
      kind: 'other',
      message: 'Something went wrong. Try again.',
      deadline: null,
    }
  }

  const wait = error.retryAfterSeconds
  const deadline = wait !== null && wait > 0 ? now + wait * 1000 : null

  if (error.isNetworkError) {
    return {
      kind: 'unreachable',
      message:
        'The ExpiryManager backend did not answer on http://127.0.0.1:8000. Check that the ' +
        'process is still running, then try again.',
      deadline: null,
    }
  }

  if (error.status === 423 || error.code === CODE_ACCOUNT_LOCKED) {
    return {
      kind: 'locked',
      message:
        'Too many failed attempts, so the account is locked for a short period. It unlocks by ' +
        'itself, and there is nothing to reset. Wait for the countdown, then try again.',
      deadline,
    }
  }

  if (error.isRateLimited) {
    return {
      kind: 'rate_limited',
      message:
        'Too many sign in attempts from this browser in a short window. The limit clears by ' +
        'itself. Wait for the countdown, then try again.',
      deadline,
    }
  }

  if (error.status === 401 || error.code === CODE_INVALID_CREDENTIALS) {
    return {
      kind: 'rejected',
      // The backend answers the same sentence whether the username or the passcode was wrong,
      // deliberately, so it is shown as written rather than guessed at.
      message: error.message,
      deadline: null,
    }
  }

  if (error.status === 422) {
    return { kind: 'other', message: error.message, deadline: null }
  }

  return {
    kind: 'other',
    message:
      error.message + (error.correlationId ? ' Reference ' + error.correlationId + '.' : ''),
    deadline: null,
  }
}

export function LoginRoute() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [refusal, setRefusal] = useState<Refusal | null>(null)

  const form = useForm<LoginFormValues>({ defaultValues: { username: '', password: '' } })

  const waitSeconds = useSecondsRemaining(refusal?.deadline ?? null)
  const waiting = waitSeconds > 0

  const login = useMutation({
    mutationFn: (values: LoginFormValues) =>
      api.post<LoginResponse>('/auth/login', {
        body: { username: values.username, password: values.password },
      }),
    onSuccess: async () => {
      setRefusal(null)
      form.reset()
      // The gate holds both of these. Refetching them is what turns this screen into the app:
      // BootstrapGate sends a successful session on /login straight to the dashboard.
      await client.invalidateQueries({ queryKey: queryKeys.auth.me() })
      await client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
      navigate('/', { replace: true })
    },
    onError: (error) => {
      setRefusal(describeLoginFailure(error))
      form.setValue('password', '')
    },
  })

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 py-10 text-foreground">
      <div className="flex w-full max-w-md flex-col gap-4">
        <Card>
          <CardHeader>
            <CardTitle>ExpiryManager</CardTitle>
            <CardDescription>
              Enter the local passcode for this installation. It protects the app on this machine
              and is not your Fyers password.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-4"
              onSubmit={form.handleSubmit((values) => login.mutate(values))}
            >
              <FieldGroup className="gap-4">
                <Field>
                  <FieldLabel htmlFor="login-username">Username</FieldLabel>
                  <Input
                    id="login-username"
                    autoComplete="username"
                    autoFocus
                    spellCheck={false}
                    aria-invalid={Boolean(form.formState.errors.username)}
                    {...form.register('username', { required: 'Enter your username.' })}
                  />
                  <FieldError errors={[form.formState.errors.username]} />
                </Field>

                <Field>
                  <FieldLabel htmlFor="login-password">Passcode</FieldLabel>
                  <Input
                    id="login-password"
                    type="password"
                    autoComplete="current-password"
                    aria-invalid={Boolean(form.formState.errors.password)}
                    {...form.register('password', { required: 'Enter your passcode.' })}
                  />
                  <FieldError errors={[form.formState.errors.password]} />
                </Field>
              </FieldGroup>

              {refusal ? (
                <div role="alert" className="flex flex-col gap-1">
                  <p className="text-sm text-destructive">{refusal.message}</p>
                  {waiting ? (
                    <p className="text-sm tabular-nums">
                      Try again in {formatDuration(waitSeconds)}.
                    </p>
                  ) : refusal.deadline !== null ? (
                    <p className="text-sm">The wait is over. Try again.</p>
                  ) : null}
                </div>
              ) : null}

              <Button type="submit" disabled={login.isPending || waiting}>
                {login.isPending ? 'Signing in' : waiting ? 'Locked' : 'Sign in'}
              </Button>
            </form>
          </CardContent>
        </Card>

        <p className="px-1 text-xs text-muted-foreground">
          There is no passcode reset. It is hashed with Argon2id and cannot be recovered. If it is
          lost, remove the account row from config.sqlite3 in the data directory and the first run
          wizard will create a new one. Nothing you have downloaded is affected.
        </p>
      </div>
    </div>
  )
}

export default LoginRoute
