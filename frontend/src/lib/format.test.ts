// The formatting that matters is the clock. Everything the user reads here is an Indian market
// wall clock, and the backend sends two shapes: a session expiry with an explicit +05:30 offset
// and a DuckDB timestamp with no zone at all, already IST. Handing the naive one to the browser
// to interpret would shift every candle time by the developer's own offset, which is invisible
// on a laptop set to IST and wrong everywhere else.

import { describe, expect, it } from 'vitest'

import {
  EMPTY_VALUE,
  formatBytes,
  formatCompact,
  formatDateTime,
  formatDuration,
  formatInteger,
  formatPercent,
  formatRelative,
  humaniseCode,
  parseTimestamp,
  pluralise,
  safeRatio,
} from '@/lib/format'

describe('timestamps', () => {
  it('reads a naive backend timestamp as IST rather than as machine local time', () => {
    // 15:29 IST is 09:59 UTC. Asserting the instant, not the rendering, so this holds on a
    // machine in any zone.
    expect(parseTimestamp('2025-03-27T15:29:00')?.toISOString()).toBe('2025-03-27T09:59:00.000Z')
  })

  it('honours an explicit offset when the backend sends one', () => {
    expect(parseTimestamp('2026-09-10T01:30:00+05:30')?.toISOString()).toBe(
      '2026-09-09T20:00:00.000Z',
    )
  })

  it('renders in IST regardless of the machine zone', () => {
    const rendered = formatDateTime('2025-03-27T15:29:00')
    expect(rendered).toContain('27 Mar 2025')
    expect(rendered).toContain('15:29:00')
    expect(rendered).toContain('IST')
  })

  it('returns the empty marker rather than Invalid Date', () => {
    expect(formatDateTime(null)).toBe(EMPTY_VALUE)
    expect(formatDateTime('not a timestamp')).toBe(EMPTY_VALUE)
    expect(parseTimestamp(undefined)).toBeNull()
  })

  it('describes a span in both directions from a supplied now', () => {
    const now = new Date('2026-09-09T10:00:00+05:30')
    expect(formatRelative('2026-09-09T09:00:00+05:30', now)).toBe('1h ago')
    expect(formatRelative('2026-09-09T12:30:00+05:30', now)).toBe('in 2h 30m')
    expect(formatRelative('2026-09-09T10:00:10+05:30', now)).toBe('just now')
  })
})

describe('numbers', () => {
  it('shortens large counts and leaves small ones grouped', () => {
    expect(formatCompact(487)).toBe('487')
    expect(formatCompact(12_400)).toBe('12.4k')
    expect(formatCompact(487_213_004)).toBe('487m')
  })

  it('reports disk usage in binary units, the way the operating system does', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1024)).toBe('1.00 KiB')
    expect(formatBytes(7_412_340_224)).toBe('6.90 GiB')
  })

  it('clamps a ratio, because two live counters can briefly disagree', () => {
    expect(formatPercent(1.04)).toBe('100%')
    expect(formatPercent(-0.2)).toBe('0%')
    expect(safeRatio(10, 0)).toBe(0)
    expect(safeRatio(5, 10)).toBe(0.5)
  })

  it('coarsens a duration as it grows', () => {
    expect(formatDuration(45)).toBe('45s')
    expect(formatDuration(320)).toBe('5m 20s')
    expect(formatDuration(3_600)).toBe('1h')
    expect(formatDuration(93_600)).toBe('1d 2h')
  })

  it('marks an absent number rather than printing NaN', () => {
    expect(formatInteger(null)).toBe(EMPTY_VALUE)
    expect(formatBytes(Number.NaN)).toBe(EMPTY_VALUE)
  })
})

describe('text', () => {
  it('never prints a bare singular for a plural count', () => {
    expect(pluralise(1, 'job')).toBe('1 job')
    expect(pluralise(0, 'job')).toBe('0 jobs')
    expect(pluralise(3, 'expiry', 'expiries')).toBe('3 expiries')
  })

  it('turns a wire enum into something a person can read', () => {
    expect(humaniseCode('blocked_auth')).toBe('Blocked auth')
    expect(humaniseCode(null)).toBe(EMPTY_VALUE)
  })
})
