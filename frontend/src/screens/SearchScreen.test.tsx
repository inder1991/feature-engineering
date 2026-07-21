import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import './lineage-test-setup' // SearchScreen's graph view mounts the xyflow LineageView canvas
import { SearchScreen } from './SearchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, searchCatalog: vi.fn(), featureImpact: vi.fn(), lineageGraph: vi.fn() }
})
const searchCatalog = vi.mocked(api.searchCatalog)
const featureImpact = vi.mocked(api.featureImpact)
const lineageGraph = vi.mocked(api.lineageGraph)

const HIT: api.SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  additivity: 'semi_additive', unit: 'dollars', currency: 'USD', entity: 'Account', score: 1.2,
}

// Distinct values across groups so a checkbox is addressable by its value alone.
const FACETS: Record<string, api.FacetBucket[]> = {
  source: [{ value: 'deposits', count: 3 }, { value: 'cards', count: 1 }],
  domain: [{ value: 'retail', count: 3 }],
  sensitivity: [{ value: '(none)', count: 3 }, { value: 'pii', count: 1 }],
  additivity: [{ value: 'semi_additive', count: 3 }, { value: 'additive', count: 1 }],
  entity: [{ value: 'Account', count: 3 }],
  kind: [{ value: 'column', count: 4 }],
  grain: [{ value: 'true', count: 2 }],
  as_of: [{ value: 'true', count: 1 }],
}

function result(
  hits: api.SearchHit[],
  facets: Record<string, api.FacetBucket[]> = {},
  total = hits.length,
): api.SearchResult {
  return { hits, facets, total }
}

beforeEach(() => {
  window.location.hash = '#/search'
  searchCatalog.mockReset()
  featureImpact.mockReset()
  lineageGraph.mockReset()
  // The screen auto-browses on mount, so every test needs a resolvable search. Tests override.
  searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
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

describe('search screen — results and rows', () => {
  it('auto-browses on mount (empty query returns the whole set)', async () => {
    render(<SearchScreen />)
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(searchCatalog).toHaveBeenCalledWith('', {})
  })

  it('renders context-rich result rows (badges, definition, meta line)', async () => {
    searchCatalog.mockResolvedValue(
      result([{ ...HIT, is_grain: true, is_as_of: true, sensitivity: 'pii' }],
        { source: [{ value: 'deposits', count: 1 }] }, 1),
    )
    render(<SearchScreen />)
    const list = await screen.findByRole('list')
    expect(within(list).getByText('public.accounts.balance')).toBeInTheDocument()
    expect(within(list).getByText('grain')).toBeInTheDocument()
    expect(within(list).getByText('as-of')).toBeInTheDocument()
    expect(within(list).getByText('pii')).toBeInTheDocument()
    expect(screen.getByText('end-of-day ledger balance')).toBeInTheDocument()
    expect(
      screen.getByText('numeric · deposits · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
  })

  it('counts results with honest "N result(s)" copy from the total', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT, { ...HIT, object_ref: 'public.accounts.opened_at' }], FACETS, 2),
    )
    render(<SearchScreen />)
    expect(await screen.findByRole('status')).toHaveTextContent('2 results')
  })

  it('uses the singular for a single result', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    expect(await screen.findByRole('status')).toHaveTextContent('1 result')
  })

  it('states the total honestly and notes when only the first page is shown', async () => {
    // total counts tables + columns and can exceed the returned (limit-capped) hit page.
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 42))
    render(<SearchScreen />)
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('42 results')
    expect(status).toHaveTextContent('showing the first 1')
  })

  it('omits absent enrichment fields and includes them when present', async () => {
    searchCatalog.mockResolvedValue(result([
      HIT,
      { ...HIT, object_ref: 'public.customers.email', concept: 'contact', domain: 'retail' },
    ], FACETS, 2))
    render(<SearchScreen />)
    expect(
      await screen.findByText('numeric · deposits · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('numeric · deposits · contact · retail · Account · semi_additive · dollars (USD)'),
    ).toBeInTheDocument()
  })

  it('explains zero results with loosen-a-facet + fail-closed freshness wording', async () => {
    searchCatalog.mockResolvedValue(result([], {}, 0))
    render(<SearchScreen />)
    expect(await screen.findByText(/no results match these filters/i)).toBeInTheDocument()
    expect(screen.getByText(/loosen or clear a facet/i)).toBeInTheDocument()
    expect(screen.getByText(/re-uploaded/i)).toBeInTheDocument()
    expect(screen.getByText(/roles cannot see/i)).toBeInTheDocument()
  })

  it('shows a calm loading hint while the initial browse is in flight, not a zero-state', async () => {
    let resolve!: (r: api.SearchResult) => void
    searchCatalog.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    render(<SearchScreen />)
    expect(screen.getByText('Searching the catalog…')).toBeInTheDocument()
    expect(screen.queryByText(/no results match/i)).not.toBeInTheDocument()
    await act(async () => { resolve(result([HIT], FACETS, 1)); await Promise.resolve() })
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
  })
})

describe('search screen — facet sidebar', () => {
  it('renders facet groups and labeled value+count checkboxes from the response', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    for (const group of ['Source', 'Domain', 'Sensitivity', 'Additivity', 'Entity', 'Kind', 'Flags']) {
      expect(screen.getByText(group)).toBeInTheDocument()
    }
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'cards 1' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Grain 2' })).toBeInTheDocument()
  })

  it('omits a facet group the response does not carry', async () => {
    const { domain, ...rest } = FACETS
    void domain
    searchCatalog.mockResolvedValue(result([HIT], rest, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.queryByText('Domain')).not.toBeInTheDocument()
    expect(screen.getByText('Source')).toBeInTheDocument()
  })

  it('checking a facet re-fetches with the right params and re-renders counts from the response', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).not.toBeChecked()

    // the fresh response narrows the same value's count — the sidebar must reflect it, not guess.
    searchCatalog.mockResolvedValueOnce(
      result([HIT], { ...FACETS, source: [{ value: 'deposits', count: 1 }] }, 1),
    )
    await userEvent.click(screen.getByRole('checkbox', { name: 'deposits 3' }))

    expect(searchCatalog).toHaveBeenLastCalledWith('', { source: ['deposits'] })
    expect(window.location.hash).toBe('#/search?source=deposits')
    expect(await screen.findByRole('checkbox', { name: 'deposits 1' })).toBeChecked()
  })

  it('encodes multi-select (OR within a group) and a flag as repeated shareable params', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    await userEvent.click(screen.getByRole('checkbox', { name: 'deposits 3' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'cards 1' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Grain 2' }))
    expect(window.location.hash).toBe('#/search?source=deposits&source=cards&grain=true')
    expect(searchCatalog).toHaveBeenLastCalledWith('', { source: ['deposits', 'cards'], grain: true })
  })

  it('renders the pii sensitivity value with a danger dot (label carries the meaning)', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    const pii = screen.getByRole('checkbox', { name: 'pii 1' })
    expect(pii.closest('label')?.querySelector('.facet-pii-dot')).toBeInTheDocument()
  })

  it('renders no pii option when the response sensitivity facet omits it (read-scope is a hard filter)', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT], { ...FACETS, sensitivity: [{ value: '(none)', count: 4 }] }, 1),
    )
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.getByText('Sensitivity')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '(none) 4' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'pii 1' })).not.toBeInTheDocument()
  })

  it('disables a flag with a zero count that is not already selected', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT], { ...FACETS, grain: [{ value: 'true', count: 0 }] }, 1),
    )
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.getByRole('checkbox', { name: 'Grain 0' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'As-of 1' })).toBeEnabled()
  })
})

describe('search screen — active filters and URL state', () => {
  it('shows removable chips and removes one, re-fetching', async () => {
    window.location.hash = '#/search?source=deposits'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).toBeChecked()

    await userEvent.click(screen.getByRole('button', { name: 'Remove source: deposits' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('', {})
    expect(screen.queryByText('source: deposits')).not.toBeInTheDocument()
    expect(window.location.hash).toBe('#/search')
  })

  it('Clear all resets every filter but keeps the committed query', async () => {
    window.location.hash = '#/search?q=balance&source=deposits&grain=true'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByText('grain')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('balance', {})
    expect(screen.queryByText('source: deposits')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Query')).toHaveValue('balance')
    expect(window.location.hash).toBe('#/search?q=balance')
  })

  it('restores query and every filter from a deep-linked hash on mount', async () => {
    window.location.hash =
      '#/search?q=balance&source=deposits&source=cards&additivity=semi_additive&grain=true'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    expect(screen.getByLabelText('Query')).toHaveValue('balance')
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(searchCatalog).toHaveBeenCalledWith('balance', {
      source: ['deposits', 'cards'], additivity: ['semi_additive'], grain: true,
    })
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByText('source: cards')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'semi_additive 3' })).toBeChecked()
  })

  it('submitting the query writes it to the hash and searches with the current filters', async () => {
    window.location.hash = '#/search?source=deposits'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    await userEvent.type(screen.getByLabelText('Query'), 'balance')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('balance', { source: ['deposits'] })
    expect(window.location.hash).toBe('#/search?q=balance&source=deposits')
  })
})

describe('search screen — errors, ordering, keys', () => {
  // Error precedence: rejection shows role=alert, clears stale results, suppresses the zero-state,
  // and the next success clears the alert.
  it('replaces results with an alert on failure and recovers on the next search', async () => {
    searchCatalog.mockResolvedValueOnce(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()

    searchCatalog.mockRejectedValueOnce(new api.ApiError(500, 'search backend unavailable'))
    await userEvent.type(screen.getByLabelText('Query'), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('search backend unavailable')
    expect(screen.queryByText('public.accounts.balance')).not.toBeInTheDocument()
    expect(screen.queryByText(/no results match/i)).not.toBeInTheDocument()

    searchCatalog.mockResolvedValueOnce(result([HIT], FACETS, 1))
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // A late response from a superseded search must never overwrite newer results.
  it('ignores a late response from a superseded search', async () => {
    searchCatalog.mockResolvedValueOnce(result([], {}, 0)) // mount browse settles empty
    render(<SearchScreen />)
    await screen.findByText(/no results match/i)

    let resolveFirst!: (r: api.SearchResult) => void
    searchCatalog.mockImplementationOnce(() => new Promise(res => { resolveFirst = res }))
    searchCatalog.mockResolvedValueOnce(
      result([{ ...HIT, object_ref: 'public.customers.email' }], FACETS, 1),
    )
    await userEvent.type(screen.getByLabelText('Query'), 'balance')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    await userEvent.clear(screen.getByLabelText('Query'))
    await userEvent.type(screen.getByLabelText('Query'), 'email')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText('public.customers.email')).toBeInTheDocument()

    await act(async () => { resolveFirst(result([HIT], FACETS, 1)); await Promise.resolve() })
    expect(screen.getByText('public.customers.email')).toBeInTheDocument()
    expect(screen.queryByText('public.accounts.balance')).not.toBeInTheDocument()
  })

  // object_ref alone is not unique across catalog sources; keys must be composite.
  it('renders the same object_ref from two catalog sources as distinct rows without duplicate keys', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      searchCatalog.mockResolvedValue(result([
        { ...HIT, catalog_source: 'deposits' },
        { ...HIT, catalog_source: 'deposits_eu' },
      ], FACETS, 2))
      render(<SearchScreen />)
      expect(await screen.findAllByText('public.accounts.balance')).toHaveLength(2)
      const duplicateKeyErrors = errorSpy.mock.calls.filter(args =>
        String(args[0]).includes('same key'),
      )
      expect(duplicateKeyErrors).toEqual([])
    } finally {
      errorSpy.mockRestore()
    }
  })
})

describe('search screen — impact and graph', () => {
  it('lists derived feature ids inline when Impact finds features', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    featureImpact.mockResolvedValue(['feat_01', 'feat_02'])
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    expect(featureImpact).toHaveBeenCalledWith('public.accounts.balance', 'deposits')
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(screen.getByText('feat_02')).toBeInTheDocument()
    expect(screen.getByText('Derived features')).toBeInTheDocument()
  })

  it('states plainly when no features derive from the column', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    featureImpact.mockResolvedValue([])
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    expect(await screen.findByText('No features derive from this column.')).toBeInTheDocument()
    expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
  })

  it('shows a small alert when the impact check fails', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    featureImpact.mockRejectedValue(new api.ApiError(503, 'graph unavailable'))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Impact for public.accounts.balance' }),
    )
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Impact check failed: graph unavailable')
    expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
    expect(screen.getByText('public.accounts.balance')).toBeInTheDocument()
  })

  it('keeps the view toggle disabled with a hint while a search returns nothing', async () => {
    searchCatalog.mockResolvedValue(result([], {}, 0))
    render(<SearchScreen />)
    expect(await screen.findByText(/no results match/i)).toBeInTheDocument()
    const toggle = screen.getByRole('group', { name: 'Result view' })
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toBeDisabled()
    expect(screen.getByText('Run a search to map lineage.')).toBeInTheDocument()
  })

  it('flips to the graph anchored on the first (facet-narrowed) hit, and back to the list', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT, { ...HIT, object_ref: 'public.accounts.opened_at' }], FACETS, 2),
    )
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')

    const toggle = screen.getByRole('group', { name: 'Result view' })
    await userEvent.click(within(toggle).getByRole('button', { name: 'Graph' }))
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toHaveAttribute(
      'aria-pressed', 'true',
    )
    expect(lineageGraph).toHaveBeenCalledWith(
      'public.accounts.balance', 'deposits',
      expect.objectContaining({ direction: 'both', depth: 1 }),
    )
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

  it('Details action navigates to the asset route with the hit\'s source and object_ref', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Details for public.accounts.balance' }),
    )
    // The hit's own catalog_source is the registration lineage key — it rides the asset route,
    // never a client-side default; object_ref's dots survive as query-string chars.
    expect(window.location.hash).toBe(
      '#/asset?source=deposits&object_ref=public.accounts.balance',
    )
  })

  it('jumps to the graph anchored on the row whose Graph action was clicked', async () => {
    searchCatalog.mockResolvedValue(result([
      HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' },
    ], FACETS, 2))
    render(<SearchScreen />)
    await screen.findByText('public.accounts.balance')
    await userEvent.click(
      await screen.findByRole('button', { name: 'Graph for public.accounts.opened_at' }),
    )
    expect(lineageGraph).toHaveBeenCalledWith(
      'public.accounts.opened_at', 'deposits',
      expect.objectContaining({ direction: 'both', depth: 1 }),
    )
    expect(await screen.findByText('Layers')).toBeInTheDocument()
  })
})
