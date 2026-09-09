import type { ReactNode } from 'react'
import type {
  ColumnDef,
  OnChangeFn,
  RowData,
  RowSelectionState,
  SortingState,
} from '@tanstack/react-table'
import { flexRender, useTable } from '@tanstack/react-table'
import { cn } from 'cn'

import { EmptyState } from '@/components/common/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { AppTableFeatures } from '@/lib/tables/features'
import { tableFeatureSet } from '@/lib/tables/features'

// The generic table four screens render into.
//
// Server driven by default. The listings behind this are cursor paged and sorted in SQL, so the
// table is told manualSorting and the caller turns a header click into a new request rather than
// letting the client reorder a page that is one of many. A screen holding its whole result set
// passes manualSorting false and gets client sorting from the shared sortedRowModel.
//
// Paging controls are deliberately not here. Cursor paging is not page numbers, and a component
// that owned a "next" button would have to own the cursor too. The caller renders its own
// control into `footer`.

export interface DataTableProps<TData extends RowData> {
  data: TData[]
  // The array is heterogeneous by nature: one column yields a string, the next a number. The
  // value parameter is left open so a screen can mix them without casting every entry.
  // oxlint-disable-next-line typescript/no-explicit-any
  columns: Array<ColumnDef<AppTableFeatures, TData, any>>

  /** Stable identity for a row. Without it a row is identified by index, and a selection made
   *  before a refetch lands on whatever moved into that slot. Every screen here has a real id,
   *  so this should always be supplied when selection or row links are in play. */
  getRowId?: (row: TData, index: number) => string

  sorting?: SortingState
  onSortingChange?: OnChangeFn<SortingState>
  /** True when the server did the sorting. Default true. */
  manualSorting?: boolean

  enableRowSelection?: boolean
  rowSelection?: RowSelectionState
  onRowSelectionChange?: OnChangeFn<RowSelectionState>

  onRowClick?: (row: TData) => void

  isLoading?: boolean
  /** Rows of skeleton drawn while loading, sized to the usual page. */
  loadingRows?: number

  emptyTitle?: string
  emptyDescription?: ReactNode
  emptyAction?: ReactNode

  /** Paging controls, totals, anything that belongs under the rows. */
  footer?: ReactNode
  /** Caps the scroll area. The scrollbars are styled globally, so a long table scrolls inside
   *  its own panel instead of dropping an operating system scrollbar onto a dark surface. */
  maxHeight?: string
  stickyHeader?: boolean
  className?: string
}

export function DataTable<TData extends RowData>({
  data,
  columns,
  getRowId,
  sorting,
  onSortingChange,
  manualSorting = true,
  enableRowSelection = false,
  rowSelection,
  onRowSelectionChange,
  onRowClick,
  isLoading = false,
  loadingRows = 8,
  emptyTitle = 'Nothing to show',
  emptyDescription,
  emptyAction,
  footer,
  maxHeight,
  stickyHeader = true,
  className,
}: DataTableProps<TData>) {
  const table = useTable<AppTableFeatures, TData>({
    features: tableFeatureSet,
    data,
    columns,
    getRowId,
    manualSorting,
    enableRowSelection,
    // Only the slices the caller controls are handed back. Leaving a slice out keeps it
    // internal, which is what column sizing and visibility want.
    state: {
      ...(sorting ? { sorting } : {}),
      ...(rowSelection ? { rowSelection } : {}),
    },
    onSortingChange,
    onRowSelectionChange,
  })

  const headerGroups = table.getHeaderGroups()
  const rows = table.getRowModel().rows
  const columnCount = table.getVisibleLeafColumns().length

  return (
    <div className={cn('flex min-w-0 flex-col', className)}>
      <div
        className="min-w-0 overflow-auto rounded-lg border"
        style={maxHeight ? { maxHeight } : undefined}
      >
        <Table>
          <TableHeader className={cn(stickyHeader && 'sticky top-0 z-10 bg-background')}>
            {headerGroups.map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const meta = header.column.columnDef.meta
                  const canSort = header.column.getCanSort()
                  const sorted = header.column.getIsSorted()
                  const content = header.isPlaceholder
                    ? null
                    : flexRender(header.column.columnDef.header, header.getContext())

                  return (
                    <TableHead
                      key={header.id}
                      colSpan={header.colSpan}
                      className={cn(
                        'h-8 whitespace-nowrap text-xs font-medium',
                        (meta?.numeric || meta?.align === 'end') && 'text-right',
                        meta?.headerClassName,
                      )}
                      style={header.column.getSize() ? { width: header.column.getSize() } : undefined}
                      aria-sort={
                        sorted === 'asc'
                          ? 'ascending'
                          : sorted === 'desc'
                            ? 'descending'
                            : canSort
                              ? 'none'
                              : undefined
                      }
                    >
                      {canSort ? (
                        <button
                          type="button"
                          onClick={header.column.getToggleSortingHandler()}
                          className="inline-flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                        >
                          {content}
                          {/* Plain words rather than a glyph. The sort state has to survive a
                              screen reader and a monospace terminal screenshot alike. */}
                          <span className="text-[0.65rem] text-muted-foreground">
                            {sorted === 'asc' ? 'asc' : sorted === 'desc' ? 'desc' : ''}
                          </span>
                        </button>
                      ) : (
                        content
                      )}
                    </TableHead>
                  )
                })}
              </TableRow>
            ))}
          </TableHeader>

          <TableBody>
            {isLoading
              ? Array.from({ length: loadingRows }, (_, rowIndex) => (
                  <TableRow key={'skeleton-' + String(rowIndex)}>
                    {Array.from({ length: Math.max(1, columnCount) }, (_, cellIndex) => (
                      <TableCell key={cellIndex} className="py-1.5">
                        <Skeleton className="h-4 w-full" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              : rows.map((row) => (
                  <TableRow
                    key={row.id}
                    data-state={row.getIsSelected() ? 'selected' : undefined}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                    className={cn(onRowClick && 'cursor-pointer')}
                  >
                    {row.getVisibleCells().map((cell) => {
                      const meta = cell.column.columnDef.meta
                      return (
                        <TableCell
                          key={cell.id}
                          className={cn(
                            'py-1.5 text-sm',
                            meta?.numeric && 'text-right tabular-nums',
                            meta?.align === 'end' && 'text-right',
                            meta?.cellClassName,
                          )}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
          </TableBody>
        </Table>

        {!isLoading && rows.length === 0 ? (
          <EmptyState
            className="rounded-none border-0"
            title={emptyTitle}
            description={emptyDescription}
            action={emptyAction}
          />
        ) : null}
      </div>

      {footer ? (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-2 text-xs text-muted-foreground">
          {footer}
        </div>
      ) : null}
    </div>
  )
}

export default DataTable
