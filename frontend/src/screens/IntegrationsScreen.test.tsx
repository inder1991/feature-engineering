import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { IntegrationsScreen } from './IntegrationsScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listIntegrations: vi.fn(),
    createIntegration: vi.fn(),
    patchIntegration: vi.fn(),
    deleteIntegration: vi.fn(),
    discoverServices: vi.fn(),
    listSyncs: vi.fn(),
    createSync: vi.fn(),
    patchSync: vi.fn(),
    deleteSync: vi.fn(),
  }
})
const listIntegrations = vi.mocked(api.listIntegrations)
const createIntegration = vi.mocked(api.createIntegration)
const deleteIntegration = vi.mocked(api.deleteIntegration)
const discoverServices = vi.mocked(api.discoverServices)
const listSyncs = vi.mocked(api.listSyncs)
const createSync = vi.mocked(api.createSync)

beforeEach(() => {
  vi.clearAllMocks()
  listIntegrations.mockResolvedValue([])
  discoverServices.mockResolvedValue([])
  listSyncs.mockResolvedValue([])
})

const INTEGRATION: api.Integration = {
  integration_id: 'intg_01HZXAAAAAAAAAAAAAAAAAAAAA',
  name: 'Corporate OpenMetadata',
  base_url: 'https://om.internal.test',
  token_env: 'FEATUREGEN_OM_TOKEN__CORP',
  tag_map: { 'PII.Sensitive': 'pii' },
  created_by: 'user:o',
  created_at: '2026-07-09T12:00:00+00:00',
  token_present: true,
}

const SYNC: api.Sync = {
  sync_id: 'sync_01HZYBBBBBBBBBBBBBBBBBBBBB',
  integration_id: INTEGRATION.integration_id,
  service_name: 'mysql_prod',
  database_filter: 'cards_db',
  schema_filter: 'public',
  target_source: 'cards',
  tag_map_override: null,
  table_naming: 'table',
  created_by: 'user:o',
  created_at: '2026-07-09T12:05:00+00:00',
  last_import_at: null,
}

const SERVICES: api.DiscoveredService[] = [
  {
    service_name: 'mysql_prod', service_type: 'Mysql', fqn: 'mysql_prod',
    synced: true, sync_id: SYNC.sync_id,
  },
  {
    service_name: 'bq_marketing', service_type: 'BigQuery', fqn: 'bq_marketing',
    synced: false, sync_id: null,
  },
]

async function cardFor(name: string): Promise<HTMLElement> {
  const el = await screen.findByText(name)
  const card = el.closest('.integration')
  if (!card) throw new Error(`integration card for ${name} not found`)
  return card as HTMLElement
}

describe('integrations list', () => {
  it('lists an instance with the sealed token + host chips; the token value is never in the DOM', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    render(<IntegrationsScreen />)
    const card = await cardFor('Corporate OpenMetadata')
    // The wire carries the env-var REFERENCE plus a presence flag — never a token value. The card
    // renders the reference and a "sealed" chip; there is no token value client-side to leak.
    expect(card).toHaveTextContent('https://om.internal.test')
    expect(card).toHaveTextContent('FEATUREGEN_OM_TOKEN__CORP')
    expect(within(card).getByText('token sealed')).toBeInTheDocument()
    expect(within(card).getByText('host allowlisted')).toBeInTheDocument()
    // No password/secret input anywhere on the screen.
    expect(document.querySelector('input[type="password"]')).toBeNull()
  })

  it('names the not-set token state so the operator knows what to fix', async () => {
    listIntegrations.mockResolvedValue([{ ...INTEGRATION, token_present: false }])
    render(<IntegrationsScreen />)
    expect(await screen.findByText('token not set')).toBeInTheDocument()
  })

  it('shows an empty state when there are no integrations', async () => {
    listIntegrations.mockResolvedValue([])
    render(<IntegrationsScreen />)
    expect(await screen.findByText('No integrations yet.')).toBeInTheDocument()
  })
})

describe('service discovery', () => {
  it('renders discovered services with synced flags: synced shows the source, unsynced offers Add sync', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    discoverServices.mockResolvedValue(SERVICES)
    render(<IntegrationsScreen />)

    const card = await cardFor('Corporate OpenMetadata')
    await waitFor(() => expect(within(card).getByText('mysql_prod')).toBeInTheDocument())
    expect(card).toHaveTextContent('Services this token can see')
    expect(card).toHaveTextContent('1 synced')

    const synced = within(card).getByText('mysql_prod').closest('.svc') as HTMLElement | null
    if (!synced) throw new Error('mysql_prod row not found')
    expect(within(synced).getByText('→ source cards')).toBeInTheDocument()
    expect(within(synced).getByText('synced')).toBeInTheDocument()
    expect(within(synced).getByRole('button', { name: 'Edit sync' })).toBeInTheDocument()

    const unsynced = within(card).getByText('bq_marketing').closest('.svc') as HTMLElement | null
    if (!unsynced) throw new Error('bq_marketing row not found')
    expect(within(unsynced).getByText('not synced')).toBeInTheDocument()
    expect(within(unsynced).getByRole('button', { name: 'Add sync' })).toBeInTheDocument()
  })

  it('renders an honest failure with retry when OpenMetadata is unreachable, and still allows adding a sync by service name', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    discoverServices
      .mockRejectedValueOnce(new api.ApiError(502, 'OpenMetadata request failed: connect timeout'))
      .mockResolvedValue(SERVICES)
    render(<IntegrationsScreen />)

    const card = await cardFor('Corporate OpenMetadata')
    expect(await within(card).findByText(/could not reach openmetadata/i)).toBeInTheDocument()
    // Discovery is only a convenience: the user can still add a sync by typing a service name.
    expect(within(card).getByRole('button', { name: 'Add sync by service name' })).toBeInTheDocument()

    await userEvent.click(within(card).getByRole('button', { name: 'Retry discovery' }))
    await waitFor(() => expect(within(card).getByText('mysql_prod')).toBeInTheDocument())
    expect(within(card).queryByText(/could not reach openmetadata/i)).not.toBeInTheDocument()
  })
})

describe('add integration', () => {
  it('creates an integration from name + URL, never a token field', async () => {
    listIntegrations.mockResolvedValue([])
    createIntegration.mockResolvedValue(INTEGRATION)
    render(<IntegrationsScreen />)
    await userEvent.click(await screen.findByRole('button', { name: 'Add integration' }))

    const form = screen.getByRole('form', { name: /add an openmetadata integration/i })
    await userEvent.type(within(form).getByLabelText('Name'), 'Corporate OpenMetadata')
    await userEvent.type(within(form).getByLabelText('OpenMetadata URL'), 'https://om.internal.test')
    await userEvent.click(within(form).getByRole('button', { name: 'Save integration' }))

    await waitFor(() =>
      expect(createIntegration).toHaveBeenCalledExactlyOnceWith({
        name: 'Corporate OpenMetadata',
        base_url: 'https://om.internal.test',
      }))
    const spec = createIntegration.mock.calls[0][0]
    expect(Object.keys(spec)).not.toContain('token')
    expect(await screen.findByText('Corporate OpenMetadata')).toBeInTheDocument()
  })

  it('carries token_env only when the operator names one', async () => {
    listIntegrations.mockResolvedValue([])
    createIntegration.mockResolvedValue(INTEGRATION)
    render(<IntegrationsScreen />)
    await userEvent.click(await screen.findByRole('button', { name: 'Add integration' }))
    const form = screen.getByRole('form', { name: /add an openmetadata integration/i })
    await userEvent.type(within(form).getByLabelText('Name'), 'Corporate OpenMetadata')
    await userEvent.type(within(form).getByLabelText('OpenMetadata URL'), 'https://om.internal.test')
    await userEvent.type(
      within(form).getByLabelText('Bot token environment variable'), 'FEATUREGEN_OM_TOKEN__SHARED')
    await userEvent.click(within(form).getByRole('button', { name: 'Save integration' }))
    await waitFor(() =>
      expect(createIntegration).toHaveBeenCalledExactlyOnceWith({
        name: 'Corporate OpenMetadata',
        base_url: 'https://om.internal.test',
        token_env: 'FEATUREGEN_OM_TOKEN__SHARED',
      }))
  })

  it('surfaces a fail-closed egress 400 calmly on the add form', async () => {
    listIntegrations.mockResolvedValue([])
    createIntegration.mockRejectedValue(new api.ApiError(400,
      'no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS'))
    render(<IntegrationsScreen />)
    await userEvent.click(await screen.findByRole('button', { name: 'Add integration' }))
    const form = screen.getByRole('form', { name: /add an openmetadata integration/i })
    await userEvent.type(within(form).getByLabelText('Name'), 'Corporate OpenMetadata')
    await userEvent.type(within(form).getByLabelText('OpenMetadata URL'), 'https://evil.example')
    await userEvent.click(within(form).getByRole('button', { name: 'Save integration' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS')
  })
})

describe('add sync', () => {
  it('adds a sync to an unsynced service and reflects the new binding', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([])
    discoverServices.mockResolvedValue([
      {
        service_name: 'bq_marketing', service_type: 'BigQuery', fqn: 'bq_marketing',
        synced: false, sync_id: null,
      },
    ])
    createSync.mockResolvedValue({
      ...SYNC, sync_id: 'sync_NEW', service_name: 'bq_marketing', target_source: 'marketing',
      database_filter: null, schema_filter: null,
    })
    render(<IntegrationsScreen />)

    const card = await cardFor('Corporate OpenMetadata')
    await waitFor(() => expect(within(card).getByText('bq_marketing')).toBeInTheDocument())
    await userEvent.click(within(card).getByRole('button', { name: 'Add sync' }))

    const form = screen.getByRole('form', { name: /sync bq_marketing into the catalog/i })
    await userEvent.type(within(form).getByLabelText('Target catalog source'), 'marketing')
    await userEvent.click(within(form).getByRole('button', { name: 'Save sync' }))

    await waitFor(() =>
      expect(createSync).toHaveBeenCalledExactlyOnceWith(INTEGRATION.integration_id, {
        service_name: 'bq_marketing',
        target_source: 'marketing',
        database_filter: null,
        schema_filter: null,
        tag_map_override: null,
        table_naming: 'table',
      }))

    const row = within(card).getByText('bq_marketing').closest('.svc') as HTMLElement | null
    if (!row) throw new Error('bq_marketing row not found')
    expect(within(row).getByText('→ source marketing')).toBeInTheDocument()
    expect(within(row).getByText('synced')).toBeInTheDocument()
  })

  it('parses a tag-map override from the compact text field', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([])
    discoverServices.mockResolvedValue([
      {
        service_name: 'bq_marketing', service_type: 'BigQuery', fqn: 'bq_marketing',
        synced: false, sync_id: null,
      },
    ])
    createSync.mockResolvedValue({ ...SYNC, sync_id: 'sync_NEW', service_name: 'bq_marketing' })
    render(<IntegrationsScreen />)
    const card = await cardFor('Corporate OpenMetadata')
    await waitFor(() => expect(within(card).getByText('bq_marketing')).toBeInTheDocument())
    await userEvent.click(within(card).getByRole('button', { name: 'Add sync' }))
    const form = screen.getByRole('form', { name: /sync bq_marketing into the catalog/i })
    await userEvent.type(within(form).getByLabelText('Target catalog source'), 'marketing')
    await userEvent.type(
      within(form).getByLabelText('Tag map override (optional)'),
      'Confidential.Internal -> restricted')
    await userEvent.click(within(form).getByRole('button', { name: 'Save sync' }))
    await waitFor(() => expect(createSync).toHaveBeenCalled())
    expect(createSync.mock.calls[0][1].tag_map_override).toEqual({
      'Confidential.Internal': 'restricted',
    })
  })
})

describe('remove integration', () => {
  it('removes an integration', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    deleteIntegration.mockResolvedValue({ deleted: true })
    render(<IntegrationsScreen />)
    const card = await cardFor('Corporate OpenMetadata')
    await userEvent.click(within(card).getByRole('button', { name: 'Remove' }))
    expect(deleteIntegration).toHaveBeenCalledExactlyOnceWith(INTEGRATION.integration_id)
    await waitFor(() =>
      expect(screen.queryByText('Corporate OpenMetadata')).not.toBeInTheDocument())
  })
})
