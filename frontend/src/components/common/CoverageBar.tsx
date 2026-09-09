import { cn } from 'cn'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatInteger, formatPercent, safeRatio } from '@/lib/format'
import type { Coverage } from '@/lib/api/types'

// One bar, three meanings, reused on the expiries screen, the dashboard and the job detail.
//
// The three states are not shades of the same thing and must not read as a progress bar:
//
//  downloaded  a chunk was fetched and rows were written
//  empty       a chunk was fetched and Fyers had nothing, which is a FINAL answer for an
//              expired contract and not a gap to retry. Conflating this with missing is what
//              makes a completed backfill look permanently unfinished.
//  missing     never fetched. This is the only segment that a download can act on.
//
// Colour alone does not carry that distinction, so the empty segment is also hatched and every
// segment has a text figure behind it in the tooltip.

export interface CoverageBarProps {
  /** Chunks fetched with rows. */
  ok: number
  /** Chunks fetched and confirmed to hold nothing. */
  empty: number
  /** Chunks never fetched. */
  missing: number
  /** Optional row count, shown in the tooltip when present. */
  rows?: number | null
  /** Renders the three counts as text under the bar. */
  showLegend?: boolean
  /** Announced to assistive technology and shown as the tooltip heading. */
  label?: string
  className?: string
}

/** Builds the props from an API coverage block, which is the shape three screens already hold. */
export function coverageBarPropsFrom(coverage: Coverage | null | undefined): CoverageBarProps {
  return {
    ok: coverage?.chunks_ok ?? 0,
    empty: coverage?.chunks_empty ?? 0,
    missing: coverage?.chunks_missing ?? 0,
    rows: coverage?.rows ?? null,
  }
}

export function CoverageBar({
  ok,
  empty,
  missing,
  rows,
  showLegend = false,
  label = 'Coverage',
  className,
}: CoverageBarProps) {
  const total = ok + empty + missing

  const segments = [
    {
      key: 'downloaded',
      name: 'Downloaded',
      count: ok,
      // chart-3 through chart-5 are the neutral ramp this theme ships. Using them rather than
      // a hand picked green keeps the bar inside the palette in both themes.
      className: 'bg-chart-5 dark:bg-chart-1',
    },
    {
      key: 'empty',
      name: 'Empty',
      count: empty,
      // Hatched, so downloaded and empty are still distinguishable without colour.
      className:
        'bg-chart-3 [background-image:repeating-linear-gradient(45deg,transparent,transparent_3px,var(--background)_3px,var(--background)_4px)]',
    },
    {
      key: 'missing',
      name: 'Missing',
      count: missing,
      className: 'bg-muted',
    },
  ]

  const summary =
    total === 0
      ? 'Nothing discovered yet'
      : formatPercent(safeRatio(ok + empty, total)) + ' of ' + formatInteger(total) + ' chunks'

  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted"
            role="img"
            aria-label={label + ': ' + summary}
          >
            {total === 0
              ? null
              : segments.map((segment) =>
                  segment.count === 0 ? null : (
                    <div
                      key={segment.key}
                      className={segment.className}
                      style={{ width: formatPercent(safeRatio(segment.count, total), 4) }}
                    />
                  ),
                )}
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <div className="flex flex-col gap-0.5">
            <span className="font-medium">{label}</span>
            {segments.map((segment) => (
              <span key={segment.key}>
                {segment.name}: {formatInteger(segment.count)}
              </span>
            ))}
            {rows === null || rows === undefined ? null : (
              <span>Rows: {formatInteger(rows)}</span>
            )}
          </div>
        </TooltipContent>
      </Tooltip>

      {showLegend ? (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          {segments.map((segment) => (
            <span key={segment.key} className="tabular-nums">
              {segment.name} {formatInteger(segment.count)}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export default CoverageBar
