import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { DataSourcesPanel } from './DataSourcesPanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getDataSourceConnections: vi.fn(),
    getCatalogEngines: vi.fn(),
    putCatalogEngine: vi.fn(),
    putDataSourceConnection: vi.fn(),
  }
})
const getConns = vi.mocked(api.getDataSourceConnections)
const getCatalogs = vi.mocked(api.getCatalogEngines)
const putCatalog = vi.mocked(api.putCatalogEngine)
const putConn = vi.mocked(api.putDataSourceConnection)

function connection(over: Partial<api.DataSourceConnection> = {}): api.DataSourceConnection {
  return {
    connection_id: 'edp-hive', environment: 'dev', engine: 'hive', tier: 'edp',
    host: 'hiveserver2.internal', port: 10000, auth_mechanism: 'kerberos',
    secret_ref: 'vault://featuregen/edp-hive', execution_principal: 'svc_ro',
    allowed_schemas: ['DPL_EIB_COMPLIANCE'], database_name: 'edp_cluster',
    active: true, usable_here: true, ...over,
  }
}

function setup(
  conns: Partial<api.DataSourceConnections> = {}, catalogs: api.CatalogEngine[] = [],
) {
  getConns.mockResolvedValue({
    environment: 'dev', engines: ['hive', 'oracle', 'postgres'], connections: [], ...conns,
  })
  getCatalogs.mockResolvedValue({ catalogs })
  return userEvent.setup()
}

beforeEach(() => {
  getConns.mockReset(); getCatalogs.mockReset(); putCatalog.mockReset(); putConn.mockReset()
})

describe('what an operator came here to fix', () => {
  it('lists UNROUTED catalogs before routed ones', async () => {
    // A list of what is configured hides exactly the thing they opened this screen for.
    setup({}, [
      { catalog_source: 'ftr', engine: 'hive', tier: 'edp', declared_by: 'priya' },
      { catalog_source: 'cib', engine: null, tier: null, declared_by: null },
    ])
    render(<DataSourcesPanel />)
    // Wait for the DATA, not for any listitem — the empty-state row matches immediately and the
    // assertion would pass on it before the fetch resolved.
    await screen.findByText('cib')
    const items = screen.getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('cib')
  })

  it('says plainly what an unrouted catalog costs', async () => {
    setup({}, [{ catalog_source: 'cib', engine: null, tier: null, declared_by: null }])
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/questions on this catalog cannot run/i)).toBeInTheDocument()
  })

  it('declares a catalog engine and tier', async () => {
    const user = setup({}, [{ catalog_source: 'ftr', engine: null, tier: null, declared_by: null }])
    putCatalog.mockResolvedValue({ catalog_source: 'ftr', engine: 'hive', tier: 'edp',
      declared_by: 'user:priya' })
    render(<DataSourcesPanel />)

    // Scoped to the catalog's own row: "Engine" and "Tier" also label the add-route form below.
    const row = (await screen.findByText('ftr')).closest('li') as HTMLElement
    await user.selectOptions(within(row).getByLabelText(/engine/i), 'hive')
    await user.type(within(row).getByLabelText(/tier/i), 'edp')
    await user.click(within(row).getByRole('button', { name: /declare/i }))

    await waitFor(() => expect(putCatalog).toHaveBeenCalledWith('ftr', 'hive', 'edp'))
  })

  it('records who declared it', async () => {
    setup({}, [{ catalog_source: 'ftr', engine: 'hive', tier: 'edp', declared_by: 'user:priya' }])
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/declared by user:priya/i)).toBeInTheDocument()
  })
})

describe('routes', () => {
  it('shows who the connection reads as, and what it may read', async () => {
    // The principal bounds what any profile taken through it can mean; the allowlist is the
    // authorization boundary. Neither belongs hidden behind an edit form.
    setup({ connections: [connection()] })
    render(<DataSourcesPanel />)
    const row = await screen.findByText(/hive · edp/i)
    const item = row.closest('li') as HTMLElement
    expect(within(item).getByText('svc_ro')).toBeInTheDocument()
    expect(within(item).getByText('DPL_EIB_COMPLIANCE')).toBeInTheDocument()
  })

  it('marks a route from ANOTHER environment as inert', async () => {
    // Visible but not working. Leaving someone to deduce that from a gap message is the failure
    // this label exists to prevent.
    setup({ connections: [connection({ environment: 'prod', usable_here: false })] })
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/not this environment/i)).toBeInTheDocument()
  })

  it('marks a switched-off route differently from a wrong-environment one', async () => {
    setup({ connections: [connection({ active: false, usable_here: false })] })
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/switched off/i)).toBeInTheDocument()
  })

  it('says an empty allowlist authorizes nothing', async () => {
    // Never an implicit "all" — a half-configured route must not read as an open one.
    setup({ connections: [connection({ allowed_schemas: [] })] })
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/authorizes nothing/i)).toBeInTheDocument()
  })

  it('says a declared catalog still needs a route', async () => {
    setup({ connections: [] })
    render(<DataSourcesPanel />)
    expect(await screen.findByText(/still cannot be read until one exists/i)).toBeInTheDocument()
  })
})

describe('no secret is typed here', () => {
  it('refuses to submit a literal and says what a reference looks like', async () => {
    const user = setup()
    render(<DataSourcesPanel />)
    await screen.findByText(/add a route/i)
    await user.type(screen.getByLabelText(/secret reference/i), 'hunter2')
    expect(screen.getByText(/never the secret itself/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add route/i })).toBeDisabled()
  })

  it('accepts a secret-manager reference', async () => {
    const user = setup()
    render(<DataSourcesPanel />)
    await screen.findByText(/add a route/i)
    await user.type(screen.getByLabelText(/secret reference/i), 'vault://featuregen/edp')
    expect(screen.getByText(/credential is never stored here/i)).toBeInTheDocument()
  })
})

describe('the environment is the deployment’s, not a field', () => {
  it('states which environment a new route will belong to', async () => {
    setup({ environment: 'uat' })
    render(<DataSourcesPanel />)
    const note = await screen.findByText(/environment this deployment is/i)
    expect(note).toHaveTextContent('uat')
  })
})

describe('permissions', () => {
  it('explains a 403 instead of failing silently', async () => {
    const user = setup({}, [{ catalog_source: 'ftr', engine: null, tier: null, declared_by: null }])
    putCatalog.mockRejectedValue(new api.ApiError(403, 'requires the platform-admin role'))
    render(<DataSourcesPanel />)

    const row = (await screen.findByText('ftr')).closest('li') as HTMLElement
    await user.selectOptions(within(row).getByLabelText(/engine/i), 'hive')
    await user.type(within(row).getByLabelText(/tier/i), 'edp')
    await user.click(within(row).getByRole('button', { name: /declare/i }))

    // T9: this used to render one hardcoded sentence for every 403 on this panel, throwing away
    // the server's own. The role the route actually named is the only one that can be right.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('requires the platform-admin role')
    expect(alert).not.toHaveTextContent(/You can see them, not edit them/i)
  })

  it('surfaces the conflict when a second route claims one engine and tier', async () => {
    const user = setup()
    putConn.mockRejectedValue(new api.ApiError(409, 'another ACTIVE connection already routes'))
    render(<DataSourcesPanel />)
    await screen.findByText(/add a route/i)

    await user.type(screen.getByLabelText(/^name$/i), 'second')
    const form = screen.getByRole('button', { name: /add route/i })
      .closest('form') as HTMLElement
    await user.selectOptions(within(form).getByLabelText(/engine/i), 'hive')
    await user.type(screen.getByLabelText(/^tier$/i), 'edp')
    await user.type(screen.getByLabelText(/^host$/i), 'h')
    await user.type(screen.getByLabelText(/reads as/i), 'svc_ro')
    await user.type(screen.getByLabelText(/secret reference/i), 'vault://x')
    await user.click(screen.getByRole('button', { name: /add route/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/already routes/i)
  })
})
