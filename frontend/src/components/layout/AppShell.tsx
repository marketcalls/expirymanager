import { useEffect } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { cn } from 'cn'

import { BudgetGauge } from '@/components/common/BudgetGauge'
import { TokenBanner } from '@/components/common/TokenBanner'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { api, isTerminalApiError, onApiEvent } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, Budget } from '@/lib/api/types'
import { useEventStream } from '@/lib/events/useEventStream'

// The application chrome.
//
// The shell carries the two facts that can ruin a session and that no single screen owns: how
// much of the daily Fyers request budget is left, and whether the broker token is still good.
// Both are visible from every screen because both are discovered too late anywhere else. A user
// who finds out the budget is gone on the download screen has already planned the download.
//
// The sidebar is hand built rather than assembled from the shadcn sidebar kit. The kit is
// organised around an icon rail and a mobile sheet; this is a single user loopback desktop app
// with ten destinations and a house rule against icons as decoration, so the kit's surface would
// be carried for nothing. What is kept from the kit is its token vocabulary, so the two match.

export interface NavItem {
  to: string
  label: string
  /** Grouping heading. Items sharing a section render under one label. */
  section: string
  /** True when the path is matched exactly rather than as a prefix. Only the dashboard needs
   *  this, because every other path would otherwise be a child of "/". */
  exact?: boolean
}

/**
 * The navigation table, and the source of the breadcrumb labels.
 *
 * These paths are part of the contract with the rest of the app: BootstrapGate redirects to
 * /setup and /login by name, the OAuth callback lands on /settings, and the API client publishes
 * a 401 that this shell turns into a /login navigation.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: '/', label: 'Dashboard', section: 'Overview', exact: true },
  { to: '/underlyings', label: 'Underlyings', section: 'Catalogue' },
  { to: '/expiries', label: 'Expiries', section: 'Catalogue' },
  { to: '/contracts', label: 'Contracts', section: 'Catalogue' },
  { to: '/jobs', label: 'Jobs', section: 'Pipeline' },
  { to: '/schedules', label: 'Schedules', section: 'Pipeline' },
  { to: '/exports', label: 'Exports', section: 'Pipeline' },
  { to: '/chart', label: 'Chart', section: 'Analysis' },
  { to: '/chain', label: 'Option chain', section: 'Analysis' },
  { to: '/settings', label: 'Settings', section: 'System' },
]

/** Extra breadcrumb labels for paths that are not navigation destinations. */
const PATH_LABELS: Record<string, string> = {
  jobs: 'Jobs',
  setup: 'Setup',
  login: 'Login',
}

/** Polled only while the event stream is not delivering. The stream carries a budget frame on
 *  every governor tick, so polling on top of it would be pure duplication. */
const BUDGET_POLL_MS = 30_000

function navSections(): Array<{ section: string; items: NavItem[] }> {
  const order: string[] = []
  const grouped = new Map<string, NavItem[]>()
  for (const item of NAV_ITEMS) {
    if (!grouped.has(item.section)) {
      grouped.set(item.section, [])
      order.push(item.section)
    }
    grouped.get(item.section)?.push(item)
  }
  return order.map((section) => ({ section, items: grouped.get(section) ?? [] }))
}

function Sidebar() {
  return (
    <nav
      aria-label="Primary"
      className="flex w-52 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground"
    >
      <div className="flex h-12 shrink-0 items-center border-b px-4">
        <Link to="/" className="font-heading text-sm font-semibold tracking-tight">
          ExpiryManager
        </Link>
      </div>

      {/* Its own scroll region, so the nav never pushes the top bar off screen and the styled
          scrollbar from index.css applies instead of a platform one on a dark panel. */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-3">
        {navSections().map(({ section, items }) => (
          <div key={section} className="mb-3 last:mb-0">
            <p className="px-2 pb-1 text-[0.65rem] font-medium uppercase tracking-wider text-muted-foreground">
              {section}
            </p>
            <ul className="flex flex-col gap-0.5">
              {items.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.exact}
                    className={({ isActive }) =>
                      cn(
                        'block rounded-md px-2 py-1 text-sm transition-colors',
                        isActive
                          ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                          : 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground',
                      )
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  )
}

function ShellBreadcrumb() {
  const location = useLocation()
  const segments = location.pathname.split('/').filter(Boolean)

  if (segments.length === 0) {
    return (
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            <BreadcrumbPage>Dashboard</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>
    )
  }

  const crumbs = segments.map((segment, index) => {
    const to = '/' + segments.slice(0, index + 1).join('/')
    const known = NAV_ITEMS.find((item) => item.to === to)
    return {
      to,
      // An unrecognised segment is an identifier, a job id or an expiry date. It is shown as it
      // is rather than title cased, because a mangled identifier is worse than a raw one.
      label: known?.label ?? PATH_LABELS[segment] ?? segment,
      isLast: index === segments.length - 1,
    }
  })

  return (
    <Breadcrumb>
      <BreadcrumbList>
        <BreadcrumbItem>
          <BreadcrumbLink asChild>
            <Link to="/">Dashboard</Link>
          </BreadcrumbLink>
        </BreadcrumbItem>
        {crumbs.map((crumb) => (
          <BreadcrumbItem key={crumb.to}>
            <BreadcrumbSeparator />
            {crumb.isLast ? (
              <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
            ) : (
              <BreadcrumbLink asChild>
                <Link to={crumb.to}>{crumb.label}</Link>
              </BreadcrumbLink>
            )}
          </BreadcrumbItem>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

function StreamIndicator({ status }: { status: string }) {
  const live = status === 'open'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          <span
            aria-hidden="true"
            className={cn('size-1.5 rounded-full', live ? 'bg-chart-5 dark:bg-chart-1' : 'bg-muted-foreground/50')}
          />
          {live ? 'Live' : 'Polling'}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {live
          ? 'Server sent events are connected. Progress updates arrive as they happen.'
          : 'The event stream is not connected, so the screens refresh on a timer. Every number shown still comes from the REST API and is correct, just slower to change.'}
      </TooltipContent>
    </Tooltip>
  )
}

export function AppShell() {
  const navigate = useNavigate()

  // One stream for the whole app, mounted here because this is the one component that is alive
  // for every authenticated screen and exactly once.
  const stream = useEventStream()

  const bootstrap = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: () => api.get<Bootstrap>('/bootstrap'),
    // BootstrapGate has already put this in the cache. The shell reads the same entry rather
    // than threading it down, so an auth_required frame patching the cache updates both.
    staleTime: 60_000,
  })

  const budget = useQuery({
    queryKey: queryKeys.system.budget(),
    queryFn: () => api.get<Budget>('/system/budget'),
    refetchInterval: stream.status === 'open' ? false : BUDGET_POLL_MS,
    retry: (failureCount, error) => !isTerminalApiError(error) && failureCount < 2,
  })

  // The client publishes a 401 rather than navigating, so that it stays testable and router
  // free. Turning it into a route change is the shell's job.
  useEffect(() => {
    return onApiEvent('unauthenticated', () => {
      navigate('/login', { replace: true })
    })
  }, [navigate])

  return (
    // Mounted here and nowhere else in the app tree. Radix tooltips are inert without it, which
    // is a silent failure: the trigger renders, the content simply never appears.
    <TooltipProvider delayDuration={200}>
      <div className="flex h-screen min-h-0 w-full overflow-hidden bg-background text-foreground">
        <Sidebar />

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <header className="flex h-12 shrink-0 items-center gap-4 border-b px-4">
            <div className="min-w-0 flex-1 truncate">
              <ShellBreadcrumb />
            </div>
            <StreamIndicator status={stream.status} />
            <TokenBanner bootstrap={bootstrap.data} variant="chip" />
            <BudgetGauge budget={budget.data} variant="bar" />
          </header>

          <TokenBanner bootstrap={bootstrap.data} variant="banner" />

          {/* The single scroll container for screen content. Screens size themselves to it and
              never add a second vertical scrollbar of their own. */}
          <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            <Outlet />
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}

export default AppShell
