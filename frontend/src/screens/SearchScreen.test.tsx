import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SEARCH_PAGE_SIZE } from '../api'
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
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null, sensitivity_display: null,
  additivity: 'semi_additive', unit: 'dollars', currency: 'USD', entity: 'Account', score: 1.2,
}

// Distinct values across groups so a checkbox is addressable by its value alone.
const FACETS: Record<string, api.FacetBucket[]> = {
  source: [{ value: 'deposits', count: 3 }, { value: 'cards', count: 1 }],
  domain: [{ value: 'retail', count: 3 }],
  // The raw source-declared tag ("Declared tag" in the sidebar) and the projected display axis
  // ("Sensitivity") are separate facets with separate vocabularies — see SEARCH_FACET_KEYS.
  sensitivity: [{ value: '(none)', count: 3 }, { value: 'pii', count: 1 }],
  sensitivity_display: [{ value: 'restricted', count: 2 }],
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
  // Task 6: every search read carries the projection marker; `ready` is the normal value.
  return { hits, facets, total, projection: { status: 'ready', code: '', detail: '' } }
}

// Task 2 rebuilt the row: the NAME leads and the physical address is a breadcrumb, so an
// object_ref is no longer one text node anywhere in the DOM. A row is addressed here the way the
// row itself spells it — source › schema.table › column, minus the leading source.
function trailRef(trail: HTMLElement): string {
  return (trail.textContent ?? '').split('›').map(part => part.trim()).slice(1).join('.')
}

function queryRow(ref: string): HTMLElement | null {
  const trail = screen.queryAllByTestId('hit-breadcrumb').find(t => trailRef(t) === ref)
  return trail ? (trail.closest('li') as HTMLElement) : null
}

// waitFor, not findAllByTestId + find: a re-search leaves the PREVIOUS rows on screen, so a query
// that settles as soon as any breadcrumb exists would resolve against the stale set.
async function findRow(ref: string): Promise<HTMLElement> {
  return await waitFor(() => {
    const row = queryRow(ref)
    if (!row) {
      const seen = screen.queryAllByTestId('hit-breadcrumb').map(trailRef).join(', ')
      throw new Error(`no result row for ${ref}; on screen: ${seen || '(none)'}`)
    }
    return row
  })
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
    expect(await findRow('public.accounts.balance')).toBeInTheDocument()
    expect(searchCatalog).toHaveBeenCalledWith('', {}, SEARCH_PAGE_SIZE, 0)
  })

  it('renders context-rich result rows (name, breadcrumb, badges, definition, capabilities)', async () => {
    searchCatalog.mockResolvedValue(
      result([{ ...HIT, is_grain: true, is_as_of: true, sensitivity: 'pii' }],
        { source: [{ value: 'deposits', count: 1 }] }, 1),
    )
    render(<SearchScreen />)
    const row = await findRow('public.accounts.balance')
    // The name leads; the physical address is demoted to the breadcrumb under it.
    expect(within(row).getByTestId('hit-name')).toHaveTextContent('balance')
    expect(within(row).getByTestId('hit-breadcrumb')).toHaveTextContent('deposits')
    expect(within(row).getByTestId('hit-breadcrumb')).toHaveTextContent('public.accounts')
    expect(within(row).getByText('grain')).toBeInTheDocument()
    expect(within(row).getByText('as-of')).toBeInTheDocument()
    expect(within(row).getByText('pii')).toBeInTheDocument()
    expect(within(row).getByText('end-of-day ledger balance')).toBeInTheDocument()
    // What the asset can DO, off the hit's own fields. The entity is named by the grain line, so
    // the quiet meta remainder is only the data type.
    expect(within(row).getByText('Grain key for Account')).toBeInTheDocument()
    expect(within(row).getByText('As-of field')).toBeInTheDocument()
    expect(within(row).getByText('Semi-additive · USD')).toBeInTheDocument()
    expect(within(row).getByText('numeric')).toBeInTheDocument()
  })

  it('counts results with honest "N result(s)" copy from the total', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' }],
        FACETS, 2),
    )
    render(<SearchScreen />)
    expect(await screen.findByRole('status')).toHaveTextContent('2 results')
  })

  it('uses the singular for a single result', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    expect(await screen.findByRole('status')).toHaveTextContent('1 result')
  })

  it('states the total honestly and names the slice on screen', async () => {
    // total counts tables + columns and can exceed the returned (limit-capped) hit page.
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 42))
    render(<SearchScreen />)
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('42 results')
    expect(status).toHaveTextContent('showing 1–1 of 42')
  })

  it('omits absent enrichment fields and includes them when present', async () => {
    searchCatalog.mockResolvedValue(result([
      HIT,
      {
        ...HIT, object_ref: 'public.customers.email', table: 'customers', column: 'email',
        concept: 'contact', domain: 'retail',
      },
    ], FACETS, 2))
    render(<SearchScreen />)
    // The source moved to the breadcrumb and the additivity to a capability line, so the meta
    // remainder is data type, domain, entity, concept — each named, none invented.
    expect(await screen.findByText('numeric · entity: Account')).toBeInTheDocument()
    expect(
      screen.getByText('numeric · retail · entity: Account · concept: contact'),
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
    expect(await findRow('public.accounts.balance')).toBeInTheDocument()
  })
})

describe('search screen — facet sidebar', () => {
  it('renders facet groups and labeled value+count checkboxes from the response', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    // getByRole('group'), not getByText: the assertion is that a labelled SIDEBAR GROUP exists,
    // and a bare text match would also be satisfied by a badge or a meta line elsewhere on screen.
    for (const group of ['Source', 'Domain', 'Sensitivity', 'Declared tag', 'Additivity',
                         'Entity', 'Kind', 'Column role']) {
      expect(screen.getByRole('group', { name: group })).toBeInTheDocument()
    }
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'cards 1' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Grain key 2' })).toBeInTheDocument()
  })

  // The reason this task exists: the six dataset-profile facets were absent from the client's key
  // list, so the groups the server returns for them could not even be SENT. This walks one of them
  // end to end — rendered from the response, selected, and on the wire.
  it('sends a dataset-profile facet the client previously could not carry', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT], { ...FACETS, data_role: [{ value: 'crosswalk', count: 2 }] }, 1),
    )
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    // "Table role", not "Column role": data_role is the TABLE axis, the flags are the column axis.
    const tableRole = within(screen.getByRole('group', { name: 'Table role' }))
    await userEvent.click(tableRole.getByRole('checkbox', { name: 'crosswalk 2' }))
    expect(searchCatalog).toHaveBeenLastCalledWith(
      '', { data_role: ['crosswalk'] }, SEARCH_PAGE_SIZE, 0,
    )
    expect(window.location.hash).toBe('#/search?data_role=crosswalk')
  })

  // A group with more values than the collapsed window hides the tail behind one disclosure, and
  // expanding reveals the whole group rather than a second page of it.
  it('collapses a long facet group to six values and expands it on request', async () => {
    searchCatalog.mockResolvedValue(result([HIT], {
      ...FACETS,
      domain: Array.from({ length: 9 }, (_, i) => ({ value: `domain_${i}`, count: 9 - i })),
    }, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    const domain = () => within(screen.getByRole('group', { name: 'Domain' }))
    expect(domain().getAllByRole('checkbox')).toHaveLength(6)
    expect(domain().queryByRole('checkbox', { name: 'domain_8 1' })).not.toBeInTheDocument()
    await userEvent.click(domain().getByRole('button', { name: 'Show all 9' }))
    expect(domain().getAllByRole('checkbox')).toHaveLength(9)
    expect(domain().getByRole('checkbox', { name: 'domain_8 1' })).toBeInTheDocument()
  })

  it('omits a facet group the response does not carry', async () => {
    const { domain, ...rest } = FACETS
    void domain
    searchCatalog.mockResolvedValue(result([HIT], rest, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.queryByText('Domain')).not.toBeInTheDocument()
    expect(screen.getByText('Source')).toBeInTheDocument()
  })

  it('checking a facet re-fetches with the right params and re-renders counts from the response', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).not.toBeChecked()

    // the fresh response narrows the same value's count — the sidebar must reflect it, not guess.
    searchCatalog.mockResolvedValueOnce(
      result([HIT], { ...FACETS, source: [{ value: 'deposits', count: 1 }] }, 1),
    )
    await userEvent.click(screen.getByRole('checkbox', { name: 'deposits 3' }))

    expect(searchCatalog).toHaveBeenLastCalledWith('', { source: ['deposits'] }, SEARCH_PAGE_SIZE, 0)
    expect(window.location.hash).toBe('#/search?source=deposits')
    expect(await screen.findByRole('checkbox', { name: 'deposits 1' })).toBeChecked()
  })

  it('encodes multi-select (OR within a group) and a flag as repeated shareable params', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    await userEvent.click(screen.getByRole('checkbox', { name: 'deposits 3' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'cards 1' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Grain key 2' }))
    expect(window.location.hash).toBe('#/search?source=deposits&source=cards&grain=true')
    expect(searchCatalog).toHaveBeenLastCalledWith(
      '', { source: ['deposits', 'cards'], grain: true }, SEARCH_PAGE_SIZE, 0,
    )
  })

  it('renders the pii sensitivity value with a danger dot (label carries the meaning)', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    const pii = screen.getByRole('checkbox', { name: 'pii 1' })
    expect(pii.closest('label')?.querySelector('.facet-pii-dot')).toBeInTheDocument()
  })

  it('renders no pii option when the response sensitivity facet omits it (read-scope is a hard filter)', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT], { ...FACETS, sensitivity: [{ value: '(none)', count: 4 }] }, 1),
    )
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByRole('group', { name: 'Declared tag' })).toBeInTheDocument()
    // The NULL bucket reads as "Not classified" — the server's own `(none)` token is a wire value,
    // not sidebar copy.
    expect(screen.getByRole('checkbox', { name: 'Not classified 4' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'pii 1' })).not.toBeInTheDocument()
  })

  it('disables a flag with a zero count that is not already selected', async () => {
    searchCatalog.mockResolvedValue(
      result([HIT], { ...FACETS, grain: [{ value: 'true', count: 0 }] }, 1),
    )
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByRole('checkbox', { name: 'Grain key 0' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'As-of field 1' })).toBeEnabled()
  })
})

describe('search screen — active filters and URL state', () => {
  it('shows removable chips and removes one, re-fetching', async () => {
    window.location.hash = '#/search?source=deposits'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).toBeChecked()

    await userEvent.click(screen.getByRole('button', { name: 'Remove source: deposits' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('', {}, SEARCH_PAGE_SIZE, 0)
    expect(screen.queryByText('source: deposits')).not.toBeInTheDocument()
    expect(window.location.hash).toBe('#/search')
  })

  // One owner of the facet vocabulary. Before this the chips carried their own label table, so a
  // chip could read "sensitivity: restricted" under a group titled "Sensitivity" while the raw
  // declared tag sat under "Declared tag" — two names for two facets, mixed up on screen.
  it('names a chip exactly as the sidebar group above it names the facet', async () => {
    window.location.hash = '#/search?sensitivity=(none)&sensitivity_display=restricted'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByText('declared tag: not classified')).toBeInTheDocument()
    expect(screen.getByText('sensitivity: restricted')).toBeInTheDocument()
    // The displayed name is display only: the selection still round-trips on the server's own
    // `(none)` token, in the sidebar and on the wire.
    expect(
      within(screen.getByRole('group', { name: 'Declared tag' }))
        .getByRole('checkbox', { name: 'Not classified 3' }),
    ).toBeChecked()
    expect(searchCatalog).toHaveBeenLastCalledWith(
      '', { sensitivity: ['(none)'], sensitivity_display: ['restricted'] }, SEARCH_PAGE_SIZE, 0,
    )
  })

  it('Clear all resets every filter but keeps the committed query', async () => {
    window.location.hash = '#/search?q=balance&source=deposits&grain=true'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByText('grain')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('balance', {}, SEARCH_PAGE_SIZE, 0)
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
    expect(await findRow('public.accounts.balance')).toBeInTheDocument()
    expect(searchCatalog).toHaveBeenCalledWith(
      'balance',
      { source: ['deposits', 'cards'], additivity: ['semi_additive'], grain: true },
      SEARCH_PAGE_SIZE,
      0,
    )
    expect(screen.getByText('source: deposits')).toBeInTheDocument()
    expect(screen.getByText('source: cards')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'deposits 3' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'semi_additive 3' })).toBeChecked()
  })

  it('submitting the query writes it to the hash and searches with the current filters', async () => {
    window.location.hash = '#/search?source=deposits'
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    await userEvent.type(screen.getByLabelText('Query'), 'balance')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(searchCatalog).toHaveBeenLastCalledWith('balance', { source: ['deposits'] }, SEARCH_PAGE_SIZE, 0)
    expect(window.location.hash).toBe('#/search?q=balance&source=deposits')
  })
})

describe('search screen — errors, ordering, keys', () => {
  // Error precedence: rejection shows role=alert, clears stale results, suppresses the zero-state,
  // and the next success clears the alert.
  it('replaces results with an alert on failure and recovers on the next search', async () => {
    searchCatalog.mockResolvedValueOnce(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    expect(await findRow('public.accounts.balance')).toBeInTheDocument()

    searchCatalog.mockRejectedValueOnce(new api.ApiError(500, 'search backend unavailable'))
    await userEvent.type(screen.getByLabelText('Query'), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('search backend unavailable')
    expect(queryRow('public.accounts.balance')).toBeNull()
    expect(screen.queryByText(/no results match/i)).not.toBeInTheDocument()

    searchCatalog.mockResolvedValueOnce(result([HIT], FACETS, 1))
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await findRow('public.accounts.balance')).toBeInTheDocument()
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
      result([{ ...HIT, object_ref: 'public.customers.email', table: 'customers', column: 'email' }],
        FACETS, 1),
    )
    await userEvent.type(screen.getByLabelText('Query'), 'balance')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    await userEvent.clear(screen.getByLabelText('Query'))
    await userEvent.type(screen.getByLabelText('Query'), 'email')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await findRow('public.customers.email')).toBeInTheDocument()

    await act(async () => { resolveFirst(result([HIT], FACETS, 1)); await Promise.resolve() })
    expect(queryRow('public.customers.email')).not.toBeNull()
    expect(queryRow('public.accounts.balance')).toBeNull()
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
      const trails = await screen.findAllByTestId('hit-breadcrumb')
      // Same ref, two sources — and the breadcrumb leads with the source, so the two rows are
      // distinguishable on screen as well as in the key.
      expect(trails.map(trailRef)).toEqual(['public.accounts.balance', 'public.accounts.balance'])
      expect(trails.map(t => t.textContent)).toEqual([
        'deposits › public.accounts › balance',
        'deposits_eu › public.accounts › balance',
      ])
      const duplicateKeyErrors = errorSpy.mock.calls.filter(args =>
        String(args[0]).includes('same key'),
      )
      expect(duplicateKeyErrors).toEqual([])
    } finally {
      errorSpy.mockRestore()
    }
  })
})

// The Feature impact behaviour itself (the id list, the empty statement, the failure alert) now
// belongs to the row and is pinned in SearchHitRow.test.tsx. What stays here is the WIRING: which
// route each action navigates to, and which row the graph anchors on.
describe('search screen — row actions and graph', () => {
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
      result([HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' }],
        FACETS, 2),
    )
    render(<SearchScreen />)
    await findRow('public.accounts.balance')

    const toggle = screen.getByRole('group', { name: 'Result view' })
    await userEvent.click(within(toggle).getByRole('button', { name: 'Graph' }))
    expect(within(toggle).getByRole('button', { name: 'Graph' })).toHaveAttribute(
      'aria-pressed', 'true',
    )
    expect(lineageGraph).toHaveBeenCalledWith(
      'public.accounts.balance', 'deposits',
      expect.objectContaining({ direction: 'both', depth: 1 }),
    )
    expect(await screen.findByText('Relationship layers')).toBeInTheDocument()
    // The probe for "a result row is on screen" is a control that lives ON the row. Feature
    // impact moved behind the row's overflow disclosure, so it is absent in list view too and
    // would make both halves of this assertion pass vacuously.
    expect(
      screen.queryByRole('button', { name: 'More actions for public.accounts.balance' }),
    ).not.toBeInTheDocument()

    await userEvent.click(within(toggle).getByRole('button', { name: 'List' }))
    expect(
      await screen.findByRole('button', { name: 'More actions for public.accounts.balance' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Relationship layers')).not.toBeInTheDocument()
  })

  it('Open asset navigates to the asset route with the hit\'s source and object_ref', async () => {
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Open asset public.accounts.balance' }),
    )
    // The hit's own catalog_source is the registration lineage key — it rides the asset route,
    // never a client-side default; object_ref's dots survive as query-string chars.
    expect(window.location.hash).toBe(
      '#/asset?source=deposits&object_ref=public.accounts.balance',
    )
  })

  it('Suggested features action navigates to the suggested route with the hit\'s table', async () => {
    // P4's ONE entry point: the sheet is otherwise unreachable. Suggestions are per TABLE, so a
    // A column hit opens the table it lives on -- the bare table_name the backend keys on --
    // AND carries the column it came from. The page stays table-scoped; the column only names
    // the context, which four identical pages otherwise dropped.
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    render(<SearchScreen />)
    // Tertiary now: reached through the row's `···` disclosure, not straight off the row.
    await userEvent.click(
      await screen.findByRole('button', { name: 'More actions for public.accounts.balance' }),
    )
    await userEvent.click(
      screen.getByRole('button', { name: 'Suggested features for accounts' }),
    )
    expect(window.location.hash)
      .toBe('#/suggested?source=deposits&table=accounts&column=balance')
  })

  it('jumps to the graph anchored on the row whose Explore relationships action was clicked', async () => {
    searchCatalog.mockResolvedValue(result([
      HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' },
    ], FACETS, 2))
    render(<SearchScreen />)
    await findRow('public.accounts.balance')
    await userEvent.click(
      await screen.findByRole('button', { name: 'Explore relationships for public.accounts.opened_at' }),
    )
    expect(lineageGraph).toHaveBeenCalledWith(
      'public.accounts.opened_at', 'deposits',
      expect.objectContaining({ direction: 'both', depth: 1 }),
    )
    expect(await screen.findByText('Relationship layers')).toBeInTheDocument()
  })

  // The unfiltered browse lists the TABLE itself as the first hit; its card title looks exactly
  // like a column ref, so users clicked its Graph action believing they anchored a column. The
  // graph must always NAME its anchor, and table rows must be visibly tables.
  it('graph view captions which ref it is anchored on, with its kind', async () => {
    searchCatalog.mockResolvedValue(result([
      HIT, { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' },
    ], FACETS, 2))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Explore relationships for public.accounts.opened_at' }),
    )
    // The hint sentence was replaced by the graph's own context bar, which names the anchor with
    // its kind chip, full ref and wording rather than a parenthetical.
    const bar = await screen.findByRole('region', { name: /graph anchor/i })
    expect(bar).toHaveTextContent('public.accounts.opened_at')
    expect(bar).toHaveTextContent('COL')
  })

  it('table hits carry a table badge so they cannot read as columns', async () => {
    searchCatalog.mockResolvedValue(result([
      { ...HIT, object_ref: 'public.accounts', column: null, kind: 'table' },
      HIT,
    ], FACETS, 2))
    render(<SearchScreen />)
    const tableRow = await findRow('public.accounts')
    expect(within(tableRow).getByText('table')).toHaveClass('badge')
    // The badge is the ONLY place the kind is stated — the meta line leaves it to the badge — so
    // a column row says "table" nowhere.
    const colRow = await findRow('public.accounts.balance')
    expect(within(colRow).queryByText('table')).not.toBeInTheDocument()
  })

  it('a re-search keeps the clicked anchor when it is still in the result set', async () => {
    const hits = [
      { ...HIT, object_ref: 'public.accounts', column: null, kind: 'table' },
      { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' },
    ]
    searchCatalog.mockResolvedValue(result(hits, FACETS, 2))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Explore relationships for public.accounts.opened_at' }),
    )
    expect(await screen.findByRole('region', { name: /graph anchor/i })).toHaveTextContent('public.accounts.opened_at')
    // Re-search (same result set): the anchor must NOT silently reset to the first hit (the table).
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByRole('region', { name: /graph anchor/i })).toHaveTextContent('public.accounts.opened_at')
  })

  it('a re-search falls back to the first hit only when the anchor left the result set', async () => {
    const colHit = { ...HIT, object_ref: 'public.accounts.opened_at', column: 'opened_at' }
    searchCatalog.mockResolvedValue(result([HIT, colHit], FACETS, 2))
    render(<SearchScreen />)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Explore relationships for public.accounts.opened_at' }),
    )
    expect(await screen.findByRole('region', { name: /graph anchor/i })).toHaveTextContent('public.accounts.opened_at')
    searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByRole('region', { name: /graph anchor/i })).toHaveTextContent('public.accounts.balance')
  })
})
