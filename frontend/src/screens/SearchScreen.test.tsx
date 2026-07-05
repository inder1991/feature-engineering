import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SearchScreen } from './SearchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, searchCatalog: vi.fn() }
})
const searchCatalog = vi.mocked(api.searchCatalog)

beforeEach(() => {
  searchCatalog.mockReset()
})

const HIT: api.SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  additivity: 'semi_additive', unit: 'dollars', currency: 'USD', entity: 'Account', score: 1.2,
}

async function search(q = 'balance') {
  await userEvent.type(screen.getByLabelText('query'), q)
  await userEvent.click(screen.getByRole('button', { name: 'Search' }))
}

describe('search screen', () => {
  it('renders context-rich hit cards (grain, aggregation, unit)', async () => {
    searchCatalog.mockResolvedValue([{ ...HIT, is_grain: true, sensitivity: 'pii' }])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.getByText('grain')).toBeInTheDocument()
    expect(screen.getByText('pii')).toBeInTheDocument()
    expect(screen.getByText('end-of-day ledger balance')).toBeInTheDocument()
    expect(screen.getByText('semi_additive · dollars (USD)')).toBeInTheDocument()
  })

  it('omits absent enrichment fields instead of showing blanks', async () => {
    searchCatalog.mockResolvedValue([HIT])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.queryByText('concept')).not.toBeInTheDocument()
    expect(screen.queryByText('domain')).not.toBeInTheDocument()
  })

  it('explains empty results with fail-closed freshness wording', async () => {
    searchCatalog.mockResolvedValue([])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText(/no fresh results/i)).toBeInTheDocument()
    expect(screen.getByText(/re-upload/i)).toBeInTheDocument()
  })
})
