import type { ReactNode } from 'react'
import { cn } from 'cn'

import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from '@/components/ui/empty'

// An empty table on this app is almost never "no data exists". It is "nothing is discovered
// yet", "the filter excludes everything" or "this needs a login first", and each of those has a
// different next step. So the description is required in practice and the action is what makes
// the state actionable rather than a dead end.
//
// No illustration and no icon. A grey picture of a box adds nothing a sentence does not.

export interface EmptyStateProps {
  title: string
  description?: ReactNode
  /** The one thing to do next. Omitted only when there genuinely is nothing to do. */
  action?: ReactNode
  className?: string
}

export function EmptyState({ title, description, action, className }: EmptyStateProps) {
  return (
    <Empty className={cn('border py-10', className)}>
      <EmptyHeader>
        <EmptyTitle>{title}</EmptyTitle>
        {description ? <EmptyDescription>{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}

export default EmptyState
