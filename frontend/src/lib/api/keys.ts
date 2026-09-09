// The query key factory.
//
// Every cache key in the app is built here. Two reasons this is centralised rather than inlined
// at each useQuery:
//
//  1. The event stream patches the cache by key. A screen that invents its own key writes to a
//     slot the stream never touches, and the screen then looks frozen until a refetch.
//  2. main.tsx pins per-prefix defaults with client.setQueryDefaults(['bars'], ...) and
//     setQueryDefaults(['jobs'], ...). Those bind by key prefix, so the FIRST SEGMENT of every
//     bar key must be exactly 'bars' and of every job key exactly 'jobs'. A key that starts with
//     anything else silently loses the 60 second gcTime cap on bar arrays, or serves a stale
//     job status. Neither failure is visible until it matters.
//
// Keys are built narrowest-last, so a broad key is always a prefix of the narrow ones under it
// and invalidateQueries({ queryKey: queryKeys.jobs.all() }) reaches every job query.

import type { QueryParams } from '@/lib/api/client'

/** Filters and paging arguments travel inside the key so two different filter sets are two
 *  different cache entries rather than one entry that flickers between them. */
export type KeyParams = QueryParams | undefined

export const queryKeys = {
  /** Read once by BootstrapGate before any screen renders. */
  bootstrap: () => ['bootstrap'] as const,

  auth: {
    all: () => ['auth'] as const,
    me: () => ['auth', 'me'] as const,
  },

  broker: {
    all: () => ['broker'] as const,
    fyers: () => ['broker', 'fyers'] as const,
  },

  underlyings: {
    all: () => ['underlyings'] as const,
    list: (params?: KeyParams) => ['underlyings', 'list', params ?? {}] as const,
    detail: (underlyingId: number) => ['underlyings', 'detail', underlyingId] as const,
    expiries: (underlyingId: number, params?: KeyParams) =>
      ['underlyings', 'detail', underlyingId, 'expiries', params ?? {}] as const,
  },

  expiries: {
    all: () => ['expiries'] as const,
    contracts: (underlyingId: number, expiryDate: string, params?: KeyParams) =>
      ['expiries', 'contracts', underlyingId, expiryDate, params ?? {}] as const,
  },

  contracts: {
    all: () => ['contracts'] as const,
    list: (params?: KeyParams) => ['contracts', 'list', params ?? {}] as const,
    detail: (contractId: number) => ['contracts', 'detail', contractId] as const,
    /** The chart calls this on every symbol change, which is why it is its own key and not
     *  folded into detail. */
    bounds: (contractId: number) => ['contracts', 'bounds', contractId] as const,
  },

  // First segment 'jobs' exactly. main.tsx pins staleTime 0 on this prefix so a run status is
  // never served stale.
  jobs: {
    all: () => ['jobs'] as const,
    list: (params?: KeyParams) => ['jobs', 'list', params ?? {}] as const,
    detail: (jobId: string) => ['jobs', 'detail', jobId] as const,
    tasks: (jobId: string, params?: KeyParams) =>
      ['jobs', 'detail', jobId, 'tasks', params ?? {}] as const,
  },

  downloads: {
    all: () => ['downloads'] as const,
    /** The plan preview is a POST, so it is a mutation rather than a query. The key exists so
     *  the last preview can be parked in the cache and compared against the confirmation. */
    plan: (params?: KeyParams) => ['downloads', 'plan', params ?? {}] as const,
  },

  // First segment 'bars' exactly. main.tsx caps gcTime and staleTime at 60 seconds on this
  // prefix, which is what stops a user flicking through contracts from holding every minute
  // series they have visited.
  bars: {
    all: () => ['bars'] as const,
    range: (params?: KeyParams) => ['bars', 'range', params ?? {}] as const,
    before: (params?: KeyParams) => ['bars', 'before', params ?? {}] as const,
    oi: (params?: KeyParams) => ['bars', 'oi', params ?? {}] as const,
    /** Spot series live in the same candles table and are the same size, so they get the same
     *  memory treatment by sharing the prefix. */
    spot: (params?: KeyParams) => ['bars', 'spot', params ?? {}] as const,
  },

  coverage: {
    all: () => ['coverage'] as const,
    grid: (params?: KeyParams) => ['coverage', 'grid', params ?? {}] as const,
    gaps: (params?: KeyParams) => ['coverage', 'gaps', params ?? {}] as const,
  },

  chain: {
    all: () => ['chain'] as const,
    slice: (params?: KeyParams) => ['chain', 'slice', params ?? {}] as const,
    atm: (params?: KeyParams) => ['chain', 'atm', params ?? {}] as const,
  },

  exports: {
    all: () => ['exports'] as const,
    list: (params?: KeyParams) => ['exports', 'list', params ?? {}] as const,
    detail: (exportId: string) => ['exports', 'detail', exportId] as const,
  },

  schedules: {
    all: () => ['schedules'] as const,
    list: () => ['schedules', 'list'] as const,
    detail: (scheduleId: string) => ['schedules', 'detail', scheduleId] as const,
    runs: (scheduleId: string, params?: KeyParams) =>
      ['schedules', 'detail', scheduleId, 'runs', params ?? {}] as const,
  },

  system: {
    all: () => ['system'] as const,
    budget: () => ['system', 'budget'] as const,
    storage: () => ['system', 'storage'] as const,
    health: () => ['system', 'health'] as const,
    settings: () => ['system', 'settings'] as const,
    notifications: (params?: KeyParams) => ['system', 'notifications', params ?? {}] as const,
    requests: (params?: KeyParams) => ['system', 'requests', params ?? {}] as const,
  },
} as const

/** The two prefixes main.tsx binds defaults to. Asserted in a test so a rename of either one
 *  fails loudly here instead of quietly there. */
export const PINNED_KEY_PREFIXES = ['bars', 'jobs'] as const
