import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError, api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, CurrentUser } from '@/lib/api/types'

// The one decision made before any screen renders: setup, login, or the app.
//
// GET /api/v1/bootstrap is public and answers whether the instance has been provisioned at all.
// It deliberately says nothing about whether THIS browser has a session, because it is reachable
// without one. So provisioned instances get one further probe, GET /api/v1/auth/me, whose 401 is
// the only honest answer to "am I logged in". Two requests, once, at startup.
//
// Everything here degrades toward showing the app rather than locking the user out. A backend
// that cannot be reached gets a plain retry screen, and a session probe that fails for any
// reason other than a 401 renders the app, because a transient failure must not become a login
// loop. The screens themselves are authoritative and will surface a real 401 through the API
// client, which the shell turns into a /login navigation.

/** Paths that render outside the application shell and have their own entry conditions. */
const SETUP_PATH = '/setup'
const LOGIN_PATH = '/login'

function GateScreen({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 text-foreground">
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <p className="font-heading text-sm font-semibold tracking-tight">{title}</p>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
        {action}
      </div>
    </div>
  )
}

export interface BootstrapGateProps {
  children: ReactNode
}

export function BootstrapGate({ children }: BootstrapGateProps) {
  const location = useLocation()

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    // Read once. Provisioning state changes exactly once in the life of an install, and the
    // setup wizard invalidates this key itself when it does.
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const provisioned = Boolean(bootstrap.data?.provisioned && bootstrap.data.has_user)

  const session = useQuery({
    queryKey: queryKeys.auth.me(),
    queryFn: () => api.get<CurrentUser>('/auth/me'),
    enabled: provisioned,
    // A 401 here is the answer, not a failure to retry.
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  })

  const content = (
    // Mounted here as well as in AppShell so that /setup and /login, which render outside the
    // shell, have working tooltips. Nesting providers is supported and the inner one wins.
    <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
  )

  if (bootstrap.isPending) {
    return <GateScreen title="Starting ExpiryManager" />
  }

  if (bootstrap.isError) {
    const error = bootstrap.error instanceof ApiError ? bootstrap.error : null
    return (
      <GateScreen
        title="Cannot reach the backend"
        description={
          error?.isNetworkError
            ? 'The ExpiryManager server is not answering on http://127.0.0.1:8000. Start it with "uv run expirymanager", then retry.'
            : (error?.message ?? 'The backend returned an unexpected response.')
        }
        action={
          <Button size="sm" variant="outline" onClick={() => void bootstrap.refetch()}>
            Retry
          </Button>
        }
      />
    )
  }

  const path = location.pathname

  if (path === SETUP_PATH) {
    // Re-running the wizard on a provisioned instance would only earn a 409 already_provisioned.
    return provisioned ? <Navigate to="/" replace /> : content
  }

  if (!provisioned) {
    return <Navigate to={SETUP_PATH} replace />
  }

  const unauthenticated = session.error instanceof ApiError && session.error.isUnauthenticated

  if (path === LOGIN_PATH) {
    return session.isSuccess ? <Navigate to="/" replace /> : content
  }

  if (session.isPending) {
    return <GateScreen title="Checking session" />
  }

  if (unauthenticated) {
    return <Navigate to={LOGIN_PATH} replace />
  }

  return content
}

export default BootstrapGate
