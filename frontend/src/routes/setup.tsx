import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'

import {
  BrokerCredentialsForm,
  CERTIFICATE_NOTE,
  CopyField,
  FyersConnectPanel,
  REDIRECT_URI,
  apiErrorMessage,
} from '@/components/settings/BrokerPanel'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { BrokerStatus, CurrentUser, SetupResponse } from '@/lib/api/types'

// The first run wizard: passcode, credentials, connect.
//
// The progression is derived from what the server reports, never from which button was clicked
// last. That matters because of one specific path: the Fyers callback lands on /settings, and on
// an instance that is not marked provisioned yet BootstrapGate sends that straight back to
// /setup. The wizard therefore remounts, in a different tab, in the middle of the flow, with no
// local state at all, and it still has to open on the right step. Reading the facts back from
// GET /auth/me and GET /broker/fyers is what makes that work.
//
// The wizard deliberately does NOT invalidate the bootstrap query when the account is created.
// The gate treats a provisioned instance on /setup as a mistake and redirects to the dashboard,
// so invalidating early would eject the user from their own wizard between step one and step
// two. Bootstrap is refreshed once, at the end, by the button that leaves.

/** The minimum the backend enforces in security/passwords.validate_password. */
export const MIN_PASSCODE_LENGTH = 12

export type WizardStep = 1 | 2 | 3

/** What the server says has been done. Nothing here is remembered locally. */
export interface WizardFacts {
  /** GET /auth/me answered, so the account exists and this browser is signed in to it. */
  hasAccount: boolean
  /** An app id and an encrypted app secret are stored. */
  hasCredentials: boolean
  /** A live Fyers access token is stored. */
  connected: boolean
}

export type StepState = 'current' | 'done' | 'available' | 'locked'

export const STEP_TITLES: Record<WizardStep, string> = {
  1: 'Set a local passcode',
  2: 'Add your Fyers app credentials',
  3: 'Connect to Fyers',
}

/** True once this step's work is recorded on the server. */
export function isStepComplete(step: WizardStep, facts: WizardFacts): boolean {
  switch (step) {
    case 1:
      return facts.hasAccount
    case 2:
      return facts.hasCredentials
    case 3:
      return facts.connected
  }
}

/**
 * Whether a step can be opened.
 *
 * Step 1 closes for good once the account exists: `POST /auth/setup` is reachable only while
 * `app_user` is empty and answers 409 afterwards, so offering the form again would only produce
 * an error. Steps 2 and 3 stay open, because replacing a credential and logging in again are
 * both things a user does more than once.
 */
export function canEnterStep(step: WizardStep, facts: WizardFacts): boolean {
  switch (step) {
    case 1:
      return !facts.hasAccount
    case 2:
      return facts.hasAccount
    case 3:
      return facts.hasAccount && facts.hasCredentials
  }
}

/** The step the wizard opens on when the user has not asked for one. */
export function furthestUnlockedStep(facts: WizardFacts): WizardStep {
  if (!facts.hasAccount) {
    return 1
  }
  if (!facts.hasCredentials) {
    return 2
  }
  return 3
}

/** The step actually shown: what was asked for when the facts allow it, and the furthest
 *  unlocked step otherwise. A request that the facts overtake is dropped rather than honoured,
 *  which is what stops a stale click from parking the user on a step they cannot complete. */
export function resolveStep(requested: WizardStep | null, facts: WizardFacts): WizardStep {
  if (requested !== null && canEnterStep(requested, facts)) {
    return requested
  }
  return furthestUnlockedStep(facts)
}

export function stepState(step: WizardStep, current: WizardStep, facts: WizardFacts): StepState {
  if (step === current) {
    return 'current'
  }
  if (isStepComplete(step, facts)) {
    return 'done'
  }
  return canEnterStep(step, facts) ? 'available' : 'locked'
}

/** All three steps recorded on the server. */
export function isWizardComplete(facts: WizardFacts): boolean {
  return facts.hasAccount && facts.hasCredentials && facts.connected
}

// ---------------------------------------------------------------------------
// Step 1
// ---------------------------------------------------------------------------

interface PasscodeFormValues {
  username: string
  password: string
  confirm_password: string
}

function PasscodeStep({ onCreated }: { onCreated: () => void }) {
  const client = useQueryClient()
  const form = useForm<PasscodeFormValues>({
    defaultValues: { username: '', password: '', confirm_password: '' },
  })

  const create = useMutation({
    mutationFn: (values: PasscodeFormValues) =>
      api.post<SetupResponse>('/auth/setup', {
        body: { username: values.username.trim(), password: values.password },
      }),
    onSuccess: async () => {
      form.reset()
      // Only the session probe. Bootstrap is left alone on purpose: see the note at the top.
      await client.invalidateQueries({ queryKey: queryKeys.auth.me() })
      onCreated()
    },
  })

  const message = apiErrorMessage(create.error)

  return (
    <form
      className="flex flex-col gap-4"
      autoComplete="off"
      onSubmit={form.handleSubmit((values) => create.mutate(values))}
    >
      <FieldGroup className="gap-4">
        <Field>
          <FieldLabel htmlFor="setup-username">Username</FieldLabel>
          <Input
            id="setup-username"
            autoComplete="username"
            autoFocus
            spellCheck={false}
            aria-invalid={Boolean(form.formState.errors.username)}
            {...form.register('username', {
              required: 'Choose a username.',
              minLength: { value: 3, message: 'At least 3 characters.' },
            })}
          />
          <FieldDescription>
            Only you will use it. It is stored on this machine and sent nowhere.
          </FieldDescription>
          <FieldError errors={[form.formState.errors.username]} />
        </Field>

        <Field>
          <FieldLabel htmlFor="setup-password">Passcode</FieldLabel>
          <Input
            id="setup-password"
            type="password"
            autoComplete="new-password"
            aria-invalid={Boolean(form.formState.errors.password)}
            {...form.register('password', {
              required: 'Choose a passcode.',
              minLength: {
                value: MIN_PASSCODE_LENGTH,
                message:
                  'The passcode must be at least ' + String(MIN_PASSCODE_LENGTH) + ' characters.',
              },
            })}
          />
          <FieldDescription>
            At least {MIN_PASSCODE_LENGTH} characters. Hashed with Argon2id and never stored in
            plaintext, so it cannot be recovered if it is lost, only replaced.
          </FieldDescription>
          <FieldError errors={[form.formState.errors.password]} />
        </Field>

        <Field>
          <FieldLabel htmlFor="setup-confirm">Repeat the passcode</FieldLabel>
          <Input
            id="setup-confirm"
            type="password"
            autoComplete="new-password"
            aria-invalid={Boolean(form.formState.errors.confirm_password)}
            {...form.register('confirm_password', {
              validate: (value, values) =>
                value === values.password || 'The two passcodes do not match.',
            })}
          />
          <FieldError errors={[form.formState.errors.confirm_password]} />
        </Field>
      </FieldGroup>

      {message ? (
        <p role="alert" className="text-sm text-destructive">
          {message}
        </p>
      ) : null}

      <div>
        <Button type="submit" size="sm" disabled={create.isPending}>
          {create.isPending ? 'Creating' : 'Set the passcode and continue'}
        </Button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// The wizard
// ---------------------------------------------------------------------------

function StepRail({
  current,
  facts,
  onSelect,
}: {
  current: WizardStep
  facts: WizardFacts
  onSelect: (step: WizardStep) => void
}) {
  const steps: WizardStep[] = [1, 2, 3]
  return (
    <ol className="flex flex-col gap-1.5 sm:flex-row sm:gap-2">
      {steps.map((step) => {
        const state = stepState(step, current, facts)
        const clickable = state === 'available' || state === 'done'
        return (
          <li key={step} className="flex-1">
            <button
              type="button"
              disabled={!clickable}
              aria-current={state === 'current' ? 'step' : undefined}
              onClick={() => onSelect(step)}
              className={cn(
                'flex w-full flex-col gap-0.5 rounded-lg border px-3 py-2 text-left text-sm transition-colors',
                state === 'current' && 'border-primary/50 bg-primary/5',
                state === 'done' && 'text-muted-foreground',
                state === 'locked' && 'text-muted-foreground opacity-60',
                clickable && 'hover:bg-muted/50',
                !clickable && 'cursor-default',
              )}
            >
              <span className="flex items-baseline justify-between gap-2 text-xs">
                <span className="tabular-nums">Step {step}</span>
                {isStepComplete(step, facts) ? <span>Done</span> : null}
              </span>
              {/* Wraps rather than truncates. The three labels are the map of the whole flow, and
                  a label cut off at "Set a local passc" is not a map. */}
              <span className="text-sm">{STEP_TITLES[step]}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

export function SetupRoute() {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [requested, setRequested] = useState<WizardStep | null>(null)

  // A 401 here is the answer, not a failure, so it is not retried and the error is not surfaced.
  const me = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: () => api.get<CurrentUser>('/auth/me'),
    retry: false,
    staleTime: 0,
  })
  const hasAccount = me.isSuccess

  const broker = useQuery({
    queryKey: queryKeys.broker.fyers(),
    queryFn: () => api.get<BrokerStatus>('/broker/fyers'),
    enabled: hasAccount,
    retry: false,
  })

  const facts: WizardFacts = {
    hasAccount,
    hasCredentials: Boolean(broker.data?.app_secret_configured),
    connected: Boolean(broker.data?.connected && broker.data.token_state === 'active'),
  }

  const step = resolveStep(requested, facts)
  const complete = isWizardComplete(facts)

  const sessionBroken =
    me.error instanceof ApiError && !me.error.isUnauthenticated ? me.error : null

  const finish = async () => {
    // The last thing the wizard does. From here on the gate treats this instance as provisioned.
    await client.invalidateQueries({ queryKey: queryKeys.bootstrap() })
    navigate('/', { replace: true })
  }

  if (me.isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Starting ExpiryManager</p>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-background px-6 py-10 text-foreground">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-5">
        <header className="flex flex-col gap-1">
          <h1 className="font-heading text-lg font-semibold tracking-tight">
            Set up ExpiryManager
          </h1>
          <p className="text-sm text-muted-foreground">
            Three steps, once. There is no configuration file to edit and no environment variable
            to set: everything below is stored inside the app data directory on this machine.
          </p>
        </header>

        <StepRail current={step} facts={facts} onSelect={setRequested} />

        {sessionBroken ? (
          <p role="alert" className="text-sm text-destructive">
            {apiErrorMessage(sessionBroken)}
          </p>
        ) : null}

        {step === 1 ? (
          <Card>
            <CardHeader>
              <CardTitle>Step 1. {STEP_TITLES[1]}</CardTitle>
              <CardDescription>
                This protects the app itself on this machine. It is not your Fyers password, and
                it is not sent anywhere.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <PasscodeStep onCreated={() => setRequested(2)} />
            </CardContent>
          </Card>
        ) : null}

        {step === 2 ? (
          <>
            <Card>
              <CardHeader>
                <CardTitle>Before you fill this in</CardTitle>
                <CardDescription>
                  Register this exact redirect URL against your app on the Fyers dashboard.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <CopyField value={REDIRECT_URI} label="Redirect URL" />
                <p className="text-sm text-muted-foreground">
                  Fyers matches the redirect URL character for character, so it has to be this
                  string exactly: the same scheme, the same 127.0.0.1, the same port 8000 and the
                  same path. A value that differs by one character does not fail when you save it
                  here. It fails later, during the login, with a message that reads like a wrong
                  app id.
                </p>
                <p className="text-sm text-muted-foreground">
                  It says https because that is what Fyers requires. {CERTIFICATE_NOTE}
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Step 2. {STEP_TITLES[2]}</CardTitle>
                <CardDescription>
                  Both values come from your app registration on the Fyers dashboard.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <BrokerCredentialsForm
                  status={broker.data}
                  submitLabel="Save and continue"
                  onSaved={() => setRequested(3)}
                />
              </CardContent>
            </Card>
          </>
        ) : null}

        {step === 3 ? (
          <Card>
            <CardHeader>
              <CardTitle>Step 3. {STEP_TITLES[3]}</CardTitle>
              <CardDescription>
                You are sent to Fyers to authenticate with your password and TOTP, and returned
                here. This is the only manual step in the whole system.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {complete ? (
                <div className="rounded-lg border border-chart-5/40 bg-chart-5/10 px-3 py-2.5">
                  <p className="text-sm font-medium">Connected to Fyers</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    A token is stored and encrypted. The schedules are enabled and the four
                    builtin underlyings are ready. Nothing has been downloaded yet: open
                    Expiries, pick an underlying and select some expiries.
                  </p>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Press Connect below. A new tab opens on the Fyers login page. {CERTIFICATE_NOTE}
                </p>
              )}

              <FyersConnectPanel status={broker.data} />
            </CardContent>
          </Card>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" disabled={!complete} onClick={() => void finish()}>
            Open the dashboard
          </Button>
          {!complete ? (
            <span className="text-xs text-muted-foreground">
              Available once all three steps are done.
            </span>
          ) : null}
        </div>
      </div>
    </div>
  )
}

export default SetupRoute
