// The two pieces of the first run wizard that fail silently if they are wrong.
//
// The step machine decides which of the three steps is shown, and it is driven by facts read
// back from the server rather than by which button was clicked, because the Fyers callback can
// remount this screen in a different tab with no local state. If the machine is wrong the user
// is parked on a step they cannot finish, and nothing throws.
//
// The paste fallback's parser is the other one. It is what stands between "the certificate
// warning ate my login" and a completed one, and every mistake it can name is a mistake it has
// to name precisely, because the backend deliberately answers every failed state check with the
// same generic sentence.
//
// Every auth code, state and app id in this file is synthetic.

import { describe, expect, it } from 'vitest'

import {
  canEnterStep,
  furthestUnlockedStep,
  isStepComplete,
  isWizardComplete,
  resolveStep,
  stepState,
  type WizardFacts,
  type WizardStep,
} from '@/routes/setup'
import {
  REDIRECT_URI,
  inspectRedirectedUrl,
  redirectProblemMessage,
} from '@/components/settings/BrokerPanel'

const FRESH: WizardFacts = { hasAccount: false, hasCredentials: false, connected: false }
const AFTER_PASSCODE: WizardFacts = { hasAccount: true, hasCredentials: false, connected: false }
const AFTER_CREDENTIALS: WizardFacts = { hasAccount: true, hasCredentials: true, connected: false }
const DONE: WizardFacts = { hasAccount: true, hasCredentials: true, connected: true }

const ALL_STEPS: WizardStep[] = [1, 2, 3]

describe('wizard step progression', () => {
  it('opens on step 1 for a cold start', () => {
    expect(furthestUnlockedStep(FRESH)).toBe(1)
    expect(resolveStep(null, FRESH)).toBe(1)
  })

  it('advances one step for each fact the server reports', () => {
    expect(furthestUnlockedStep(AFTER_PASSCODE)).toBe(2)
    expect(furthestUnlockedStep(AFTER_CREDENTIALS)).toBe(3)
    expect(furthestUnlockedStep(DONE)).toBe(3)
  })

  it('locks every step ahead of the facts', () => {
    expect(canEnterStep(2, FRESH)).toBe(false)
    expect(canEnterStep(3, FRESH)).toBe(false)
    expect(canEnterStep(3, AFTER_PASSCODE)).toBe(false)
  })

  it('closes step 1 for good once the account exists', () => {
    // POST /auth/setup answers 409 on a provisioned instance, so offering the form again could
    // only produce an error.
    expect(canEnterStep(1, FRESH)).toBe(true)
    for (const facts of [AFTER_PASSCODE, AFTER_CREDENTIALS, DONE]) {
      expect(canEnterStep(1, facts)).toBe(false)
    }
  })

  it('keeps steps 2 and 3 open once they are reachable', () => {
    // Replacing a credential and logging in again are both repeatable.
    expect(canEnterStep(2, DONE)).toBe(true)
    expect(canEnterStep(3, DONE)).toBe(true)
  })

  it('drops a request the facts do not allow instead of honouring it', () => {
    // The click that asked for step 3 arrived before the credentials were saved.
    expect(resolveStep(3, AFTER_PASSCODE)).toBe(2)
    // A stale request to redo step 1 after the account exists.
    expect(resolveStep(1, AFTER_CREDENTIALS)).toBe(3)
  })

  it('honours a request to go back to a step that is still open', () => {
    expect(resolveStep(2, DONE)).toBe(2)
    expect(resolveStep(3, AFTER_CREDENTIALS)).toBe(3)
  })

  it('rebuilds the right step from the server facts alone, with no local state', () => {
    // This is the callback path: the wizard remounts in the tab Fyers returned to, with
    // requested === null, and still has to open on the step the user is actually on.
    expect(resolveStep(null, AFTER_CREDENTIALS)).toBe(3)
    expect(resolveStep(null, DONE)).toBe(3)
    expect(resolveStep(null, AFTER_PASSCODE)).toBe(2)
  })

  it('marks a step done only when the server has recorded it', () => {
    expect(isStepComplete(1, FRESH)).toBe(false)
    expect(isStepComplete(1, AFTER_PASSCODE)).toBe(true)
    expect(isStepComplete(2, AFTER_PASSCODE)).toBe(false)
    expect(isStepComplete(2, AFTER_CREDENTIALS)).toBe(true)
    expect(isStepComplete(3, AFTER_CREDENTIALS)).toBe(false)
    expect(isStepComplete(3, DONE)).toBe(true)
  })

  it('gives every step exactly one state', () => {
    for (const facts of [FRESH, AFTER_PASSCODE, AFTER_CREDENTIALS, DONE]) {
      const current = resolveStep(null, facts)
      const states = ALL_STEPS.map((step) => stepState(step, current, facts))
      expect(states.filter((state) => state === 'current')).toHaveLength(1)
      expect(states[current - 1]).toBe('current')
    }
  })

  it('shows a finished earlier step as done rather than as locked', () => {
    expect(stepState(1, 2, AFTER_PASSCODE)).toBe('done')
    expect(stepState(2, 3, DONE)).toBe('done')
    expect(stepState(3, 1, FRESH)).toBe('locked')
  })

  it('is complete only when all three facts hold', () => {
    expect(isWizardComplete(FRESH)).toBe(false)
    expect(isWizardComplete(AFTER_PASSCODE)).toBe(false)
    expect(isWizardComplete(AFTER_CREDENTIALS)).toBe(false)
    expect(isWizardComplete(DONE)).toBe(true)
  })
})

describe('paste fallback url parsing', () => {
  const good = REDIRECT_URI + '?s=ok&code=200&auth_code=synthetic-auth-code&state=synthetic-state'

  it('accepts the URL Fyers returns', () => {
    const result = inspectRedirectedUrl(good)
    expect(result.ok).toBe(true)
    expect(result.problem).toBeNull()
    expect(result.hasAuthCode).toBe(true)
    expect(result.hasState).toBe(true)
    expect(result.url).toBe(good)
    expect(redirectProblemMessage(result)).toBeNull()
  })

  it('trims what the clipboard adds', () => {
    const result = inspectRedirectedUrl('  ' + good + '\n')
    expect(result.ok).toBe(true)
    expect(result.url).toBe(good)
  })

  it('never carries the auth code or the state in the result', () => {
    const serialised = JSON.stringify(inspectRedirectedUrl(good))
    // The url field is the one place the values survive, because it is what gets sent.
    const withoutUrl = { ...inspectRedirectedUrl(good), url: '' }
    expect(JSON.stringify(withoutUrl)).not.toContain('synthetic-auth-code')
    expect(JSON.stringify(withoutUrl)).not.toContain('synthetic-state')
    expect(serialised).toContain('synthetic-auth-code')
  })

  it('completes a bare query string into the callback URL', () => {
    // Pasting from the address bar after the question mark is a common miss, and the scheme,
    // host and path are fixed, so there is nothing to guess.
    const result = inspectRedirectedUrl('s=ok&code=200&auth_code=synthetic-auth-code&state=xyz')
    expect(result.ok).toBe(true)
    expect(result.url).toBe(REDIRECT_URI + '?s=ok&code=200&auth_code=synthetic-auth-code&state=xyz')
  })

  it('accepts the URL without its scheme', () => {
    const result = inspectRedirectedUrl(
      '127.0.0.1:8000/fyers/callback?auth_code=synthetic-auth-code&state=xyz',
    )
    expect(result.ok).toBe(true)
    expect(result.url).toBe(
      '127.0.0.1:8000/fyers/callback?auth_code=synthetic-auth-code&state=xyz',
    )
  })

  it('ignores a fragment after the query', () => {
    const result = inspectRedirectedUrl(good + '#section')
    expect(result.ok).toBe(true)
    expect(result.hasAuthCode).toBe(true)
  })

  it('names an empty box rather than sending it', () => {
    const result = inspectRedirectedUrl('   ')
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('empty')
    expect(redirectProblemMessage(result)).toContain('Paste the whole URL')
  })

  it('names text that is not a callback at all', () => {
    const result = inspectRedirectedUrl('the login page would not load')
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('not_a_callback')
    expect(redirectProblemMessage(result)).toContain(REDIRECT_URI)
  })

  it('recognises the authorize URL as the wrong tab', () => {
    // This is the page the user was sent to. It has a state and no auth code, so without this
    // branch it would be reported as "no auth code" and the user would be told to start again
    // when they only copied the wrong tab.
    const authorize =
      'https://api-t1.fyers.in/api/v3/generate-authcode?client_id=SYNTH01-100' +
      '&redirect_uri=https%3A%2F%2F127.0.0.1%3A8000%2Ffyers%2Fcallback' +
      '&response_type=code&state=synthetic-state'
    const result = inspectRedirectedUrl(authorize)
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('authorize_url')
    expect(redirectProblemMessage(result)).toContain('Fyers login page you were sent to')
  })

  it('reports a failure Fyers put in the query, with its own message', () => {
    const result = inspectRedirectedUrl(
      REDIRECT_URI + '?s=error&code=-99&message=invalid+request',
    )
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('broker_error')
    expect(result.brokerStatus).toBe('error')
    expect(result.brokerCode).toBe('-99')
    expect(result.brokerMessage).toBe('invalid request')
    expect(redirectProblemMessage(result)).toContain('invalid request')
  })

  it('treats a code without an auth code as a broker failure', () => {
    const result = inspectRedirectedUrl(REDIRECT_URI + '?code=-16&message=invalid+token')
    expect(result.problem).toBe('broker_error')
  })

  it('names a callback that carries no auth code', () => {
    const result = inspectRedirectedUrl(REDIRECT_URI + '?state=synthetic-state')
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('no_auth_code')
    expect(result.hasState).toBe(true)
  })

  it('names a callback that carries no state', () => {
    // The backend matches the state against a single use row bound to this session, so a URL
    // without one cannot be verified and is worth catching before the round trip.
    const result = inspectRedirectedUrl(REDIRECT_URI + '?s=ok&auth_code=synthetic-auth-code')
    expect(result.ok).toBe(false)
    expect(result.problem).toBe('no_state')
  })

  it('treats an empty parameter as absent', () => {
    const result = inspectRedirectedUrl(REDIRECT_URI + '?auth_code=&state=synthetic-state')
    expect(result.problem).toBe('no_auth_code')
  })

  it('gives every problem a message that says what to do next', () => {
    const inputs = [
      '',
      'not a url',
      'https://api-t1.fyers.in/api/v3/generate-authcode?client_id=SYNTH01-100&response_type=code',
      REDIRECT_URI + '?s=error&code=-99&message=nope',
      REDIRECT_URI + '?state=synthetic-state',
      REDIRECT_URI + '?auth_code=synthetic-auth-code',
    ]
    for (const input of inputs) {
      const message = redirectProblemMessage(inspectRedirectedUrl(input))
      expect(message).toBeTruthy()
      expect(String(message).length).toBeGreaterThan(40)
    }
  })
})
