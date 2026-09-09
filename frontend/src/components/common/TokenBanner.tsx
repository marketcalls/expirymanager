import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'

import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, BrokerConnectResponse } from '@/lib/api/types'
import { formatDateTime, formatRelative } from '@/lib/format'

// The Fyers access token is a fact about the whole application, not about one screen, so its bad
// state is a persistent banner rather than an error on whichever page happened to make the call
// that failed.
//
// There is no refresh path to offer. SEBI discontinued the refresh token flow from 1 April 2026,
// so an expired token is re-obtained only by a human completing the OAuth login, and the token
// is dropped on a schedule at 03:00 IST rather than discovered to be dead mid-job. Running jobs
// checkpoint and park; they resume after the next login. That is the message here: work is
// waiting, not lost.

export type TokenBannerVariant = 'banner' | 'chip'

export interface TokenBannerProps {
  bootstrap: Bootstrap | undefined
  /** 'banner' is the persistent full width form. 'chip' is the top bar status. */
  variant?: TokenBannerVariant
  /** Jobs parked awaiting authentication, when the caller knows the count. */
  parkedJobs?: number
  className?: string
}

function tokenLabel(bootstrap: Bootstrap): string {
  if (bootstrap.needs_reauth) {
    return 'Login required'
  }
  switch (bootstrap.token_state) {
    case 'active':
      return 'Fyers connected'
    case 'expired':
      return 'Token expired'
    case 'revoked':
      return 'Token revoked'
    default:
      return 'Not connected'
  }
}

/**
 * Starts the OAuth login and opens the Fyers authorize page.
 *
 * The blank tab is opened synchronously inside the click, before the request is made, because a
 * window.open that runs after an await has lost the user gesture and is blocked. The URL is
 * written into that already-open tab when the response lands.
 */
function useConnectFyers() {
  const client = useQueryClient()
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null)
  // A ref and not state. The handle is a live browser object that is written to, never
  // rendered, and putting it in state would both mutate a state value and rerender for nothing.
  const pendingTabRef = useRef<Window | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.post<BrokerConnectResponse>('/broker/fyers/connect'),
    onSuccess: (data) => {
      setAuthorizeUrl(data.authorize_url)
      const tab = pendingTabRef.current
      if (tab && !tab.closed) {
        tab.location.href = data.authorize_url
      }
      pendingTabRef.current = null
      void client.invalidateQueries({ queryKey: queryKeys.broker.fyers() })
    },
    onError: () => {
      pendingTabRef.current?.close()
      pendingTabRef.current = null
    },
  })

  const start = useCallback(() => {
    setAuthorizeUrl(null)
    pendingTabRef.current = window.open('', '_blank', 'noopener,noreferrer')
    mutation.mutate()
  }, [mutation])

  return { start, authorizeUrl, mutation }
}

export function TokenBanner({
  bootstrap,
  variant = 'banner',
  parkedJobs,
  className,
}: TokenBannerProps) {
  const { start, authorizeUrl, mutation } = useConnectFyers()

  if (!bootstrap) {
    return null
  }

  if (variant === 'chip') {
    const connected = bootstrap.broker_connected && !bootstrap.needs_reauth
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              'inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
              connected
                ? 'border-border text-muted-foreground'
                : 'border-destructive/40 text-destructive',
              className,
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'size-1.5 rounded-full',
                connected ? 'bg-chart-5 dark:bg-chart-1' : 'bg-destructive',
              )}
            />
            {tokenLabel(bootstrap)}
          </span>
        </TooltipTrigger>
        <TooltipContent>
          <div className="flex flex-col gap-0.5">
            <span className="font-medium">Fyers access token</span>
            <span>State: {bootstrap.token_state}</span>
            {bootstrap.token_expires_at ? (
              <>
                <span>Expires {formatDateTime(bootstrap.token_expires_at)}</span>
                <span>Scheduled logout at 03:00 IST daily</span>
              </>
            ) : (
              <span>No token stored</span>
            )}
          </div>
        </TooltipContent>
      </Tooltip>
    )
  }

  // The banner is only for the state a user has to act on. A healthy token says nothing.
  if (!bootstrap.needs_reauth && bootstrap.token_state === 'active') {
    return null
  }

  const error = mutation.error instanceof ApiError ? mutation.error : null
  const missingCredentials = error?.code === 'no_credentials' || !bootstrap.has_credentials

  return (
    <div
      role="status"
      className={cn(
        'flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-sm',
        className,
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="font-medium">Fyers login required</p>
        <p className="text-xs text-muted-foreground">
          {missingCredentials
            ? 'No broker credentials are saved yet. Add the app id and secret in Settings, then connect.'
            : 'Downloads are parked until a login completes. Nothing is lost: parked work resumes on the next successful login.'}
          {bootstrap.token_expires_at && !missingCredentials
            ? ' Token expired ' + formatRelative(bootstrap.token_expires_at) + '.'
            : ''}
          {parkedJobs && parkedJobs > 0 ? ' Jobs waiting: ' + String(parkedJobs) + '.' : ''}
        </p>
        {error && !missingCredentials ? (
          <p className="mt-1 text-xs text-destructive">
            {error.message}
            {error.correlationId ? ' Reference ' + error.correlationId + '.' : ''}
          </p>
        ) : null}
        {authorizeUrl ? (
          <p className="mt-1 text-xs text-muted-foreground">
            If the Fyers tab did not open,{' '}
            <a
              className="underline underline-offset-4"
              href={authorizeUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              open the authorize page
            </a>
            .
          </p>
        ) : null}
      </div>

      {missingCredentials ? (
        <Button asChild size="sm" variant="outline">
          <Link to="/settings">Open Settings</Link>
        </Button>
      ) : (
        <Button size="sm" onClick={start} disabled={mutation.isPending}>
          {mutation.isPending ? 'Starting' : 'Log in to Fyers'}
        </Button>
      )}
    </div>
  )
}

export default TokenBanner
