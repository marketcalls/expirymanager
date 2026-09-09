// The shared TanStack Table v9 feature bundle.
//
// v9 changed the shape of this: row models are registered INSIDE the features object, not passed
// as table options, and the factories take no arguments. A v8 habit of passing
// getSortedRowModel: getSortedRowModel() as a table option compiles to nothing here and the
// table simply never sorts.
//
// One bundle for the whole app rather than one per screen. The features object is what carries
// the type information for every table API, so a shared bundle means every DataTable column
// definition has the same inferred surface, and it is what tree shaking prunes against: only the
// features named here are in the bundle at all.
//
// Built statically at module scope on purpose. tableFeatures() is documented as a static helper;
// rebuilding it inside a component hands the table a new features identity on every render.

import {
  columnFilteringFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  createFilteredRowModel,
  createSortedRowModel,
  filterFn_includesString,
  rowSelectionFeature,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_basic,
  sortFn_datetime,
  sortFn_text,
  tableFeatures,
} from '@tanstack/react-table'

/** Per-column presentation hints DataTable reads. Numbers are right aligned and tabular so a
 *  column of strikes or row counts lines up on the decimal point. */
export interface AppColumnMeta {
  align?: 'start' | 'end'
  /** True for any column holding a figure, which gets tabular-nums and end alignment. */
  numeric?: boolean
  headerClassName?: string
  cellClassName?: string
}

/**
 * Features every table in this app gets.
 *
 * What is here and why:
 *  - rowSortingFeature plus sortedRowModel: the client sorts the page it already holds. The
 *    server sorts the full set. Both exist because a cursor-paged screen still wants a column
 *    header to reorder the visible page without spending a round trip.
 *  - columnFilteringFeature plus filteredRowModel: the same argument for a quick in-page filter.
 *  - rowSelectionFeature: the expiry table selects rows and the download sheet prices exactly
 *    that selection. This is the one feature the app cannot do without.
 *  - columnVisibilityFeature and columnSizingFeature: the contract browser has more columns
 *    than fit, and the user decides which.
 *
 * What is deliberately absent: rowPaginationFeature. Paging here is cursor based and server
 * driven, so a client-side paginated row model would silently page the page.
 */
export const tableFeatureSet = tableFeatures({
  columnFilteringFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  rowSelectionFeature,
  rowSortingFeature,

  filteredRowModel: createFilteredRowModel(),
  sortedRowModel: createSortedRowModel(),

  // Registered by name rather than spread from the exported registries. The keys below become
  // the valid string values for filterFn and sortFn on a column definition, and naming them one
  // at a time keeps every unused built-in out of the bundle.
  filterFns: {
    includesString: filterFn_includesString,
  },
  sortFns: {
    alphanumeric: sortFn_alphanumeric,
    basic: sortFn_basic,
    datetime: sortFn_datetime,
    text: sortFn_text,
  },

  // Type-only slot. The value is phantom and is stripped at runtime; only its type is used.
  // Declaring the meta here rather than by global declaration merging means it is scoped to this
  // app's tables instead of every table in the process, and DataTable can right-align a numeric
  // column without every screen restating the class list.
  columnMeta: {} as AppColumnMeta,
})

/** The feature bundle's type. Every ColumnDef and every table instance in the app is written
 *  against this, so a feature added above becomes available everywhere at once. */
export type AppTableFeatures = typeof tableFeatureSet
