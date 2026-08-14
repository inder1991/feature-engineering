import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import App from './App'
import { getSession, setSession } from './session'

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listQuarantine: vi.fn(),
    uploadFile: vi.fn(),
    listIntegrations: vi.fn(),
    getTableSuggestionsV4: vi.fn(),
  }
})
const listQuarantine = vi.mocked(api.listQuarantine)
const uploadFile = vi.mocked(api.uploadFile)
const listIntegrations = vi.mocked(api.listIntegrations)
const getTableSuggestions = vi.mocked(api.getTableSuggestionsV4)

beforeEach(() => {
  setSession({ user: 'dev', roles: ['data_owner'] })
  window.location.hash = ''
  listQuarantine.mockReset()
  uploadFile.mockReset()
  listIntegrations.mockReset()
  listIntegrations.mockResolvedValue([])
  getTableSuggestions.mockReset()
})

const ingest = (over: Partial<api.IngestResult>): api.IngestResult => ({
  status: 'ingested', reason: null, asserted: 0, changed_objects: 0, quarantined: 0, flagged: null, ...over })

const qRow = (rowIndex: number): api.QuarantineItem => ({
  row_index: rowIndex,
  reason: 'missing required field(s): type',
  raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' },
})

// Browsers fire hashchange asynchronously; dispatch it synchronously inside act() so the
// route store updates deterministically (same pattern as nav.ts's navigate()).
function arriveAt(hash: string) {
  act(() => {
    window.location.hash = hash
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
}

describe('app shell', () => {
  it('renders twelve nav items in order (Recipe reviews after Governance) and lands on Overview by default', () => {
    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    expect(nav.getAllByRole('button').map(b => b.textContent)).toEqual([
      'Overview',
      'Generate features',
      'Ask a question',
      'Registry',
      'Search',
      'Ingest',
      'Integrations',
      'Review queue',
      'Semantics',
      'Governance',
      'Recipe reviews',
      'Dashboard',
    ])
    expect(screen.getByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'The loop' })).toBeInTheDocument()
    expect(
      screen.getByText(/once data is in, generate features is where the engine works for you/i),
    ).toBeInTheDocument()
  })

  it('nav click navigates and updates location.hash', async () => {
    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    await userEvent.click(nav.getByRole('button', { name: 'Generate features' }))
    expect(window.location.hash).toBe('#/workbench')
    expect(
      screen.getByRole('heading', { level: 1, name: /feature generation/i }),
    ).toBeInTheDocument()
    expect(screen.getByText('CATALOG · GENERATE')).toBeInTheDocument()
  })

  it('deep-links a screen from the hash', () => {
    window.location.hash = '#/search'
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'Search' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /search the catalog/i })).toBeInTheDocument()
  })

  it('deep-links #/suggested to the read-only suggested-features sheet, off the left rail', async () => {
    getTableSuggestions.mockResolvedValue({
      contract_version: 4,
      readiness_counts: {},
      semantic: {
        semantic_context_hash: 'ctx-app-test', table: 'public.comp_fin_tran',
        ranked: [], actionable: [], order_basis: 'test',
      },
      read_mode: 'on_demand',
      read_scope_key: 'scope-test',
      projection: null,
      collection: {
        anchor_catalog_source: 'core_banking',
        anchor_table_ref: 'public.comp_fin_tran',
        anchor_column_ref: null,
        table_known: true,
        summary: { suggested: 0, design_checked: 0, needs_external_validation: 0, groups: 0 },
        groups: [],
        rejections: [],
        neighbourhood: {
          tables_considered: 0, tables_available: 0, truncated: false, max_hops: 1,
          limit_reason: null,
        },
        omitted_counts: {},
      },
      hits: [],
      facets: {},
      next_cursor: null,
    })
    window.location.hash = '#/suggested?source=core_banking&table=public.comp_fin_tran'
    render(<App />)
    expect(screen.getByText('CATALOG · SUGGESTED FEATURES')).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 1, name: 'Suggested features' }),
    ).toBeInTheDocument()
    expect(await screen.findByText(/no suggestions yet/i)).toBeInTheDocument()
    // a detail sheet, not a nav destination: no rail item was added
    const nav = within(screen.getByRole('navigation'))
    expect(nav.queryByRole('button', { name: /suggested/i })).not.toBeInTheDocument()
    expect(getTableSuggestions).toHaveBeenCalledWith('core_banking', 'public.comp_fin_tran')
  })

  it('overview start-here button navigates to Ingest (the route hash stays #/upload)', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'Go to Ingest' }))
    expect(window.location.hash).toBe('#/upload')
    expect(screen.getByRole('heading', { level: 1, name: 'Ingest' })).toBeInTheDocument()
  })

  it('deep-links #/upload to the Ingest screen: two paths, connector gates, mockup copy', () => {
    window.location.hash = '#/upload'
    render(<App />)
    expect(screen.getByText('CATALOG · INGEST')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Ingest' })).toBeInTheDocument()
    expect(
      screen.getByText('Bring data maps into the catalog: upload a file, or pull from a configured sync.'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /upload a schema and facts file/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /pull from a metadata service/i }),
    ).toBeInTheDocument()
    expect(screen.getByRole('list', { name: /connector path/i })).toBeInTheDocument()
  })

  it('Integrations nav item routes to #/integrations and renders the Integrations screen', async () => {
    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    await userEvent.click(nav.getByRole('button', { name: 'Integrations' }))
    expect(window.location.hash).toBe('#/integrations')
    expect(screen.getByRole('heading', { level: 1, name: 'Integrations' })).toBeInTheDocument()
    expect(screen.getByText('CATALOG · INTEGRATIONS')).toBeInTheDocument()
    expect(
      await screen.findByRole('heading', { name: 'OpenMetadata instances' }),
    ).toBeInTheDocument()
    expect(listIntegrations).toHaveBeenCalled()
  })

  it('overview loop links navigate to their screens', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('link', { name: 'Review queue' }))
    expect(window.location.hash).toBe('#/review')
    expect(screen.getByRole('heading', { level: 1, name: 'Review queue' })).toBeInTheDocument()
  })

  it('overview loop Generate features link navigates to the workbench route', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('link', { name: 'Generate features' }))
    expect(window.location.hash).toBe('#/workbench')
    expect(
      screen.getByRole('heading', { level: 1, name: /feature generation/i }),
    ).toBeInTheDocument()
  })

  it('session chips edit the stub session store', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('checkbox', { name: 'pii_reader' }))
    expect(getSession().roles).toContain('pii_reader')
    await userEvent.click(screen.getByRole('checkbox', { name: 'data_owner' }))
    expect(getSession().roles).not.toContain('data_owner')
  })

  it('exposes the functional RBAC roles that grant feature:read (feature lineage + registry)', async () => {
    render(<App />)
    // catalog_viewer and feature_engineer both grant feature:read, so the live UI can exercise
    // the feature-lineage layer and the Registry (the sensitivity-only chips could not).
    for (const role of ['catalog_viewer', 'feature_engineer', 'pii_reader', 'restricted_reader']) {
      expect(screen.getByRole('checkbox', { name: role })).toBeInTheDocument()
    }
    await userEvent.click(screen.getByRole('checkbox', { name: 'feature_engineer' }))
    expect(getSession().roles).toContain('feature_engineer')
  })
})

describe('review ?source= deep-linking', () => {
  it('deep link #/review?source=deposits auto-loads that queue', async () => {
    listQuarantine.mockResolvedValue([qRow(9)])
    window.location.hash = '#/review?source=deposits'
    render(<App />)
    expect(await screen.findByText('row 9')).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('deposits')
    expect(screen.getByLabelText('Source')).toHaveValue('deposits')
  })

  it('upload handoff rides the URL param, and a later ?source= deep link wins over it', async () => {
    uploadFile.mockResolvedValue(ingest({ asserted: 4, quarantined: 2 }))
    listQuarantine.mockImplementation(async source =>
      source === 'cards' ? [qRow(7)] : [qRow(1), qRow(2)])

    render(<App />)
    const nav = within(screen.getByRole('navigation'))
    const main = within(screen.getByRole('main'))
    await userEvent.click(nav.getByRole('button', { name: 'Ingest' }))
    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    await userEvent.click(main.getByRole('button', { name: 'Upload' }))
    await userEvent.click(
      await screen.findByRole('button', { name: /review 2 quarantined rows/i }))

    // The handoff travels in the URL and auto-loads the uploaded source's queue.
    expect(window.location.hash).toBe('#/review?source=deposits')
    expect(await screen.findByText('row 1')).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('deposits')

    // Navigate around, then come back via a different source's deep link (shared URL).
    // The param must win over any leftover handoff state from the deposits upload.
    await userEvent.click(nav.getByRole('button', { name: 'Search' }))
    arriveAt('#/review?source=cards')
    expect(await screen.findByText('row 7')).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('cards')
    expect(screen.queryByText('row 1')).not.toBeInTheDocument()
  })

  it('a param-only hash change reloads the queue without a remount (back/forward)', async () => {
    listQuarantine.mockImplementation(async source =>
      source === 'cards' ? [qRow(7)] : [qRow(1)])
    window.location.hash = '#/review?source=deposits'
    render(<App />)
    expect(await screen.findByText('row 1')).toBeInTheDocument()

    // Same route, different param: the review screen stays mounted, so only the ?source=
    // prop changes. The screen must reload for the source the address bar names.
    arriveAt('#/review?source=cards')
    expect(await screen.findByText('row 7')).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenLastCalledWith('cards')
    expect(screen.queryByText('row 1')).not.toBeInTheDocument()
  })
})
