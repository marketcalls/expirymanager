import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { StatusRow, apiErrorMessage, isRouteMissing } from '@/components/settings/BrokerPanel'
import { EmptyState } from '@/components/common/EmptyState'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { api } from '@/lib/api/client'
import { queryKeys } from '@/lib/api/keys'
import type { CheckpointResponse, Storage } from '@/lib/api/types'
import { formatBytes, formatCompact, formatInteger } from '@/lib/format'

// What the store costs on disk, and the three maintenance actions that change it.
//
// Every action here is confirmed before it runs, because all three quiesce the writer and two of
// them move the whole DuckDB file. The compaction figure is shown as a ratio rather than as a
// verdict, so the suggestion the backend makes can be read against the number it made it from.

export function StoragePanel() {
  const client = useQueryClient()

  const storage = useQuery({
    queryKey: queryKeys.system.storage(),
    queryFn: () => api.get<Storage>('/system/storage'),
    retry: false,
  })

  const refresh = () => {
    void client.invalidateQueries({ queryKey: queryKeys.system.storage() })
  }

  const checkpoint = useMutation({
    mutationFn: () => api.post<CheckpointResponse>('/system/checkpoint'),
    onSuccess: (result) => {
      toast.success(
        'Checkpoint complete. Write ahead log ' +
          formatBytes(result.wal_bytes_before) +
          ' down to ' +
          formatBytes(result.wal_bytes_after) +
          '.',
      )
      refresh()
    },
  })

  const optimise = useMutation({
    mutationFn: () => api.post<{ job_id: string }>('/system/optimise', { body: { confirm: true } }),
    onSuccess: () => {
      toast.success('Compaction started. Watch it on the Jobs screen.')
      refresh()
    },
  })

  const backup = useMutation({
    mutationFn: () => api.post<unknown>('/system/backup', { body: { target_dir: null } }),
    onSuccess: () => {
      toast.success('Backup started. It checkpoints first, then copies both databases together.')
    },
  })

  if (storage.isError) {
    return (
      <EmptyState
        title={isRouteMissing(storage.error) ? 'Storage figures are not wired up yet' : 'Cannot read the storage figures'}
        description={
          isRouteMissing(storage.error)
            ? 'This build does not serve the storage route yet. Nothing is wrong with your installation, and the login and download paths do not depend on it.'
            : (apiErrorMessage(storage.error) ?? 'The backend returned an unexpected response.')
        }
        action={
          <Button size="sm" variant="outline" onClick={() => void storage.refetch()}>
            Retry
          </Button>
        }
      />
    )
  }

  const data = storage.data
  const actionError =
    apiErrorMessage(checkpoint.error) ??
    apiErrorMessage(optimise.error) ??
    apiErrorMessage(backup.error)

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>On disk</CardTitle>
          <CardDescription>
            Everything lives in one data directory. Nothing outside it is ever written.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col">
          {storage.isPending ? (
            <p className="text-sm text-muted-foreground">Measuring.</p>
          ) : (
            <div className="divide-y">
              <StatusRow label="Candle store">{formatBytes(data?.duckdb_bytes)}</StatusRow>
              <StatusRow label="Write ahead log">{formatBytes(data?.duckdb_wal_bytes)}</StatusRow>
              <StatusRow label="Catalogue and jobs">{formatBytes(data?.sqlite_bytes)}</StatusRow>
              <StatusRow label="Exports">{formatBytes(data?.exports_bytes)}</StatusRow>
              <StatusRow label="Archived responses">
                {formatBytes(data?.raw_payload_bytes)}
              </StatusRow>
              <StatusRow label="Candle rows">
                {formatInteger(data?.candle_rows)} ({formatCompact(data?.candle_rows)})
              </StatusRow>
              <StatusRow label="Bytes per row">
                {data ? data.bytes_per_row.toFixed(2) : 'unknown'}
              </StatusRow>
              <StatusRow label="Bloat ratio">
                {data ? data.bloat_ratio.toFixed(4) : 'unknown'}
              </StatusRow>
              <StatusRow label="Free disk">{formatBytes(data?.free_disk_bytes)}</StatusRow>
            </div>
          )}
          {data?.compaction_suggested ? (
            <p className="pt-3 text-sm">
              The file is measurably larger than the rows in it. Compaction would reclaim the
              difference. It needs free space equal to the current file plus 20 percent.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Maintenance</CardTitle>
          <CardDescription>
            All three quiesce the writer first, so a download in flight pauses for the duration
            and resumes afterwards.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {actionError ? (
            <p role="alert" className="text-sm text-destructive">
              {actionError}
            </p>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => checkpoint.mutate()}
              disabled={checkpoint.isPending}
            >
              {checkpoint.isPending ? 'Checkpointing' : 'Checkpoint now'}
            </Button>

            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="outline" disabled={optimise.isPending}>
                  Compact the store
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Compact the candle store</AlertDialogTitle>
                  <AlertDialogDescription>
                    This rewrites the whole file in sorted order and swaps it in. It needs free
                    space equal to the current file plus 20 percent, and it runs as a job you can
                    watch. Downloads pause while it runs and resume afterwards.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={() => optimise.mutate()}>
                    Start compaction
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>

            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="outline" disabled={backup.isPending}>
                  Back up now
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Back up both databases</AlertDialogTitle>
                  <AlertDialogDescription>
                    Checkpoints first, then copies the candle store, its write ahead log, the
                    catalogue and its sidecars together. A copy taken without the log restores a
                    database missing the most recent writes.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={() => backup.mutate()}>
                    Start the backup
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

export default StoragePanel
