import type { ReactNode } from 'react'
import { cn } from 'cn'

// Every screen opens the same way, so the title, the one line of orientation and the primary
// actions are one component rather than ten slightly different flex rows.

export interface PageHeaderProps {
  title: string
  /** One line. If it needs two, it belongs in the screen body. */
  description?: ReactNode
  /** Primary actions, laid out inline-end. Buttons only, and compact ones. */
  actions?: ReactNode
  /** Filters and toolbars that belong with the header rather than in the body. */
  children?: ReactNode
  className?: string
}

export function PageHeader({
  title,
  description,
  actions,
  children,
  className,
}: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-3 border-b px-5 py-4', className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h1 className="font-heading text-base font-semibold tracking-tight">{title}</h1>
          {description ? (
            <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      {children ? <div className="flex flex-wrap items-center gap-2">{children}</div> : null}
    </header>
  )
}

export default PageHeader
