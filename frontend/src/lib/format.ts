// Display formatting. Pure functions, no React, no locale guessing.
//
// The trading day here is Indian, so every wall clock the user reads is IST regardless of the
// machine's own zone. A developer testing on a laptop set to UTC must see the same 15:29 close
// the exchange saw, so the zone is pinned rather than taken from the browser.

const IST_ZONE = 'Asia/Kolkata'

/** Grouped with thin separators so a nine digit row count is readable at a glance. en-IN is
 *  deliberate: the user reads lakh grouping everywhere else in this domain. */
const integerFormat = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })

/** Prices are DECIMAL(11,4) in the store. Rendering fewer places would make two different
 *  ticks look identical. */
const priceFormat = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

const strikeFormat = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 })

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: IST_ZONE,
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

const dateTimeFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: IST_ZONE,
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const timeFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: IST_ZONE,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

/** Shown wherever a value is genuinely absent. One string everywhere, so an empty cell never
 *  reads as a bug in one table and a dash in another. */
export const EMPTY_VALUE = 'not set'

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }
  return integerFormat.format(value)
}

/** Compact form for a tile that has no room for 487,213,004. Falls back to the grouped form
 *  below a thousand, where the compact form is not shorter. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }
  const abs = Math.abs(value)
  if (abs < 1000) {
    return integerFormat.format(value)
  }
  const units: Array<[number, string]> = [
    [1_000_000_000, 'b'],
    [1_000_000, 'm'],
    [1_000, 'k'],
  ]
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const scaled = value / size
      const places = Math.abs(scaled) >= 100 ? 0 : 1
      return scaled.toFixed(places).replace(/\.0$/, '') + suffix
    }
  }
  return integerFormat.format(value)
}

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }
  return priceFormat.format(value)
}

export function formatStrike(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }
  return strikeFormat.format(value)
}

/** Binary units, because this is disk usage and the operating system reports the same file the
 *  same way. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return EMPTY_VALUE
  }
  if (value < 1024) {
    return String(Math.round(value)) + ' B'
  }
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let scaled = value / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && scaled >= 1024; index += 1) {
    scaled /= 1024
    unit = units[index]
  }
  const places = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2
  return scaled.toFixed(places) + ' ' + unit
}

/** `fraction` is 0 to 1. Values are clamped, because a progress ratio computed from two live
 *  counters can momentarily exceed 1 and a 104 percent bar looks like corruption. */
export function formatPercent(fraction: number | null | undefined, places = 0): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
    return EMPTY_VALUE
  }
  const clamped = Math.min(1, Math.max(0, fraction))
  return (clamped * 100).toFixed(places) + '%'
}

export function safeRatio(numerator: number, denominator: number): number {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) {
    return 0
  }
  return Math.min(1, Math.max(0, numerator / denominator))
}

/** Coarse by design. An ETA on a job with a governor in front of it is an estimate, and showing
 *  seconds on a two hour run implies a precision that is not there. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return EMPTY_VALUE
  }
  const total = Math.round(seconds)
  if (total < 60) {
    return String(total) + 's'
  }
  const minutes = Math.floor(total / 60)
  if (minutes < 60) {
    const rest = total % 60
    return rest === 0 ? String(minutes) + 'm' : String(minutes) + 'm ' + String(rest) + 's'
  }
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  if (hours < 24) {
    return restMinutes === 0 ? String(hours) + 'h' : String(hours) + 'h ' + String(restMinutes) + 'm'
  }
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours === 0 ? String(days) + 'd' : String(days) + 'd ' + String(restHours) + 'h'
}

export function formatLatency(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined || !Number.isFinite(milliseconds)) {
    return EMPTY_VALUE
  }
  if (milliseconds < 1000) {
    return String(Math.round(milliseconds)) + ' ms'
  }
  return (milliseconds / 1000).toFixed(2) + ' s'
}

/**
 * Parses a timestamp the backend produced.
 *
 * Two shapes arrive. A session or token expiry carries an explicit IST offset. A DuckDB
 * timestamp is naive and is already IST wall clock. Date.parse treats a naive string as local
 * time, which is wrong on any machine that is not in IST, so the naive case gets the offset
 * appended before parsing rather than being handed to the browser to guess.
 */
export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) {
    return null
  }
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  const parsed = new Date(hasZone ? value : value + '+05:30')
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function formatDate(value: string | null | undefined): string {
  const parsed = parseTimestamp(value)
  return parsed ? dateFormat.format(parsed) : EMPTY_VALUE
}

export function formatDateTime(value: string | null | undefined): string {
  const parsed = parseTimestamp(value)
  return parsed ? dateTimeFormat.format(parsed) + ' IST' : EMPTY_VALUE
}

export function formatTime(value: string | null | undefined): string {
  const parsed = parseTimestamp(value)
  return parsed ? timeFormat.format(parsed) + ' IST' : EMPTY_VALUE
}

/** Chart and bounds payloads carry UTC seconds, not ISO strings. */
export function formatEpochSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return EMPTY_VALUE
  }
  return dateTimeFormat.format(new Date(seconds * 1000)) + ' IST'
}

/**
 * Coarse relative span, past or future.
 *
 * `now` is a parameter rather than a call to Date.now so this stays a pure function and a test
 * does not have to freeze the clock.
 */
export function formatRelative(value: string | null | undefined, now: Date = new Date()): string {
  const parsed = parseTimestamp(value)
  if (!parsed) {
    return EMPTY_VALUE
  }
  const deltaSeconds = (parsed.getTime() - now.getTime()) / 1000
  const magnitude = formatDuration(Math.abs(deltaSeconds))
  if (Math.abs(deltaSeconds) < 45) {
    return 'just now'
  }
  return deltaSeconds < 0 ? magnitude + ' ago' : 'in ' + magnitude
}

/** Plain text plural. No library, and no bare "1 items". */
export function pluralise(count: number, singular: string, plural?: string): string {
  const word = count === 1 ? singular : (plural ?? singular + 's')
  return formatInteger(count) + ' ' + word
}

/** Turns a wire enum into a label. Screens print these directly, so the vocabulary of the
 *  backend never leaks as blocked_auth into the interface. */
export function humaniseCode(value: string | null | undefined): string {
  if (!value) {
    return EMPTY_VALUE
  }
  const spaced = value.replace(/[_-]+/g, ' ').trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}
