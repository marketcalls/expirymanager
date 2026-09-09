import { cn } from 'cn'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { Budget } from '@/lib/api/types'
import {
  formatCompact,
  formatDateTime,
  formatInteger,
  formatPercent,
  humaniseCode,
  safeRatio,
} from '@/lib/format'

// The daily request budget, which is the one number that decides whether the rest of the day is
// productive. Fyers Standard allows 100,000 requests a day and the governor targets 8 a second
// and 170 a minute against a hard 10 and 200. Spending the budget is not recoverable until the
// IST date rolls, so it belongs in the top bar and not on a screen the user has to visit.
//
// Three facts, in order of how badly they ruin a session:
//   remaining        gone until tomorrow
//   strikes          three per-minute violations and the governor stops the pipeline
//   pipeline mode    already stopped, and why

/** Below this the bar changes tone. Chosen because a full expiry backfill is a five figure job:
 *  under a fifth of the day left is the point at which planning a new one needs a second look. */
const LOW_REMAINING_FRACTION = 0.2

export type BudgetGaugeVariant = 'bar' | 'panel'

export interface BudgetGaugeProps {
  budget: Budget | undefined
  /** 'bar' is the compact top bar form. 'panel' is the dashboard and settings form. */
  variant?: BudgetGaugeVariant
  className?: string
}

function usedFraction(budget: Budget): number {
  return safeRatio(budget.requests_used, budget.plan_limit_day)
}

function isLow(budget: Budget): boolean {
  return safeRatio(budget.remaining, budget.plan_limit_day) < LOW_REMAINING_FRACTION
}

function TooltipBody({ budget }: { budget: Budget }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-medium">Fyers request budget, {budget.ist_date} IST</span>
      <span>
        Used {formatInteger(budget.requests_used)} of {formatInteger(budget.plan_limit_day)} on the{' '}
        {budget.plan} plan
      </span>
      <span>Remaining {formatInteger(budget.remaining)}</span>
      <span>Per minute headroom {formatInteger(budget.minute_headroom)}</span>
      <span>
        Strikes remaining {formatInteger(budget.strikes_remaining)} of 3
        {budget.minute_violations > 0
          ? ', ' + formatInteger(budget.minute_violations) + ' violations today'
          : ''}
      </span>
      <span>Pipeline {humaniseCode(budget.pipeline_mode)}</span>
      {budget.pipeline_reason ? <span>{budget.pipeline_reason}</span> : null}
      {budget.blocked_until ? <span>Blocked until {formatDateTime(budget.blocked_until)}</span> : null}
    </div>
  )
}

function Meter({ budget }: { budget: Budget }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div
        className={cn('h-full', isLow(budget) ? 'bg-destructive' : 'bg-chart-5 dark:bg-chart-1')}
        style={{ width: formatPercent(usedFraction(budget), 4) }}
      />
    </div>
  )
}

export function BudgetGauge({ budget, variant = 'bar', className }: BudgetGaugeProps) {
  if (!budget) {
    // The top bar is present before the first budget response lands. A dash keeps the row height
    // stable instead of making the whole bar reflow when the number arrives.
    return (
      <div className={cn('text-xs text-muted-foreground', className)} aria-label="Budget loading">
        Budget not loaded
      </div>
    )
  }

  if (variant === 'bar') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className={cn('flex w-40 shrink-0 flex-col gap-1', className)}
            aria-label={
              'Request budget: ' +
              formatInteger(budget.remaining) +
              ' of ' +
              formatInteger(budget.plan_limit_day) +
              ' remaining'
            }
          >
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="text-muted-foreground">Budget</span>
              <span
                className={cn('tabular-nums', isLow(budget) && 'text-destructive')}
              >
                {formatCompact(budget.remaining)} left
              </span>
            </div>
            <Meter budget={budget} />
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <TooltipBody budget={budget} />
        </TooltipContent>
      </Tooltip>
    )
  }

  const rows: Array<[string, string]> = [
    ['Used today', formatInteger(budget.requests_used)],
    ['Remaining', formatInteger(budget.remaining)],
    ['Daily limit', formatInteger(budget.plan_limit_day)],
    ['Per minute headroom', formatInteger(budget.minute_headroom)],
    ['Strikes remaining', formatInteger(budget.strikes_remaining) + ' of 3'],
    ['Pipeline', humaniseCode(budget.pipeline_mode)],
  ]

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium tracking-tight">Request budget</span>
        <span className="text-xs text-muted-foreground">
          {budget.plan} plan, {budget.ist_date} IST
        </span>
      </div>
      <Meter budget={budget} />
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
        {rows.map(([term, value]) => (
          <div key={term} className="flex items-baseline justify-between gap-3">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {budget.pipeline_reason ? (
        <p className="text-xs text-muted-foreground">{budget.pipeline_reason}</p>
      ) : null}
      {budget.blocked_until ? (
        <p className="text-xs text-destructive">
          Blocked until {formatDateTime(budget.blocked_until)}
        </p>
      ) : null}
    </div>
  )
}

export default BudgetGauge
