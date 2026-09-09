import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { StatusRow, apiErrorMessage, isRouteMissing } from '@/components/settings/BrokerPanel'
import { BudgetGauge } from '@/components/common/BudgetGauge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { Bootstrap, Budget, Health, RequestLogRow } from '@/lib/api/types'
import { formatDateTime, formatLatency } from '@/lib/format'

// What to read out when something is wrong, in the order it is usually needed: the versions, the
// budget, the health checks, then the last outbound requests.
//
// The request log is the one that answers "why did that download stop", because it carries the
// Fyers error code per request. Parameters arrive already scrubbed from the backend.

const REQUEST_LIMIT = 50

/** The documented shape is a list of rows. A body wrapped in the usual paging envelope is
 *  accepted too rather than rendering an empty table over a shape difference. */
function asRows<T>(body: unknown): T[] {
  if (Array.isArray(body)) {
    return body as T[]
  }
  if (body && typeof body === 'object' && Array.isArray((body as { items?: unknown }).items)) {
    return (body as { items: T[] }).items
  }
  return []
}

function Unavailable({
  error,
  what,
  onRetry,
}: {
  error: unknown
  what: string
  onRetry: () => void
}) {
  return (
    <div className="flex flex-col items-start gap-2">
      <p className="text-sm text-muted-foreground">
        {isRouteMissing(error)
          ? 'This build does not serve the ' +
            what +
            ' route yet. Nothing is wrong with your installation.'
          : (apiErrorMessage(error) ?? 'The backend returned an unexpected response.')}
      </p>
      <Button size="sm" variant="outline" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

export interface DiagnosticsPanelProps {
  bootstrap: Bootstrap | undefined
}

export function DiagnosticsPanel({ bootstrap }: DiagnosticsPanelProps) {
  const budget = useQuery({
    queryKey: queryKeys.system.budget(),
    queryFn: () => api.get<Budget>('/system/budget'),
    retry: false,
  })

  const health = useQuery({
    queryKey: queryKeys.system.health(),
    queryFn: () => api.get<Health>('/system/health'),
    retry: false,
  })

  const requests = useQuery({
    queryKey: queryKeys.system.requests({ limit: REQUEST_LIMIT }),
    queryFn: () => api.get<unknown>('/system/requests', { query: { limit: REQUEST_LIMIT } }),
    retry: false,
  })

  const rows = useMemo(() => asRows<RequestLogRow>(requests.data), [requests.data])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Build</CardTitle>
          <CardDescription>Quote these in a bug report.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="divide-y">
            <StatusRow label="App version">{bootstrap?.app_version ?? 'unknown'}</StatusRow>
            <StatusRow label="DuckDB version">{bootstrap?.duckdb_version ?? 'unknown'}</StatusRow>
            <StatusRow label="Data directory">
              <span className="font-mono text-xs">{bootstrap?.data_dir ?? 'unknown'}</span>
            </StatusRow>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Request budget</CardTitle>
          <CardDescription>
            The Fyers daily allowance, and how close the governor is to a strike.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {budget.isError ? (
            <Unavailable error={budget.error} what="budget" onRetry={() => void budget.refetch()} />
          ) : budget.isPending ? (
            <p className="text-sm text-muted-foreground">Reading the budget.</p>
          ) : (
            <BudgetGauge budget={budget.data} variant="panel" />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Health checks</CardTitle>
          <CardDescription>
            What the last maintenance pass found in the store.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {health.isError ? (
            <Unavailable error={health.error} what="health" onRetry={() => void health.refetch()} />
          ) : health.isPending ? (
            <p className="text-sm text-muted-foreground">Reading the health checks.</p>
          ) : (health.data?.rows.length ?? 0) === 0 ? (
            <p className="text-sm text-muted-foreground">No checks have run yet.</p>
          ) : (
            <div className="max-h-80 overflow-y-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Check</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Detail</TableHead>
                    <TableHead>Observed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {health.data?.rows.map((row) => (
                    <TableRow key={row.check_name}>
                      <TableCell className="font-mono text-xs">{row.check_name}</TableCell>
                      <TableCell>{row.status}</TableCell>
                      <TableCell className="text-muted-foreground">{row.detail ?? ''}</TableCell>
                      <TableCell className="tabular-nums">
                        {formatDateTime(row.observed_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {health.data?.last_maintenance ? (
            <p className="text-xs text-muted-foreground">
              Last maintenance {formatDateTime(health.data.last_maintenance.ran_at)}:{' '}
              {health.data.last_maintenance.outcome}
              {health.data.last_maintenance.detail
                ? '. ' + health.data.last_maintenance.detail
                : ''}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent outbound requests</CardTitle>
          <CardDescription>
            The last {REQUEST_LIMIT} calls to Fyers, newest first, with the broker error code
            where there was one. Parameters are scrubbed by the backend.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {requests.isError ? (
            <Unavailable
              error={requests.error}
              what="request log"
              onRetry={() => void requests.refetch()}
            />
          ) : requests.isPending ? (
            <p className="text-sm text-muted-foreground">Reading the request log.</p>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No requests have been sent yet. That is expected before the first download.
            </p>
          ) : (
            <div className="max-h-96 overflow-y-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>When</TableHead>
                    <TableHead>Endpoint</TableHead>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead className="text-right">Status</TableHead>
                    <TableHead className="text-right">Latency</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.task_id}>
                      <TableCell className="tabular-nums">
                        {formatDateTime(row.requested_at)}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{row.endpoint}</TableCell>
                      <TableCell className="font-mono text-xs">
                        {row.fyers_symbol ?? ''}
                      </TableCell>
                      <TableCell>
                        {row.outcome}
                        {row.error_code ? ' (' + row.error_code + ')' : ''}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.http_status ?? ''}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatLatency(row.latency_ms)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export default DiagnosticsPanel
