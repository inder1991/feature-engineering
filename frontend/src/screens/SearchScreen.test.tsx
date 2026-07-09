import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SearchScreen } from './SearchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, searchCatalog: vi.fn(), featureImpact: vi.fn(), lineageGraph: vi.fn() }
})
const searchCatalog = vi.mocked(api.searchCatalog)
const featureImpact = vi.mocked(api.featureImpact)
const lineageGraph = vi.mocked(api.lineageGraph)

beforeEach(() => {
  searchCatalog.mockReset()
  featureImpact.mockReset()
  lineageGraph.mockReset()
  // Minimal wire graph so the graph view can always resolve when a test flips to it.
  lineageGraph.mockResolvedValue({
    nodes: [
      {
        id: 'deposits:public.accounts', kind: 'table', object_ref: 'public.accounts',
        table: 'accounts', catalog_source: 'deposits', grain: false, as_of: false,
        stale: false, resolved: true,
      },
      {
        id: 'deposits:public.accounts.balance', kind: 'column',
        object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance',
        catalog_source: 'deposits', grain: false, as_of: false, stale: false, resolved: true,
      },
    ],
    edges: [
      {
        from: 'deposits:public.accounts', to: 'deposits:public.accounts.balance',
        layer: 'joins', kind: 'contains', resolved: true,
      },
    ],
    truncated: false,
  })
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

  // F19: error precedence — rejection shows role=alert, clears stale results,
  // suppresses the zero-state, and the next success clears the alert.
  it('replaces results with an alert on failure and recovers on the next success', async () => {
    searchCatalog.mockResolvedValueOnce([HIT])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()

    searchCatalog.mockRejectedValueOnce(new api.ApiError(500, 'search backend unavailable'))
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('search backend unavailable')
    // Prior results are gone: stale rows must not render below the error.
    expect(screen.queryByText('public.accounts.balance')).not.toBeInTheDocument()
    // Neither empty state renders alongside the alert.
    expect(screen.queryByText(/freshness-vouched catalog/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/no fresh results/i)).not.toBeInTheDocument()

    searchCatalog.mockResolvedValueOnce([HIT])
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // F5: a late response from a superseded search must never overwrite newer results.
  it('ignores a late response from a superseded search', async () => {
    let resolveFirst!: (hits: api.SearchHit[]) => void
    searchCatalog.mockImplementationOnce(
      () =>
        new Promise<api.SearchHit[]>(res => {
          resolveFirst = res
        }),
    )
    searchCatalog.mockResolvedValueOnce([{ ...HIT, object_ref: 'public.customers.email' }])
    render(<SearchScreen />)
    await search('balance')
    await userEvent.clear(screen.getByLabelText('Query'))
    await search('email')
    expect(await screen.findByText('public.customers.email')).toBeInTheDocument()

    // The older request resolves after the newer one already rendered.
    await act(async () => {
      resolveFirst([HIT])
      await Promise.resolve()
    })
    expect(screen.getByText('public.customers.email')).toBeInTheDocument()
    expect(screen.queryByText('public.accounts.balance')).not.toBeInTheDocument()
  })

  // F25: object_ref alone is not unique across catalog sources; keys must be composite.
  it('renders the same object_ref from two catalog sources as distinct rows without duplicate keys', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      searchCatalog.mockResolvedValue([
        { ...HIT, catalog_source: 'deposits' },
        { ...HIT, catalog_source: 'deposits_eu' },
      ])
      render(<SearchScreen />)
      await search()
      expect(await screen.findAllByText('public.accounts.balance')).toHaveLength(2)
      const duplicateKeyErrors = errorSpy.mock.calls.filter(args =>
        String(args[0]).includes('same key'),
      )
      expect(duplicateKeyErrors).toEqual([])
    } finally {
      errorSpy.mockRestore()
    }
  })

  // F10: drift-impact flow is reachable from each result row.
  it('lists derived feature ids inline when Impact finds features', async () => {
    searchCatalog.mockResolvedValue([HIT])
    featureImpact.mockResolvedValue(['feat_01', 'feat_02'])
    render(<SearchScreen />)
    await search()
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    expect(featureImpact).toHaveBeenCalledWith('public.accounts.balance', 'deposits')
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(screen.getByText('feat_02')).toBeInTheDocument()
    expect(screen.getByText('Derived features')).toBeInTheDocument()
  })

  it('states plainly when no features derive from the column', async () => {
    searchCatalog.mockResolvedValue([HIT])
    featureImpact.mockResolvedValue([])
    render(<SearchScreen />)
    await search()
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    expect(
      await screen.findByText('No features derive from this column.'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
  })

  // The List | Graph toggle: list is unchanged behavior; graph maps lineage around a hit.
  it('disables the view toggle with a hint until results exist', async () => {
    searchCatalog.mockResolvedValue([HIT])
    render(<SearchScreen />)
    const toggle = screen.getByRole('group', { name: 'Result view' })
    expect(within(toggle).getByRole('button', { name: 'List' })).toBeDisabled()
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toBeDisabled()
    expect(screen.getByText('Run a search to map lineage.')).toBeInTheDocument()

    await search()
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toBeEnabled()
    expect(screen.queryByText('Run a search to map lineage.')).not.toBeInTheDocument()
  })

  it('keeps the toggle disabled when a search returns nothing', async () => {
    searchCatalog.mockResolvedValue([])
    render(<SearchScreen />)
    await search()
    expect(await screen.findByText(/no fresh results/i)).toBeInTheDocument()
    const toggle = screen.getByRole('group', { name: 'Result view' })
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toBeDisabled()
    expect(screen.getByText('Run a search to map lineage.')).toBeInTheDocument()
  })

  it('flips to the graph view anchored on the first hit, and back to the unchanged list', async () => {
    searchCatalog.mockResolvedValue([HIT, { ...HIT, object_ref: 'public.accounts.opened_at' }])
    render(<SearchScreen />)
    await search()
    await screen.findByText('public.accounts.balance')

    const toggle = screen.getByRole('group', { name: 'Result view' })
    await userEvent.click(within(toggle).getByRole('button', { name: 'Graph' }))
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toHaveAttribute(
      'aria-pressed', 'true',
    )
    expect(lineageGraph).toHaveBeenCalledWith('public.accounts.balance', 'deposits', {
      direction: 'both', depth: 1,
    })
    // the canvas replaces the rows; the layers panel marks the graph view
    expect(await screen.findByText('Layers')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Impact for public.accounts.balance' }),
    ).not.toBeInTheDocument()

    await userEvent.click(within(toggle).getByRole('button', { name: 'List' }))
    expect(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Layers')).not.toBeInTheDocument()
  })

  it('jumps to the graph anchored on the row whose Graph action was clicked', async () => {
    searchCatalog.mockResolvedValue([HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' }])
    render(<SearchScreen />)
    await search()
    await userEvent.click(
      await screen.findByRole('button', { name: 'Graph for public.accounts.opened_at' }),
    )
    expect(lineageGraph).toHaveBeenCalledWith('public.accounts.opened_at', 'deposits', {
      direction: 'both', depth: 1,
    })
    expect(await screen.findByText('Layers')).toBeInTheDocument()
  })

  it('shows a small alert when the impact check fails', async () => {
    searchCatalog.mockResolvedValue([HIT])
    featureImpact.mockRejectedValue(new api.ApiError(503, 'graph unavailable'))
    render(<SearchScreen />)
    await search()
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Impact check failed: graph unavailable')
    expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
    // The search results themselves stay on screen.
    expect(screen.getByText('public.accounts.balance')).toBeInTheDocument()
  })
})
