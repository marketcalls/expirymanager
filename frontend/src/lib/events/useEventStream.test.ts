// The stream's only job is to patch the query cache, so that is what is tested: the patching
// function against a real QueryClient, with no rendering and no EventSource.
//
// The patching is the part that fails silently. A key that does not match, or a counter mapped
// to the wrong field, produces a screen that looks alive and shows the wrong number, which is
// exactly the failure the architecture forbids.

import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '@/lib/api/keys'
import type { Budget, EventFrame, Job, Paged } from '@/lib/api/types'
import { applyEventFrame, createFrameCoalescer } from '@/lib/events/useEventStream'

let client: QueryClient

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
})

function jobRow(overrides: Partial<Job> = {}): Job {
  return {
    job_id: 'job-1',
    kind: 'candle_download',
    status: 'running',
    created_at: '2026-09-09T10:00:00+05:30',
    started_at: '2026-09-09T10:00:01+05:30',
    finished_at: null,
    total_tasks: 100,
    done_tasks: 0,
    empty_tasks: 0,
    failed_tasks: 0,
    skipped_tasks: 0,
    requests_used: 0,
    rows_written: 0,
    throughput_per_minute: null,
    eta_seconds: null,
    reason: null,
    parent_job_id: null,
    schedule_id: null,
    params: null,
    ...overrides,
  }
}

function budgetRow(overrides: Partial<Budget> = {}): Budget {
  return {
    ist_date: '2026-09-09',
    plan: 'standard',
    requests_used: 12_400,
    plan_limit_day: 100_000,
    remaining: 87_600,
    minute_headroom: 170,
    minute_violations: 0,
    strikes_remaining: 3,
    blocked_until: null,
    pipeline_mode: 'running',
    pipeline_reason: null,
    sweep_reserve_fraction: 0.7,
    ...overrides,
  }
}

const progressFrame: EventFrame = {
  event: 'job_progress',
  data: {
    job_id: 'job-1',
    status: 'running',
    total: 100,
    done: 42,
    empty: 3,
    failed: 1,
    skipped: 2,
    requests_used: 48,
    rows_written: 91_000,
    eta_seconds: 320,
  },
}

describe('job frames', () => {
  it('maps the short frame counters onto the long job fields', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), jobRow())

    applyEventFrame(client, progressFrame)

    const job = client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))
    expect(job?.done_tasks).toBe(42)
    expect(job?.empty_tasks).toBe(3)
    expect(job?.failed_tasks).toBe(1)
    expect(job?.skipped_tasks).toBe(2)
    expect(job?.requests_used).toBe(48)
    expect(job?.rows_written).toBe(91_000)
    expect(job?.eta_seconds).toBe(320)
    // Fields the frame does not carry survive the patch.
    expect(job?.kind).toBe('candle_download')
    expect(job?.created_at).toBe('2026-09-09T10:00:00+05:30')
  })

  it('does not create a cache entry for a job nobody has loaded', () => {
    applyEventFrame(client, progressFrame)

    // A half populated job object in the cache would be trusted by the next render.
    expect(client.getQueryData(queryKeys.jobs.detail('job-1'))).toBeUndefined()
  })

  it('patches the same job inside a cached list', () => {
    const listKey = queryKeys.jobs.list({ status: 'running' })
    client.setQueryData<Paged<Job>>(listKey, {
      items: [jobRow(), jobRow({ job_id: 'job-2', done_tasks: 7 })],
      next_cursor: null,
    })

    applyEventFrame(client, progressFrame)

    const page = client.getQueryData<Paged<Job>>(listKey)
    expect(page?.items[0].done_tasks).toBe(42)
    // The other job in the same page is untouched.
    expect(page?.items[1].done_tasks).toBe(7)
  })

  it('leaves a list that does not contain the job alone', () => {
    const listKey = queryKeys.jobs.list({ status: 'completed' })
    const page: Paged<Job> = { items: [jobRow({ job_id: 'job-9' })], next_cursor: null }
    client.setQueryData(listKey, page)

    applyEventFrame(client, progressFrame)

    // Identity, not just equality: an untouched page must not force a rerender.
    expect(client.getQueryData(listKey)).toBe(page)
  })

  it('marks the catalogue stale when a job finishes, because that is the only moment it changes', () => {
    client.setQueryData(queryKeys.jobs.detail('job-1'), jobRow())
    client.setQueryData(queryKeys.underlyings.list(), [])
    client.setQueryData(queryKeys.coverage.grid({ underlying_id: 1 }), { cells: [] })
    client.setQueryData(queryKeys.contracts.list({ underlying_id: 1 }), { items: [] })

    applyEventFrame(client, {
      event: 'job_finished',
      data: { job_id: 'job-1', status: 'completed', reason: null },
    })

    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.status).toBe('completed')
    expect(client.getQueryState(queryKeys.underlyings.list())?.isInvalidated).toBe(true)
    expect(
      client.getQueryState(queryKeys.coverage.grid({ underlying_id: 1 }))?.isInvalidated,
    ).toBe(true)
    expect(
      client.getQueryState(queryKeys.contracts.list({ underlying_id: 1 }))?.isInvalidated,
    ).toBe(true)
  })

  it('invalidates only the task lists of the job the task belongs to', () => {
    const mine = queryKeys.jobs.tasks('job-1', { state: 'failed' })
    const other = queryKeys.jobs.tasks('job-2', { state: 'failed' })
    client.setQueryData(mine, { items: [] })
    client.setQueryData(other, { items: [] })

    applyEventFrame(client, {
      event: 'task_completed',
      data: {
        job_id: 'job-1',
        task_id: 'task-1',
        kind: 'candle_download',
        state: 'ok',
        fyers_symbol: 'NSE:NIFTY25MAR23000CE',
        row_count: 375,
        latency_ms: 214,
      },
    })

    expect(client.getQueryState(mine)?.isInvalidated).toBe(true)
    expect(client.getQueryState(other)?.isInvalidated).toBe(false)
  })
})

describe('budget frames', () => {
  it('replaces the budget entry outright, since the frame is the rest body', () => {
    client.setQueryData(queryKeys.system.budget(), budgetRow())
    const fresh = budgetRow({ requests_used: 20_000, remaining: 80_000 })

    applyEventFrame(client, { event: 'budget', data: fresh })

    expect(client.getQueryData<Budget>(queryKeys.system.budget())).toEqual(fresh)
  })

  it('merges a rate limit frame without discarding the rest of the budget', () => {
    client.setQueryData(queryKeys.system.budget(), budgetRow())

    applyEventFrame(client, {
      event: 'rate_limited',
      data: { strikes_used: 1, strikes_remaining: 2, blocked_until: '2026-09-09T11:00:00+05:30' },
    })

    const budget = client.getQueryData<Budget>(queryKeys.system.budget())
    expect(budget?.strikes_remaining).toBe(2)
    expect(budget?.blocked_until).toBe('2026-09-09T11:00:00+05:30')
    expect(budget?.requests_used).toBe(12_400)
  })

  it('merges a pipeline mode frame', () => {
    client.setQueryData(queryKeys.system.budget(), budgetRow())

    applyEventFrame(client, {
      event: 'pipeline_mode',
      data: { mode: 'blocked_auth', reason: 'token parked' },
    })

    const budget = client.getQueryData<Budget>(queryKeys.system.budget())
    expect(budget?.pipeline_mode).toBe('blocked_auth')
    expect(budget?.pipeline_reason).toBe('token parked')
    expect(budget?.plan_limit_day).toBe(100_000)
  })
})

describe('auth_required frame', () => {
  it('raises needs_reauth on the bootstrap entry the banner reads', () => {
    client.setQueryData(queryKeys.bootstrap(), {
      provisioned: true,
      has_user: true,
      has_credentials: true,
      broker_connected: true,
      token_state: 'active',
      token_expires_at: '2026-09-10T01:30:00+05:30',
      needs_reauth: false,
      data_dir: '/tmp/expirymanager',
      app_version: '1.0.0',
      duckdb_version: '1.5.5',
    })

    applyEventFrame(client, {
      event: 'auth_required',
      data: { token_state: 'expired', reason: 'scheduled', parked_jobs: 2, parked_tasks: 91 },
    })

    const bootstrap = client.getQueryData<Record<string, unknown>>(queryKeys.bootstrap())
    expect(bootstrap?.needs_reauth).toBe(true)
    expect(bootstrap?.broker_connected).toBe(false)
    expect(bootstrap?.token_state).toBe('expired')
  })
})

describe('frame coalescing', () => {
  function manualClock() {
    const queue: Array<() => void> = []
    return {
      schedule: (callback: () => void) => {
        queue.push(callback)
        return queue.length
      },
      cancel: (handle: number) => {
        queue[handle - 1] = () => undefined
      },
      tick: () => {
        const pending = queue.splice(0, queue.length)
        for (const callback of pending) {
          callback()
        }
      },
    }
  }

  it('keeps only the newest frame for the same subject', () => {
    const clock = manualClock()
    const applied: EventFrame[] = []
    const coalescer = createFrameCoalescer((frame) => applied.push(frame), {
      schedule: clock.schedule,
      cancel: clock.cancel,
    })

    for (const done of [10, 20, 30]) {
      coalescer.push({ ...progressFrame, data: { ...progressFrame.data, done } })
    }
    clock.tick()

    expect(applied).toHaveLength(1)
    expect(applied[0].data).toMatchObject({ done: 30 })
  })

  it('keeps frames about different jobs apart', () => {
    const clock = manualClock()
    const applied: EventFrame[] = []
    const coalescer = createFrameCoalescer((frame) => applied.push(frame), {
      schedule: clock.schedule,
      cancel: clock.cancel,
    })

    coalescer.push(progressFrame)
    coalescer.push({ ...progressFrame, data: { ...progressFrame.data, job_id: 'job-2' } })
    clock.tick()

    expect(applied.map((frame) => frame.data)).toMatchObject([
      { job_id: 'job-1' },
      { job_id: 'job-2' },
    ])
  })

  it('applies nothing after dispose', () => {
    const clock = manualClock()
    const apply = vi.fn()
    const coalescer = createFrameCoalescer(apply, {
      schedule: clock.schedule,
      cancel: clock.cancel,
    })

    coalescer.push(progressFrame)
    coalescer.dispose()
    clock.tick()

    expect(apply).not.toHaveBeenCalled()
  })

  it('drives the cache when wired to applyEventFrame', () => {
    const clock = manualClock()
    client.setQueryData(queryKeys.jobs.detail('job-1'), jobRow())
    const coalescer = createFrameCoalescer((frame) => applyEventFrame(client, frame), {
      schedule: clock.schedule,
      cancel: clock.cancel,
    })

    coalescer.push({ ...progressFrame, data: { ...progressFrame.data, done: 5 } })
    coalescer.push({ ...progressFrame, data: { ...progressFrame.data, done: 99 } })
    clock.tick()

    expect(client.getQueryData<Job>(queryKeys.jobs.detail('job-1'))?.done_tasks).toBe(99)
  })
})
