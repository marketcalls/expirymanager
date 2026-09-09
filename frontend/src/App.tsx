import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'

import ChainRoute from '@/routes/chain'
import ChartRoute from '@/routes/chart'
import ContractsRoute from '@/routes/contracts'
import DashboardRoute from '@/routes/dashboard'
import ExpiriesRoute from '@/routes/expiries'
import ExportsRoute from '@/routes/exports'
import JobDetailRoute from '@/routes/job-detail'
import JobsRoute from '@/routes/jobs'
import LoginRoute from '@/routes/login'
import SchedulesRoute from '@/routes/schedules'
import SettingsRoute from '@/routes/settings'
import SetupRoute from '@/routes/setup'
import UnderlyingsRoute from '@/routes/underlyings'

// The route table is fixed here in Phase 0 so the Phase 4 agents fill screens in rather than
// each inventing a path. Paths are part of the contract: BootstrapGate redirects to /setup and
// /login by name, and the API client sends a 401 to /login.
export const NAV_ITEMS = [
  { to: '/', label: 'Dashboard' },
  { to: '/underlyings', label: 'Underlyings' },
  { to: '/expiries', label: 'Expiries' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/contracts', label: 'Contracts' },
  { to: '/chart', label: 'Chart' },
  { to: '/chain', label: 'Option chain' },
  { to: '/exports', label: 'Exports' },
  { to: '/schedules', label: 'Schedules' },
  { to: '/settings', label: 'Settings' },
] as const

// Placeholder shell. W22 replaces the body of this component with a re-export of
// AppShell from '@/components/layout/AppShell'. It is defined locally, not imported, because
// W22 has not run yet and an import of a missing module fails the build.
function AppShell() {
  const location = useLocation()
  return (
    <div className="flex min-h-screen">
      <nav className="w-56 shrink-0 border-r p-4">
        <p className="mb-4 text-sm font-semibold tracking-tight">ExpiryManager</p>
        <ul className="space-y-1">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <Link
                to={item.to}
                className={
                  'block rounded px-2 py-1 text-sm ' +
                  (location.pathname === item.to
                    ? 'bg-accent text-accent-foreground'
                    : 'text-muted-foreground hover:text-foreground')
                }
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}

// Placeholder gate. W22 replaces this with '@/components/layout/BootstrapGate', which reads
// /api/v1/bootstrap once and sends the user to /setup, /login or the app. Until then it is a
// pass-through so the scaffold renders without a backend.
function BootstrapGate({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

export default function App() {
  return (
    <BootstrapGate>
      <Routes>
        <Route path="/login" element={<LoginRoute />} />
        <Route path="/setup" element={<SetupRoute />} />
        <Route element={<AppShell />}>
          <Route index element={<DashboardRoute />} />
          <Route path="/underlyings" element={<UnderlyingsRoute />} />
          <Route path="/expiries" element={<ExpiriesRoute />} />
          <Route path="/jobs" element={<JobsRoute />} />
          <Route path="/jobs/:jobId" element={<JobDetailRoute />} />
          <Route path="/contracts" element={<ContractsRoute />} />
          <Route path="/chart" element={<ChartRoute />} />
          <Route path="/chain" element={<ChainRoute />} />
          <Route path="/exports" element={<ExportsRoute />} />
          <Route path="/schedules" element={<SchedulesRoute />} />
          <Route path="/settings" element={<SettingsRoute />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BootstrapGate>
  )
}
