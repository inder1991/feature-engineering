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
  await userEvent.type(screen.getByLabelText('Query'), q)
  await userEvent.click(screen.getByRole('button', { name: 'Search' }))
}

describe('search screen', () => {
  it('renders context-rich result rows (badges, definition, meta line)', async () => {
    searchCatalog.mockResolvedValue([{ ...HIT, is_grain: true, is_as_of: true, sensitivity: 'pii' }])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.getByText('grain')).toBeInTheDocument()
    expect(screen.getByText('as-of')).toBeInTheDocument()
    expect(screen.getByText('pii')).toBeInTheDocument()
    expect(screen.getByText('end-of-day ledger balance')).toBeInTheDocument()
    expect(
      screen.getByText('numeric · deposits · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
  })

  it('counts results above the list', async () => {
    searchCatalog.mockResolvedValue([HIT, { ...HIT, object_ref: 'public.accounts.opened_at' }])
    render(<SearchScreen />)
    await search()
    // The count number is wrapped in an accent span; assert the whole line via role=status.
    expect(await screen.findByRole('status')).toHaveTextContent('2 columns')
  })

  it('uses the singular for a single result', async () => {
    searchCatalog.mockResolvedValue([HIT])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByRole('status')).toHaveTextContent('1 column')
  })

  it('omits absent enrichment fields and includes them when present', async () => {
    searchCatalog.mockResolvedValue([
      HIT,
      { ...HIT, object_ref: 'public.customers.email', concept: 'contact', domain: 'retail' },
    ])
    render(<SearchScreen />)
    await search()
    // HIT has no concept/domain: nothing between source and entity.
    expect(
      await screen.findByText('numeric · deposits · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
    // The enriched hit carries them in the meta line.
    expect(
      screen.getByText('numeric · deposits · contact · retail · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
  })

  it('explains empty results with fail-closed freshness wording', async () => {
    searchCatalog.mockResolvedValue([])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText(/no fresh results/i)).toBeInTheDocument()
    expect(screen.getByText(/re-upload/i)).toBeInTheDocument()
  })

  it('offers a zero-state with suggestion chips before the first search', async () => {
    searchCatalog.mockResolvedValue([HIT])
    render(<SearchScreen />)
    expect(screen.getByRole('search')).toBeInTheDocument()
    expect(screen.getByText(/freshness-vouched catalog/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'balance' }))
    expect(searchCatalog).toHaveBeenCalledWith('balance')
    expect(screen.getByLabelText('Query')).toHaveValue('balance')
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.queryByText(/freshness-vouched catalog/i)).not.toBeInTheDocument()
  })
})
