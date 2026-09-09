import { Navigate, Route, Routes } from 'react-router-dom'

import AppShell, { NAV_ITEMS as SHELL_NAV_ITEMS } from '@/components/layout/AppShell'
import BootstrapGate from '@/components/layout/BootstrapGate'
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

// The route table is fixed here so screens fill in rather than each inventing a path. Paths are
// part of the contract: BootstrapGate redirects to /setup and /login by name, and the API client
// publishes a 401 that AppShell turns into a /login navigation.
//
// The navigation entries themselves live with the sidebar that renders them, in AppShell, and
// are re-exported here because that is where the rest of the tree has always read them from.
export const NAV_ITEMS = SHELL_NAV_ITEMS

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
