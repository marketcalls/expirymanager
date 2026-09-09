// One EventSource for the whole application, patching the TanStack Query cache.
//
// The stream is a refresh accelerator and never the source of truth. Every frame it delivers has
// a REST equivalent, and every screen keeps its own query. So the failure mode of a dead stream
// is a slower refresh, never a wrong number on screen, and nothing here throws upward or renders
// an error state. That is why the hook returns a status instead of a value: the status is worth
// showing as a small connected indicator, and nothing else depends on it.
//
// Resume: the browser sends the Last-Event-ID header by itself on its own automatic reconnects,
// which is the path taken for a brief drop. When the browser gives up and closes the stream, the
// hook builds a new EventSource and carries the last id forward as a query parameter, because a
// header cannot be set on an EventSource constructor.

import { useEffect, useRef, useState } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { useQueryClient } from '@tanstack/react-query'

import { queryKeys } from '@/lib/api/keys'
import type {
  AuthRequiredFrame,
  Bootstrap,
  Budget,
  EventFrame,
  EventName,
  Job,
  JobLifecycleFrame,
  JobProgressFrame,
  Paged,
  PipelineModeFrame,
  RateLimitedFrame,
} from '@/lib/api/types'
import { EVENT_NAMES } from '@/lib/api/types'

const STREAM_PATH = '/api/v1/events/stream'

/** Frames are applied in batches on this cadence. A full speed download emits several task
 *  frames a second per worker, and applying each one straight to the cache would rerender every
 *  subscribed screen at that rate for no extra information. */
const FLUSH_INTERVAL_MS = 250

/** Backoff for reconnects the hook has to drive itself, after the browser has closed the
 *  stream and stopped retrying on its own. */
const RECONNECT_MIN_MS = 1_000
const RECONNECT_MAX_MS = 30_000

export type EventStreamStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'unavailable'

// ---------------------------------------------------------------------------
// Cache patching
// ---------------------------------------------------------------------------

function patchJobFields(
  client: QueryClient,
  jobId: string,
  fields: Partial<Job>,
): void {
  // The updater returning undefined tells TanStack Query to leave the entry alone. That is what
  // keeps a frame for a job nobody is looking at from materialising a half-populated job object
  // in the cache, which a later render would then trust.
  client.setQueryData<Job>(queryKeys.jobs.detail(jobId), (previous) =>
    previous ? { ...previous, ...fields } : undefined,
  )

  client.setQueriesData<Paged<Job>>(
    { queryKey: [...queryKeys.jobs.all(), 'list'] },
    (previous) => {
      if (!previous?.items.some((job) => job.job_id === jobId)) {
        return undefined
      }
      return {
        ...previous,
        items: previous.items.map((job) =>
          job.job_id === jobId ? { ...job, ...fields } : job,
        ),
      }
    },
  )
}

function fromProgress(data: JobProgressFrame): Partial<Job> {
  // The frame uses the short counter names the pipeline emits. The REST job row uses the long
  // ones. The mapping is here so a screen never has to know there are two vocabularies.
  return {
    status: data.status,
    total_tasks: data.total,
    done_tasks: data.done,
    empty_tasks: data.empty,
    failed_tasks: data.failed,
    skipped_tasks: data.skipped,
    requests_used: data.requests_used,
    rows_written: data.rows_written,
    eta_seconds: data.eta_seconds,
  }
}

function fromLifecycle(data: JobLifecycleFrame): Partial<Job> {
  return { status: data.status, reason: data.reason }
}

function patchBudget(client: QueryClient, fields: Partial<Budget>): void {
  client.setQueryData<Budget>(queryKeys.system.budget(), (previous) =>
    previous ? { ...previous, ...fields } : undefined,
  )
}

function applyAuthRequired(client: QueryClient, data: AuthRequiredFrame): void {
  // The banner reads bootstrap, so the parked state has to land there and not only in a local
  // state somewhere. A scheduled logout at 03:00 IST arrives as exactly this frame.
  client.setQueryData<Bootstrap>(queryKeys.bootstrap(), (previous) =>
    previous
      ? {
          ...previous,
          needs_reauth: true,
          broker_connected: false,
          token_state: data.token_state,
        }
      : undefined,
  )
  void client.invalidateQueries({ queryKey: queryKeys.broker.fyers() })
  void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
}

function applyRateLimited(client: QueryClient, data: RateLimitedFrame): void {
  patchBudget(client, {
    strikes_remaining: data.strikes_remaining,
    blocked_until: data.blocked_until,
  })
}

function applyPipelineMode(client: QueryClient, data: PipelineModeFrame): void {
  patchBudget(client, { pipeline_mode: data.mode, pipeline_reason: data.reason })
}

/**
 * Writes one frame into the query cache.
 *
 * Exported and free of React so it can be tested against a real QueryClient with no rendering,
 * and so a future worker or a replay tool can drive the same patching.
 */
export function applyEventFrame(client: QueryClient, frame: EventFrame): void {
  switch (frame.event) {
    case 'job_progress':
      patchJobFields(client, frame.data.job_id, fromProgress(frame.data))
      return

    case 'job_started':
    case 'job_blocked':
      patchJobFields(client, frame.data.job_id, fromLifecycle(frame.data))
      // A job that has only just started may not be in any cached list yet, so patching alone
      // would leave the jobs screen empty until its own refetch.
      void client.invalidateQueries({ queryKey: [...queryKeys.jobs.all(), 'list'] })
      return

    case 'job_finished':
      patchJobFields(client, frame.data.job_id, fromLifecycle(frame.data))
      // A finished job is the moment the catalogue and the coverage rollups change, and it is
      // the only moment they do. This invalidation is what lets every other query hold a
      // comfortable stale window.
      void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
      void client.invalidateQueries({ queryKey: queryKeys.underlyings.all() })
      void client.invalidateQueries({ queryKey: queryKeys.expiries.all() })
      void client.invalidateQueries({ queryKey: queryKeys.contracts.all() })
      void client.invalidateQueries({ queryKey: queryKeys.coverage.all() })
      void client.invalidateQueries({ queryKey: queryKeys.system.storage() })
      return

    case 'task_completed':
      void client.invalidateQueries({
        queryKey: [...queryKeys.jobs.detail(frame.data.job_id), 'tasks'],
      })
      return

    case 'budget':
      // The frame body is the /system/budget body, so it replaces the cache entry outright
      // rather than merging into it.
      client.setQueryData<Budget>(queryKeys.system.budget(), frame.data)
      return

    case 'auth_required':
      applyAuthRequired(client, frame.data)
      return

    case 'rate_limited':
      applyRateLimited(client, frame.data)
      return

    case 'pipeline_mode':
      applyPipelineMode(client, frame.data)
      return

    case 'schedule_fired':
      void client.invalidateQueries({ queryKey: queryKeys.schedules.all() })
      void client.invalidateQueries({ queryKey: [...queryKeys.jobs.all(), 'list'] })
      return

    case 'export_ready':
      void client.invalidateQueries({ queryKey: queryKeys.exports.all() })
      return

    case 'notification':
      // Notification queries are keyed by their unread_only filter, so there is no single entry
      // to prepend to.
      void client.invalidateQueries({ queryKey: queryKeys.system.all() })
      return
  }
}

// ---------------------------------------------------------------------------
// Coalescing
// ---------------------------------------------------------------------------

/** Frames of the same kind about the same subject supersede each other. Only the newest carries
 *  information: an older progress frame for the same job is strictly stale. */
function coalesceKey(frame: EventFrame): string {
  switch (frame.event) {
    case 'job_progress':
    case 'job_started':
    case 'job_finished':
    case 'job_blocked':
    case 'task_completed':
      return frame.event + ':' + frame.data.job_id
    case 'notification':
      return frame.event + ':' + frame.data.notification_id
    case 'export_ready':
      return frame.event + ':' + frame.data.export_id
    case 'schedule_fired':
      return frame.event + ':' + frame.data.schedule_id
    default:
      return frame.event
  }
}

export interface FrameCoalescer {
  push(frame: EventFrame): void
  flush(): void
  dispose(): void
}

/**
 * Buffers frames and applies them on a fixed cadence, newest wins per subject.
 *
 * Exported for tests. `schedule` and `cancel` are injected so a test can drive the clock
 * without waiting on a real timer.
 */
export function createFrameCoalescer(
  apply: (frame: EventFrame) => void,
  options: {
    intervalMs?: number
    schedule?: (callback: () => void, delayMs: number) => number
    cancel?: (handle: number) => void
  } = {},
): FrameCoalescer {
  const intervalMs = options.intervalMs ?? FLUSH_INTERVAL_MS
  const schedule = options.schedule ?? ((callback, delay) => window.setTimeout(callback, delay))
  const cancel = options.cancel ?? ((handle: number) => window.clearTimeout(handle))

  // Insertion order is preserved by Map, and re-setting an existing key keeps its original
  // position, so a burst still applies in the order the jobs first appeared.
  const pending = new Map<string, EventFrame>()
  let timer: number | null = null

  function flush(): void {
    if (timer !== null) {
      cancel(timer)
      timer = null
    }
    if (pending.size === 0) {
      return
    }
    const frames = [...pending.values()]
    pending.clear()
    for (const frame of frames) {
      apply(frame)
    }
  }

  return {
    push(frame) {
      pending.set(coalesceKey(frame), frame)
      if (timer === null) {
        timer = schedule(flush, intervalMs)
      }
    },
    flush,
    dispose() {
      if (timer !== null) {
        cancel(timer)
        timer = null
      }
      pending.clear()
    },
  }
}

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

export interface UseEventStreamOptions {
  /** Held off until there is a session. Opening the stream while logged out earns a 401 the
   *  browser cannot retry, and the reconnect loop would then hammer it. */
  enabled?: boolean
}

export interface EventStreamState {
  status: EventStreamStatus
  lastEventId: string | null
}

export function useEventStream(options: UseEventStreamOptions = {}): EventStreamState {
  const enabled = options.enabled ?? true
  // Not a supported browser, or a test environment with no EventSource. REST is still
  // authoritative, so this is a degraded refresh rate and nothing more.
  const supported = typeof EventSource !== 'undefined'
  const client = useQueryClient()
  const [connection, setConnection] = useState<EventStreamStatus>(() =>
    enabled && supported ? 'connecting' : 'idle',
  )
  const lastEventIdRef = useRef<string | null>(null)
  const [lastEventId, setLastEventId] = useState<string | null>(null)

  useEffect(() => {
    if (!enabled || !supported) {
      return
    }

    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: number | null = null
    let reconnectDelay = RECONNECT_MIN_MS
    let hasConnectedOnce = false

    const coalescer = createFrameCoalescer((frame) => applyEventFrame(client, frame))

    function handleFrame(name: EventName, event: MessageEvent<string>): void {
      if (event.lastEventId) {
        lastEventIdRef.current = event.lastEventId
        setLastEventId(event.lastEventId)
      }
      let data: unknown
      try {
        data = JSON.parse(event.data)
      } catch {
        // A frame this client cannot read is dropped. The REST equivalent still holds the
        // truth, so there is nothing to recover and nothing to report.
        return
      }
      coalescer.push({ event: name, data } as EventFrame)
    }

    function scheduleReconnect(): void {
      if (disposed || reconnectTimer !== null) {
        return
      }
      setConnection('reconnecting')
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS)
        connect()
      }, reconnectDelay)
    }

    function connect(): void {
      if (disposed) {
        return
      }

      // The header form is what the browser sends on its own retries. This parameter covers the
      // case the browser will not: a stream it closed for good, reopened here as a new object.
      const resumeFrom = lastEventIdRef.current
      const url = resumeFrom
        ? STREAM_PATH + '?last_event_id=' + encodeURIComponent(resumeFrom)
        : STREAM_PATH

      const opened = new EventSource(url, { withCredentials: true })
      source = opened

      opened.onopen = () => {
        if (disposed) {
          return
        }
        reconnectDelay = RECONNECT_MIN_MS
        setConnection('open')
        if (hasConnectedOnce) {
          // Frames emitted during the gap may be past the replay buffer. Resyncing the two live
          // facts costs two cheap requests and removes the only window in which the top bar
          // could sit on a number from before the drop.
          void client.invalidateQueries({ queryKey: queryKeys.system.budget() })
          void client.invalidateQueries({ queryKey: queryKeys.jobs.all() })
        }
        hasConnectedOnce = true
      }

      opened.onerror = () => {
        if (disposed) {
          return
        }
        if (opened.readyState === EventSource.CLOSED) {
          // The browser has given up. Everything from here is driven by the backoff above.
          opened.close()
          if (source === opened) {
            source = null
          }
          scheduleReconnect()
          return
        }
        // readyState CONNECTING means the browser is retrying by itself and will send the
        // Last-Event-ID header when it does. Leave it alone.
        setConnection('reconnecting')
      }

      for (const name of EVENT_NAMES) {
        opened.addEventListener(name, (event) => {
          handleFrame(name, event as MessageEvent<string>)
        })
      }
    }

    connect()

    return () => {
      disposed = true
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer)
      }
      coalescer.dispose()
      source?.close()
      source = null
    }
    // Every setConnection above runs from a socket callback or a timer, never synchronously
    // from this effect body, so the first paint uses the initial value rather than a second
    // render.
  }, [client, enabled, supported])

  const status: EventStreamStatus = !enabled ? 'idle' : !supported ? 'unavailable' : connection

  return { status, lastEventId }
}
