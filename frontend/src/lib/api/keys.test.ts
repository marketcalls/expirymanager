// main.tsx pins per-prefix query defaults with setQueryDefaults(['bars']) and
// setQueryDefaults(['jobs']). Those bind by key prefix, so a key factory whose first segment
// drifts loses its defaults silently: bar arrays would be held for the default five minutes
// instead of sixty seconds, and a job status would be served stale. Nothing visible fails.
//
// These tests are the tripwire on both halves of that arrangement.

import fs from 'node:fs'
import path from 'node:path'

import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import { PINNED_KEY_PREFIXES, queryKeys } from '@/lib/api/keys'

const mainSource = fs.readFileSync(
  path.resolve(import.meta.dirname, '..', '..', 'main.tsx'),
  'utf8',
)

describe('pinned prefixes', () => {
  it('matches exactly the prefixes main.tsx sets defaults on', () => {
    const found = [...mainSource.matchAll(/setQueryDefaults\(\[\s*'([^']+)'\s*\]/g)].map(
      (match) => match[1],
    )
    expect(found.sort()).toEqual([...PINNED_KEY_PREFIXES].sort())
  })

  it('starts every bar key with the pinned bars segment', () => {
    const keys = [
      queryKeys.bars.all(),
      queryKeys.bars.range({ contract_id: 1 }),
      queryKeys.bars.before({ contract_id: 1 }),
      queryKeys.bars.oi({ contract_id: 1 }),
      queryKeys.bars.spot({ underlying_id: 1 }),
    ]
    for (const key of keys) {
      expect(key[0]).toBe('bars')
    }
  })

  it('starts every job key with the pinned jobs segment', () => {
    const keys = [
      queryKeys.jobs.all(),
      queryKeys.jobs.list({ status: 'running' }),
      queryKeys.jobs.detail('job-1'),
      queryKeys.jobs.tasks('job-1', { state: 'failed' }),
    ]
    for (const key of keys) {
      expect(key[0]).toBe('jobs')
    }
  })

  it('actually binds those defaults to the generated keys', () => {
    // The same two calls main.tsx makes, against keys this factory produced.
    const client = new QueryClient()
    client.setQueryDefaults(['bars'], { gcTime: 60_000, staleTime: 60_000 })
    client.setQueryDefaults(['jobs'], { staleTime: 0 })

    expect(client.getQueryDefaults(queryKeys.bars.range({ contract_id: 1 })).gcTime).toBe(60_000)
    expect(client.getQueryDefaults(queryKeys.bars.spot({ underlying_id: 1 })).staleTime).toBe(
      60_000,
    )
    expect(client.getQueryDefaults(queryKeys.jobs.tasks('job-1')).staleTime).toBe(0)

    // And that a neighbouring group does not accidentally inherit them.
    expect(client.getQueryDefaults(queryKeys.contracts.list()).gcTime).toBeUndefined()
  })
})

describe('key shape', () => {
  it('makes a broad key a prefix of the narrow keys under it, so invalidation reaches them', () => {
    const groups: Array<[readonly unknown[], readonly unknown[][]]> = [
      [
        queryKeys.jobs.all(),
        [queryKeys.jobs.list(), queryKeys.jobs.detail('j'), queryKeys.jobs.tasks('j')],
      ],
      [
        queryKeys.underlyings.all(),
        [queryKeys.underlyings.list(), queryKeys.underlyings.detail(1), queryKeys.underlyings.expiries(1)],
      ],
      [queryKeys.contracts.all(), [queryKeys.contracts.detail(1), queryKeys.contracts.bounds(1)]],
      [queryKeys.system.all(), [queryKeys.system.budget(), queryKeys.system.storage()]],
    ]

    for (const [broad, narrow] of groups) {
      for (const key of narrow) {
        expect(key.slice(0, broad.length)).toEqual([...broad])
      }
    }
  })

  it('gives two different filter sets two different cache entries', () => {
    expect(queryKeys.contracts.list({ kind: 'OPT' })).not.toEqual(
      queryKeys.contracts.list({ kind: 'FUT' }),
    )
  })

  it('treats a missing params object as an empty one, so the key is stable', () => {
    expect(queryKeys.contracts.list()).toEqual(queryKeys.contracts.list(undefined))
  })
})
