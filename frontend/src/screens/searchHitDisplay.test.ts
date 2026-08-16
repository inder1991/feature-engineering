import { describe, expect, it } from 'vitest'
import type { SearchHit } from '../api'
import { hitBreadcrumb, hitCapabilities, hitDisplayName, hitMeta } from './searchHitDisplay'

const COLUMN: SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  sensitivity_display: null, additivity: null, unit: null, currency: null, entity: null, score: 1,
}
const TABLE: SearchHit = {
  ...COLUMN, object_ref: 'public.accounts', table: 'accounts', column: null, kind: 'table',
  data_type: null, definition: 'customer account master',
}

describe('hitDisplayName', () => {
  it('leads a column hit with its column name', () => {
    expect(hitDisplayName(COLUMN)).toBe('balance')
  })

  it('leads a table hit with its table name', () => {
    expect(hitDisplayName(TABLE)).toBe('accounts')
  })

  // The honesty rule: the catalog carries no attested business name in the search projection, so
  // an identifier is rendered AS the identifier. Expanding `business_dt` to "Business Date" would
  // put a machine-written label where an attested value belongs.
  it('never prettifies an identifier into prose', () => {
    expect(hitDisplayName({ ...COLUMN, column: 'business_dt' })).toBe('business_dt')
    expect(hitDisplayName({ ...COLUMN, column: 'cust_num' })).toBe('cust_num')
  })
})

describe('hitBreadcrumb', () => {
  it('reads source › schema-qualified table › column for a column hit', () => {
    expect(hitBreadcrumb(COLUMN)).toEqual(['deposits', 'public.accounts', 'balance'])
  })

  it('reads source › table for a table hit', () => {
    expect(hitBreadcrumb(TABLE)).toEqual(['deposits', 'public.accounts'])
  })

  it('falls back to the table field when the ref carries no schema', () => {
    expect(hitBreadcrumb({ ...COLUMN, object_ref: 'balance' }))
      .toEqual(['deposits', 'accounts', 'balance'])
  })
})

describe('hitCapabilities', () => {
  it('names the entity a grain key identifies', () => {
    expect(hitCapabilities({ ...COLUMN, is_grain: true, entity: 'Account' }))
      .toEqual([{ key: 'grain', label: 'Grain key for Account' }])
  })

  it('says only "Grain key" when no entity is resolved', () => {
    expect(hitCapabilities({ ...COLUMN, is_grain: true }))
      .toEqual([{ key: 'grain', label: 'Grain key' }])
  })

  it('reports an as-of field', () => {
    expect(hitCapabilities({ ...COLUMN, is_as_of: true }))
      .toEqual([{ key: 'as_of', label: 'As-of field' }])
  })

  it('renders additivity as prose, qualified by currency', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'semi_additive', currency: 'USD', unit: 'dollars' }))
      .toEqual([{ key: 'measure', label: 'Semi-additive · USD' }])
  })

  it('falls back to the unit when there is no currency', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'additive', unit: 'days' }))
      .toEqual([{ key: 'measure', label: 'Additive · days' }])
  })

  it('humanizes an additivity value it does not know', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'ratio_like' }))
      .toEqual([{ key: 'measure', label: 'Ratio like' }])
  })

  it('lists grain, as-of and measure together in that order', () => {
    const caps = hitCapabilities({
      ...COLUMN, is_grain: true, is_as_of: true, entity: 'Account', additivity: 'additive',
    })
    expect(caps.map(c => c.key)).toEqual(['grain', 'as_of', 'measure'])
  })

  // The search response carries no review state, so a row can never claim one.
  it('claims nothing when the hit carries no roles', () => {
    expect(hitCapabilities(COLUMN)).toEqual([])
  })
})

describe('hitMeta', () => {
  it('carries data type, domain and the concept without an authority claim', () => {
    expect(hitMeta({ ...COLUMN, domain: 'retail', concept: 'account_balance' }))
      .toEqual(['numeric', 'retail', 'concept: account_balance'])
  })

  // The row renders a `table` badge of its own, so repeating the word here would print it twice.
  it('omits the kind — the row badges it', () => {
    expect(hitMeta(TABLE)).toEqual([])
  })

  it('names the entity when no grain line already does', () => {
    expect(hitMeta({ ...COLUMN, entity: 'Account' })).toEqual(['numeric', 'entity: Account'])
  })

  // "Grain key for Account" already names it; saying it twice on one row is noise.
  it('leaves the entity to the grain capability line when there is one', () => {
    expect(hitMeta({ ...COLUMN, is_grain: true, entity: 'Account' })).toEqual(['numeric'])
  })

  it('drops every field the catalog does not hold', () => {
    expect(hitMeta({ ...COLUMN, data_type: null })).toEqual([])
  })
})
