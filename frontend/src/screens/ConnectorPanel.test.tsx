import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ConnectorPanel } from './ConnectorPanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listConnectors: vi.fn(),
    createConnector: vi.fn(),
    deleteConnector: vi.fn(),
    previewConnector: vi.fn(),
    importConnector: vi.fn(),
  }
})
const listConnectors = vi.mocked(api.listConnectors)
const createConnector = vi.mocked(api.createConnector)
const deleteConnector = vi.mocked(api.deleteConnector)
const previewConnector = vi.mocked(api.previewConnector)
const importConnector = vi.mocked(api.importConnector)

beforeEach(() => {
  listConnectors.mockReset()
  createConnector.mockReset()
  deleteConnector.mockReset()
  previewConnector.mockReset()
  importConnector.mockReset()
})

const CONNECTOR: api.Connector = {
  connector_id: 'conn_01HZXAAAAAAAAAAAAAAAAAAAAA',
  name: 'cards om',
  base_url: 'https://om.internal.test',
  target_source: 'cards',
  tag_map: { 'PII.Sensitive': 'pii' },
  filters: { service: 'mysql_*', database: 'cards_db', schema: 'public' },
  table_naming: 'table',
  token_env: 'FEATUREGEN_OM_TOKEN__CARDS_OM',
  token_present: true,
  created_by: 'user:o',
  created_at: '2026-07-09T12:00:00+00:00',
}

const SNAPSHOT_HASH = 'ab'.repeat(32)

// One of each table status, one quarantine subline, one unmapped + one mapped + one ignored tag:
// the full review surface from a single canned dry run.
const PREVIEW: api.ConnectorPreview = {
  summary: {
    tables: 3, columns: 14, new: 1, changed: 1, unchanged: 1, removed: 0,
    would_quarantine: 1, semantics_pending: 13,
  },
  tag_map: [
    { om_tag: 'Confidential.Internal', mapped_to: '', unmapped: true, count: 1 },
    { om_tag: 'PII.Sensitive', mapped_to: 'pii', unmapped: false, count: 2 },
    { om_tag: 'Tier.Tier1', mapped_to: '', unmapped: false, count: 5 },
  ],
  tables: [
    {
      table: 'accounts', status: 'new', columns: 4,
      quarantine: [{
        column: 'ssn',
        reason: "unrecognized sensitivity 'Confidential.Internal' (expected one of: pii, restricted)",
      }],
      changes: [],
    },
    {
      table: 'credit_limits', status: 'changed', columns: 4, quarantine: [],
      changes: ['limit_amt type: int -> numeric', 'column risk_flag added'],
    },
    { table: 'card_products', status: 'unchanged', columns: 6, quarantine: [], changes: [] },
  ],
  brake: { would_hold: false, reason: null },
  as_of_suggestions: [
    { table: 'accounts', column: 'opened_on', hint: 'partition column (TIME-UNIT)' },
    { table: 'transactions', column: 'posted_at', hint: 'timestamp column named like a time axis' },
  ],
  snapshot_hash: SNAPSHOT_HASH,
}

const IMPORT_OK: api.ConnectorImportResult = {
  result: {
    status: 'ingested', reason: null, asserted: 3, staled: 0, quarantined: 1,
    flagged: "first upload of 'cards' (13 objects) — review recommended",
  },
  import_id: 'omimp_01HZYBBBBBBBBBBBBBBBBBBBBB',
  review_queue: { quarantined: 1, semantics_pending: 13 },
}

function renderPanel(over: { onReviewQueue?: (s: string) => void; onStage?: (s: string) => void } = {}) {
  render(
    <ConnectorPanel
      onReviewQueue={over.onReviewQueue ?? (() => {})}
      onStage={over.onStage ?? (() => {})}
    />,
  )
}

// Arranges the mocks BEFORE render (the panel lists connections on mount), then walks to a
// rendered preview.
async function renderWithPreview(over: { onReviewQueue?: (s: string) => void } = {}) {
  listConnectors.mockResolvedValue([CONNECTOR])
  previewConnector.mockResolvedValue(PREVIEW)
  renderPanel(over)
  await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
  await screen.findByRole('heading', { name: 'Preview: cards om into source cards' })
}

describe('configured connections', () => {
  it('lists connections with the sealed token state — the token value is never anywhere', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    renderPanel()
    const row = (await screen.findByText('cards om')).closest('li')
    if (!row) throw new Error('connection row not found')
    expect(row).toHaveTextContent('https://om.internal.test')
    expect(row).toHaveTextContent('mysql_*.cards_db.public')
    expect(row).toHaveTextContent('into cards')
    // The wire carries only the env-var reference + a presence flag; the row renders "sealed",
    // never a value (there is no value client-side to leak — api.test pins the response shape).
    expect(within(row).getByText('token sealed')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Preview import' })).toBeInTheDocument()
  })

  it('names the not-set token state so the operator knows what to fix', async () => {
    listConnectors.mockResolvedValue([{ ...CONNECTOR, token_present: false }])
    renderPanel()
    expect(await screen.findByText('token not set')).toBeInTheDocument()
  })

  it('saves a new connection with exactly the config fields — no token ever leaves the form', async () => {
    listConnectors.mockResolvedValue([])
    createConnector.mockResolvedValue(CONNECTOR)
    renderPanel()
    await userEvent.type(screen.getByLabelText('Connection name'), 'cards om')
    await userEvent.type(screen.getByLabelText('OpenMetadata URL'), 'https://om.internal.test')
    await userEvent.type(screen.getByLabelText('Target source'), 'cards')
    await userEvent.type(screen.getByLabelText('Service filter'), 'mysql_*')
    await userEvent.type(screen.getByLabelText('Database filter'), 'cards_db')
    await userEvent.type(screen.getByLabelText('Schema filter'), 'public')
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }))
    expect(await screen.findByText('token sealed')).toBeInTheDocument()
    expect(createConnector).toHaveBeenCalledExactlyOnceWith({
      name: 'cards om',
      base_url: 'https://om.internal.test',
      target_source: 'cards',
      tag_map: {},
      filters: { service: 'mysql_*', database: 'cards_db', schema: 'public' },
      table_naming: 'table',
    })
    const spec = createConnector.mock.calls[0][0]
    expect(Object.keys(spec)).not.toContain('token')
  })

  it('derives the token env-var reference from the name, mirroring the server default', async () => {
    listConnectors.mockResolvedValue([])
    renderPanel()
    await userEvent.type(screen.getByLabelText('Connection name'), 'cards om')
    expect(screen.getByText('FEATUREGEN_OM_TOKEN__CARDS_OM')).toBeInTheDocument()
  })

  it('surfaces a duplicate-name 409 calmly on the form', async () => {
    listConnectors.mockResolvedValue([])
    createConnector.mockRejectedValue(new api.ApiError(409, "connector 'cards om' already exists"))
    renderPanel()
    await userEvent.type(screen.getByLabelText('Connection name'), 'cards om')
    await userEvent.type(screen.getByLabelText('OpenMetadata URL'), 'https://om.internal.test')
    await userEvent.type(screen.getByLabelText('Target source'), 'cards')
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("connector 'cards om' already exists")
  })

  it('removes a connection', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    deleteConnector.mockResolvedValue({ deleted: true })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(deleteConnector).toHaveBeenCalledExactlyOnceWith(CONNECTOR.connector_id)
    expect(screen.queryByText('cards om')).not.toBeInTheDocument()
  })
})

describe('preview rendering', () => {
  it('renders the full dry run: stats, brake, tag map, tables, quarantine, as-of, pending', async () => {
    await renderWithPreview()
    expect(previewConnector).toHaveBeenCalledExactlyOnceWith(CONNECTOR.connector_id)

    const stats = screen.getByRole('group', { name: 'Preview summary' })
    expect(stats).toHaveTextContent('3 tables')
    expect(stats).toHaveTextContent('14 columns')
    expect(stats).toHaveTextContent('1 new tables')
    expect(stats).toHaveTextContent('1 changed')
    expect(stats).toHaveTextContent('1 unchanged')
    expect(stats).toHaveTextContent('1 would quarantine')
    expect(stats).toHaveTextContent('13 semantics pending')

    expect(screen.getByText(/brake: clear/i)).toBeInTheDocument()
    expect(screen.getByText(/held for a human, exactly like a hostile upload/i)).toBeInTheDocument()

    // Tag map: mapped, ignored, and unmapped rows each carry a labeled chip, never color alone.
    const tagRow = screen.getByText('Confidential.Internal').closest('tr')
    if (!tagRow) throw new Error('tag row not found')
    expect(within(tagRow).getByText('unmapped')).toBeInTheDocument()
    expect(within(tagRow).getByLabelText('Map Confidential.Internal')).toBeInTheDocument()
    const piiRow = screen.getByText('PII.Sensitive').closest('tr')
    if (!piiRow) throw new Error('pii row not found')
    expect(within(piiRow).getByText('pii')).toBeInTheDocument()
    expect(within(piiRow).getByText('mapped')).toBeInTheDocument()
    const tierRow = screen.getByText('Tier.Tier1').closest('tr')
    if (!tierRow) throw new Error('tier row not found')
    expect(within(tierRow).getByText('ignored: not a sensitivity')).toBeInTheDocument()
    expect(within(tierRow).getByText('ignored')).toBeInTheDocument()

    // Tables diff: status chips + change lines + the quarantine subline with the honest reason.
    const accounts = screen.getByText('accounts').closest('li')
    if (!accounts) throw new Error('accounts row not found')
    expect(within(accounts).getByText('new')).toBeInTheDocument()
    expect(within(accounts).getByText('1 would quarantine')).toBeInTheDocument()
    expect(within(accounts).getByText('accounts.ssn')).toBeInTheDocument()
    expect(accounts).toHaveTextContent(
      "unrecognized sensitivity 'Confidential.Internal' (expected one of: pii, restricted)")
    const changed = screen.getByText('credit_limits').closest('li')
    if (!changed) throw new Error('credit_limits row not found')
    expect(within(changed).getByText('changed')).toBeInTheDocument()
    expect(within(changed).getByText('limit_amt type: int -> numeric')).toBeInTheDocument()
    expect(within(changed).getByText('column risk_flag added')).toBeInTheDocument()
    const unchanged = screen.getByText('card_products').closest('li')
    if (!unchanged) throw new Error('card_products row not found')
    expect(within(unchanged).getByText('unchanged')).toBeInTheDocument()

    expect(screen.getByText('accounts.opened_on')).toBeInTheDocument()
    expect(screen.getByText(/partition column \(TIME-UNIT\)/)).toBeInTheDocument()
    expect(screen.getByText(/13 columns arrive/)).toBeInTheDocument()
    expect(screen.getByText(/routed to the review queue for owner confirmation/)).toBeInTheDocument()

    expect(screen.getByText(/approve import of 14 columns into source/i)).toBeInTheDocument()
  })

  it('surfaces a whole-table removal so the human sees the drop before approving', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    previewConnector.mockResolvedValue({
      ...PREVIEW,
      summary: { ...PREVIEW.summary, removed: 1 },
      tables: [
        ...PREVIEW.tables,
        {
          table: 'promotions', status: 'removed', columns: 2, quarantine: [],
          changes: ['no longer in the pull; import will drop this table and stale its 2 columns'],
        },
      ],
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    const stats = await screen.findByRole('group', { name: 'Preview summary' })
    expect(stats).toHaveTextContent('1 removed')
    const removed = screen.getByText('promotions').closest('li')
    if (!removed) throw new Error('promotions row not found')
    expect(within(removed).getByText('removed')).toBeInTheDocument()
    expect(removed).toHaveTextContent('import will drop this table and stale its 2 columns')
  })

  it('renders a fail-closed egress 400 calmly on the save form', async () => {
    listConnectors.mockResolvedValue([])
    createConnector.mockRejectedValue(new api.ApiError(400,
      'no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS'))
    renderPanel()
    await userEvent.type(screen.getByLabelText('Connection name'), 'cards om')
    await userEvent.type(screen.getByLabelText('OpenMetadata URL'), 'https://om.internal.test')
    await userEvent.type(screen.getByLabelText('Target source'), 'cards')
    await userEvent.click(screen.getByRole('button', { name: 'Save connection' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'no OpenMetadata hosts are allowlisted: set FEATUREGEN_OM_ALLOWED_HOSTS')
  })

  it('renders the would-hold brake with its reason', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    previewConnector.mockResolvedValue({
      ...PREVIEW,
      brake: { would_hold: true, reason: 'sync removes 8 of 10 known objects (80% > 30%)' },
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByText(/brake: this sync would be held/i)).toBeInTheDocument()
    expect(screen.getByText(/sync removes 8 of 10 known objects/)).toBeInTheDocument()
  })

  it('guards against double preview: the button disables while the pull is in flight', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    let release: (p: api.ConnectorPreview) => void = () => {}
    previewConnector.mockImplementation(
      () => new Promise<api.ConnectorPreview>(resolve => { release = resolve }))
    renderPanel()
    const button = await screen.findByRole('button', { name: 'Preview import' })
    await userEvent.click(button)
    expect(screen.getByRole('status')).toHaveTextContent(/running the dry run/i)
    expect(button).toBeDisabled()
    await userEvent.click(button)
    expect(previewConnector).toHaveBeenCalledTimes(1)
    release(PREVIEW)
    await screen.findByRole('heading', { name: 'Preview: cards om into source cards' })
    expect(button).toBeEnabled()
  })

  it('renders OM-unreachable calmly and touches nothing', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    previewConnector.mockRejectedValue(
      new api.ApiError(502, 'OpenMetadata request failed: connect timeout'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('OpenMetadata is unreachable.')
    expect(alert).toHaveTextContent('Nothing was touched.')
  })

  it('renders a rejected token as an auth problem, not a crash', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    previewConnector.mockRejectedValue(
      new api.ApiError(401, 'OpenMetadata rejected the bot token (401)'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'OpenMetadata rejected the connector token.')
  })

  it('renders the unconfigured-token 400 with the env-var instruction', async () => {
    listConnectors.mockResolvedValue([CONNECTOR])
    previewConnector.mockRejectedValue(new api.ApiError(400,
      'connector token is not configured: set the FEATUREGEN_OM_TOKEN__CARDS_OM environment variable'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'set the FEATUREGEN_OM_TOKEN__CARDS_OM environment variable')
  })
})

describe('remap', () => {
  it('a remap updates the config server-side and re-previews — never edits the payload client-side', async () => {
    const next: api.Connector = {
      ...CONNECTOR,
      connector_id: 'conn_01NEWCCCCCCCCCCCCCCCCCCCCC',
      tag_map: { 'PII.Sensitive': 'pii', 'Confidential.Internal': 'restricted' },
    }
    await renderWithPreview()
    deleteConnector.mockResolvedValue({ deleted: true })
    createConnector.mockResolvedValue(next)
    previewConnector.mockResolvedValue({
      ...PREVIEW,
      summary: { ...PREVIEW.summary, would_quarantine: 0 },
      tag_map: [
        { om_tag: 'Confidential.Internal', mapped_to: 'restricted', unmapped: false, count: 1 },
        { om_tag: 'PII.Sensitive', mapped_to: 'pii', unmapped: false, count: 2 },
      ],
      snapshot_hash: 'cd'.repeat(32),
    })

    await userEvent.selectOptions(
      screen.getByLabelText('Map Confidential.Internal'), 'restricted')

    // Config replaced (delete + recreate: v1 has no update endpoint), then the fresh dry run:
    // the remap select is gone because the fresh preview has no unmapped tag left.
    await waitFor(() =>
      expect(screen.queryByLabelText('Map Confidential.Internal')).not.toBeInTheDocument())
    const tagRow = screen.getByText('Confidential.Internal').closest('tr')
    if (!tagRow) throw new Error('tag row not found')
    expect(within(tagRow).getByText('restricted')).toBeInTheDocument()
    expect(within(tagRow).getByText('mapped')).toBeInTheDocument()
    expect(deleteConnector).toHaveBeenCalledExactlyOnceWith(CONNECTOR.connector_id)
    expect(createConnector).toHaveBeenCalledExactlyOnceWith({
      name: 'cards om',
      base_url: 'https://om.internal.test',
      target_source: 'cards',
      tag_map: { 'PII.Sensitive': 'pii', 'Confidential.Internal': 'restricted' },
      filters: { service: 'mysql_*', database: 'cards_db', schema: 'public' },
      table_naming: 'table',
      token_env: 'FEATUREGEN_OM_TOKEN__CARDS_OM',
    })
    expect(previewConnector).toHaveBeenLastCalledWith(next.connector_id)
  })

  it('remap to ignore sends the empty mapping', async () => {
    await renderWithPreview()
    deleteConnector.mockResolvedValue({ deleted: true })
    createConnector.mockResolvedValue(CONNECTOR)
    await userEvent.selectOptions(screen.getByLabelText('Map Confidential.Internal'), 'ignore')
    await waitFor(() => expect(createConnector).toHaveBeenCalled())
    expect(createConnector.mock.calls[0][0].tag_map).toEqual({
      'PII.Sensitive': 'pii',
      'Confidential.Internal': '',
    })
  })
})

describe('approve flow', () => {
  it('approval demands an explicit confirm; cancel backs out without importing', async () => {
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    expect(importConnector).not.toHaveBeenCalled()
    expect(screen.getByText(/will enter the catalog in one transaction/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeInTheDocument()
    expect(importConnector).not.toHaveBeenCalled()
  })

  it('confirm imports the exact previewed snapshot and hands off to the review queue', async () => {
    const onReviewQueue = vi.fn()
    importConnector.mockResolvedValue(IMPORT_OK)
    await renderWithPreview({ onReviewQueue })
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Ingested.')
    expect(status).toHaveTextContent('3 facts asserted, 0 staled, 1 quarantined')
    expect(status).toHaveTextContent(/first upload of 'cards'/)
    expect(importConnector).toHaveBeenCalledExactlyOnceWith(
      CONNECTOR.connector_id, SNAPSHOT_HASH)

    expect(screen.getByText('omimp_01HZYBBBBBBBBBBBBBBBBBBBBB')).toBeInTheDocument()
    expect(screen.getByText(/14 items now in the review queue for cards/i)).toBeInTheDocument()
    expect(screen.getByText(/13 semantics confirmations/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open review queue' }))
    expect(onReviewQueue).toHaveBeenCalledWith('cards')

    // The standard result callout's quarantine handoff works here too.
    await userEvent.click(screen.getByRole('button', { name: /review 1 quarantined row/i }))
    expect(onReviewQueue).toHaveBeenLastCalledWith('cards')
    expect(screen.getByRole('button', { name: 'Imported' })).toBeDisabled()
  })

  it('renders a held import with the connector-shaped advice, not file wording', async () => {
    importConnector.mockResolvedValue({
      result: {
        status: 'held', reason: 'overlap 20% < 60% (possible wrong source)',
        asserted: 0, staled: 0, quarantined: 0, flagged: null,
      },
      import_id: 'omimp_01HELD',
      review_queue: { quarantined: 0, semantics_pending: 0 },
    })
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    const held = await screen.findByRole('status')
    expect(held).toHaveTextContent(/held: this change removes too much/i)
    expect(held).toHaveTextContent(/overlap 20%/)
    expect(held).toHaveTextContent(/narrow the connector scope/i)
    expect(held).not.toHaveTextContent(/adjust the file/i)
  })

  it('a 409 renders the honest stale-preview notice and re-previews on request', async () => {
    importConnector.mockRejectedValue(new api.ApiError(409,
      'OpenMetadata changed since this preview (snapshot hash mismatch). '
        + 'Run preview again and approve the fresh dry run.'))
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/the preview went stale/i)
    expect(notice).toHaveTextContent(/snapshot hash mismatch/)
    expect(notice).toHaveTextContent(/nothing was imported/i)
    // The stale dry run is no longer approvable; only a fresh preview reopens the gate.
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeDisabled()

    previewConnector.mockResolvedValue({ ...PREVIEW, snapshot_hash: 'ef'.repeat(32) })
    await userEvent.click(screen.getByRole('button', { name: 'Run preview again' }))
    await screen.findByRole('heading', { name: 'Preview: cards om into source cards' })
    expect(previewConnector).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeEnabled()
  })

  it('shows a non-409 import failure calmly and keeps the preview', async () => {
    importConnector.mockRejectedValue(
      new api.ApiError(502, 'OpenMetadata request failed: gateway timeout'))
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('OpenMetadata is unreachable.')
    expect(
      screen.getByRole('heading', { name: 'Preview: cards om into source cards' }),
    ).toBeInTheDocument()
  })
})
