// TanStack Table v9 moved the row models into the features object. A v8 habit of passing
// getSortedRowModel as a table option type-checks against nothing and leaves the table
// permanently unsorted, so the bundle is asserted to carry them, and then exercised through a
// real table instance rather than by inspecting its shape alone.

import { batch, createAtom } from '@tanstack/react-store'
import { constructTable } from '@tanstack/table-core'
import { renderPhaseReactivity } from '@tanstack/table-core/reactivity'
import { describe, expect, it } from 'vitest'

import { tableFeatureSet } from '@/lib/tables/features'

interface Row {
  contract_id: number
  fyers_symbol: string
}

const rows: Row[] = [
  { contract_id: 3, fyers_symbol: 'NSE:NIFTY25MAR23100CE' },
  { contract_id: 1, fyers_symbol: 'NSE:NIFTY25MAR22900CE' },
  { contract_id: 2, fyers_symbol: 'NSE:NIFTY25MAR23000CE' },
]

function table(state: Record<string, unknown> = {}) {
  return constructTable({
    // The reactivity binding is what a framework adapter normally supplies. useTable installs
    // React's; this test installs the same render phase binding so the bundle can be exercised
    // without rendering a component.
    features: {
      coreReactivityFeature: renderPhaseReactivity({ createAtom, batch }),
      ...tableFeatureSet,
    },
    data: rows,
    columns: [
      { id: 'contract_id', accessorKey: 'contract_id' },
      { id: 'fyers_symbol', accessorKey: 'fyers_symbol', filterFn: 'includesString' },
    ],
    getRowId: (row: Row) => String(row.contract_id),
    state,
  })
}

describe('shared feature bundle', () => {
  it('registers the row models inside the features object, not as table options', () => {
    expect(typeof tableFeatureSet.sortedRowModel).toBe('function')
    expect(typeof tableFeatureSet.filteredRowModel).toBe('function')
  })

  it('registers the function names a column definition may reference', () => {
    expect(Object.keys(tableFeatureSet.sortFns ?? {}).sort()).toEqual([
      'alphanumeric',
      'basic',
      'datetime',
      'text',
    ])
    expect(Object.keys(tableFeatureSet.filterFns ?? {})).toEqual(['includesString'])
  })

  it('leaves client pagination out, because paging here is cursor based and server driven', () => {
    expect('rowPaginationFeature' in tableFeatureSet).toBe(false)
    expect('paginatedRowModel' in tableFeatureSet).toBe(false)
  })

  it('actually sorts through the registered sorted row model', () => {
    const sorted = table({ sorting: [{ id: 'contract_id', desc: false }] })
    expect(sorted.getRowModel().rows.map((row) => row.original.contract_id)).toEqual([1, 2, 3])

    const descending = table({ sorting: [{ id: 'contract_id', desc: true }] })
    expect(descending.getRowModel().rows.map((row) => row.original.contract_id)).toEqual([3, 2, 1])
  })

  it('actually filters through the registered filtered row model', () => {
    const filtered = table({
      columnFilters: [{ id: 'fyers_symbol', value: '23000' }],
    })
    expect(filtered.getRowModel().rows.map((row) => row.original.contract_id)).toEqual([2])
  })

  it('identifies a selected row by the caller supplied id, not by its position', () => {
    const selected = table({ rowSelection: { '2': true } })
    expect(selected.getRowModel().rows.filter((row) => row.getIsSelected()).map((row) => row.id)).toEqual(
      ['2'],
    )
  })
})
