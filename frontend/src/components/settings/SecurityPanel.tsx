import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { StatusRow, apiErrorMessage } from '@/components/settings/BrokerPanel'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, BrokerStatus, CurrentUser } from '@/lib/api/types'
import { formatDateTime, formatRelative } from '@/lib/format'

// The Security tab answers three questions and nothing else: who this browser is signed in as,
// how to change the local passcode, and what happens to the broker token overnight.
//
// The third one is here rather than on the Broker tab because it is the fact that surprises
// people. The token is not left to expire and be discovered dead in the middle of a job. It is
// dropped deliberately at 03:00 IST, jobs checkpoint and park at that moment, and they resume
// after the next login. Nothing is lost, and there is no refresh path to offer: SEBI
// discontinued the refresh token flow from 1 April 2026, so a human completing the OAuth login
// is the only way a new token exists.

/** The minimum the backend enforces in security/passwords.validate_password. Repeated here so
 *  the form says so before the round trip, not so it becomes the authority. */
export const MIN_PASSCODE_LENGTH = 12

interface PasswordFormValues {
  current_password: string
  new_password: string
  confirm_password: string
}

export interface SecurityPanelProps {
  bootstrap: Bootstrap | undefined
  brokerStatus: BrokerStatus | undefined
}

export function SecurityPanel({ bootstrap, brokerStatus }: SecurityPanelProps) {
  const client = useQueryClient()
  const navigate = useNavigate()

  const me = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: () => api.get<CurrentUser>('/auth/me'),
    retry: false,
  })

  const form = useForm<PasswordFormValues>({
    defaultValues: { current_password: '', new_password: '', confirm_password: '' },
  })

  const change = useMutation({
    mutationFn: (values: PasswordFormValues) =>
      api.post<void>('/auth/password', {
        body: {
          current_password: values.current_password,
          new_password: values.new_password,
        },
      }),
    onSuccess: () => {
      form.reset()
      // The backend deletes every session for this user and issues a new one for this browser,
      // so any other browser that was signed in is now signed out. Say so: a silent sign out
      // elsewhere reads as a bug.
      toast.success('Passcode changed. Every other signed in browser was signed out.')
      void client.invalidateQueries({ queryKey: queryKeys.auth.me() })
    },
  })

  const signOut = useMutation({
    mutationFn: () => api.post<void>('/auth/logout'),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.auth.me() })
      await client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
      navigate('/login', { replace: true })
    },
  })

  const changeError = apiErrorMessage(change.error)
  const signOutError = apiErrorMessage(signOut.error)
  const tokenExpiry = brokerStatus?.token_expires_at ?? bootstrap?.token_expires_at ?? null

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>This browser</CardTitle>
          <CardDescription>
            The session is a cookie that is HttpOnly, Secure and same site. It slides on use for
            up to 8 hours idle and expires 7 days after it was issued whatever happens.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col">
          <div className="divide-y">
            <StatusRow label="Signed in as">{me.data?.username ?? 'not signed in'}</StatusRow>
            <StatusRow label="Session expires">
              {me.data?.session_expires_at
                ? formatDateTime(me.data.session_expires_at) +
                  ' (' +
                  formatRelative(me.data.session_expires_at) +
                  ')'
                : 'unknown'}
            </StatusRow>
          </div>
          {signOutError ? (
            <p role="alert" className="pt-2 text-sm text-destructive">
              {signOutError}
            </p>
          ) : null}
          <div className="pt-3">
            <Button
              size="sm"
              variant="outline"
              onClick={() => signOut.mutate()}
              disabled={signOut.isPending}
            >
              {signOut.isPending ? 'Signing out' : 'Sign out'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Local passcode</CardTitle>
          <CardDescription>
            This protects the app itself, not your Fyers account. It is hashed with Argon2id and
            is never stored in plaintext, so it cannot be recovered, only replaced.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="flex max-w-md flex-col gap-4"
            autoComplete="off"
            onSubmit={form.handleSubmit((values) => change.mutate(values))}
          >
            <FieldGroup className="gap-4">
              <Field>
                <FieldLabel htmlFor="security-current">Current passcode</FieldLabel>
                <Input
                  id="security-current"
                  type="password"
                  autoComplete="current-password"
                  aria-invalid={Boolean(form.formState.errors.current_password)}
                  {...form.register('current_password', {
                    required: 'Enter the passcode you sign in with today.',
                  })}
                />
                <FieldError errors={[form.formState.errors.current_password]} />
              </Field>

              <Field>
                <FieldLabel htmlFor="security-new">New passcode</FieldLabel>
                <Input
                  id="security-new"
                  type="password"
                  autoComplete="new-password"
                  aria-invalid={Boolean(form.formState.errors.new_password)}
                  {...form.register('new_password', {
                    required: 'Choose a new passcode.',
                    minLength: {
                      value: MIN_PASSCODE_LENGTH,
                      message:
                        'The passcode must be at least ' +
                        String(MIN_PASSCODE_LENGTH) +
                        ' characters.',
                    },
                  })}
                />
                <FieldDescription>
                  At least {MIN_PASSCODE_LENGTH} characters. Length is what matters here, so a
                  short phrase beats a short scramble.
                </FieldDescription>
                <FieldError errors={[form.formState.errors.new_password]} />
              </Field>

              <Field>
                <FieldLabel htmlFor="security-confirm">Repeat the new passcode</FieldLabel>
                <Input
                  id="security-confirm"
                  type="password"
                  autoComplete="new-password"
                  aria-invalid={Boolean(form.formState.errors.confirm_password)}
                  {...form.register('confirm_password', {
                    validate: (value, values) =>
                      value === values.new_password || 'The two new passcodes do not match.',
                  })}
                />
                <FieldError errors={[form.formState.errors.confirm_password]} />
              </Field>
            </FieldGroup>

            {changeError ? (
              <p role="alert" className="text-sm text-destructive">
                {changeError}
              </p>
            ) : null}

            <div className="flex items-center gap-3">
              <Button type="submit" size="sm" disabled={change.isPending}>
                {change.isPending ? 'Changing' : 'Change passcode'}
              </Button>
              <span className="text-xs text-muted-foreground">
                Signs out every other browser.
              </span>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Broker token lifecycle</CardTitle>
          <CardDescription>
            One interactive Fyers login a day, at a time you choose, and never in the middle of a
            job.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col">
          <div className="divide-y">
            <StatusRow label="Token state">{brokerStatus?.token_state ?? 'none'}</StatusRow>
            <StatusRow label="Token expires">
              {tokenExpiry
                ? formatDateTime(tokenExpiry) + ' (' + formatRelative(tokenExpiry) + ')'
                : 'no token stored'}
            </StatusRow>
            <StatusRow label="Scheduled logout">Every day at 03:00 IST</StatusRow>
          </div>
          <p className="pt-3 text-sm text-muted-foreground">
            At 03:00 IST the stored token is destroyed on purpose rather than left to be
            discovered dead. Any job that is running checkpoints at that moment and parks
            awaiting authentication, and resumes at the exact request it stopped on after your
            next login. There is no automatic renewal to enable: the refresh token flow was
            discontinued from 1 April 2026, so a human completing the login is the only way a new
            token exists.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How the secrets are held</CardTitle>
          <CardDescription>
            Everything is on this machine. Nothing is sent anywhere except to Fyers.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="flex list-disc flex-col gap-1.5 pl-4 text-sm text-muted-foreground">
            <li>The local passcode is hashed with Argon2id. The hash is not reversible.</li>
            <li>
              The Fyers app secret and the access token are encrypted with AES-256-GCM, bound to
              the row they live in, under a key held in a 0600 file outside the database.
            </li>
            <li>
              The server listens on 127.0.0.1 only, over https, with a certificate it generated
              itself. Nothing on your network can reach it.
            </li>
            <li>
              Auth codes, tokens and app secrets are stripped from every log line before it is
              written, including the callback URL in the access log.
            </li>
            {bootstrap?.data_dir ? (
              <li>
                Everything lives under{' '}
                <span className="font-mono text-xs">{bootstrap.data_dir}</span>, a 0700 directory.
              </li>
            ) : null}
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}

export default SecurityPanel
