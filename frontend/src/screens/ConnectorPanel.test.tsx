import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ConnectorPanel } from './ConnectorPanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listIntegrations: vi.fn(),
    listSyncs: vi.fn(),
    previewSync: vi.fn(),
    importSync: vi.fn(),
    patchSync: vi.fn(),
  }
})
const listIntegrations = vi.mocked(api.listIntegrations)
const listSyncs = vi.mocked(api.listSyncs)
const previewSync = vi.mocked(api.previewSync)
const importSync = vi.mocked(api.importSync)
const patchSync = vi.mocked(api.patchSync)

beforeEach(() => {
  listIntegrations.mockReset()
  listSyncs.mockReset()
  previewSync.mockReset()
  importSync.mockReset()
  patchSync.mockReset()
  listIntegrations.mockResolvedValue([])
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

const SNAPSHOT_HASH = 'ab'.repeat(32)

// One of each table status, one quarantine subline, one unmapped + one mapped + one ignored tag:
// the full review surface from a single canned dry run.
const PREVIEW: api.SyncPreview = {
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
  collisions: [],
  dropped_joins: [],
  brake: { would_hold: false, reason: null },
  as_of_suggestions: [
    { table: 'accounts', column: 'opened_on', hint: 'partition column (TIME-UNIT)' },
    { table: 'transactions', column: 'posted_at', hint: 'timestamp column named like a time axis' },
  ],
  snapshot_hash: SNAPSHOT_HASH,
  local_baseline_hash: 'ef'.repeat(32),
}

const IMPORT_OK: api.SyncImportResult = {
  result: {
    status: 'ingested', reason: null, asserted: 3, changed_objects: 0, quarantined: 1,
    flagged: "first upload of 'cards' (13 objects) — review recommended",
  },
  import_id: 'omimp_01HZYBBBBBBBBBBBBBBBBBBBBB',
  semantics_pending: 13,
}

function renderPanel(over: {
  onReviewQueue?: (s: string) => void
  onSemanticsQueue?: (s: string) => void
  onStage?: (s: string) => void
  onManageIntegrations?: () => void
} = {}) {
  render(
    <ConnectorPanel
      onReviewQueue={over.onReviewQueue ?? (() => {})}
      onSemanticsQueue={over.onSemanticsQueue ?? (() => {})}
      onStage={over.onStage ?? (() => {})}
      onManageIntegrations={over.onManageIntegrations ?? (() => {})}
    />,
  )
}

// Arranges the mocks BEFORE render (the panel lists integrations + syncs on mount), then walks to
// a rendered preview of the first (auto-selected) sync.
async function renderWithPreview(over: {
  onReviewQueue?: (s: string) => void
  onSemanticsQueue?: (s: string) => void
} = {}) {
  listIntegrations.mockResolvedValue([INTEGRATION])
  listSyncs.mockResolvedValue([SYNC])
  previewSync.mockResolvedValue(PREVIEW)
  renderPanel(over)
  await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
  await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })
}

describe('sync picker', () => {
  it('groups syncs under their integration and previews the auto-selected one', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockResolvedValue(PREVIEW)
    renderPanel()

    // The optgroup carries the integration name; the option names the service and target source.
    expect(await screen.findByRole('group', { name: 'Corporate OpenMetadata' })).toBeInTheDocument()
    expect(
      screen.getByRole('option', { name: 'mysql_prod (cards_db.public) → source cards' }),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })
    expect(previewSync).toHaveBeenCalledExactlyOnceWith(SYNC.sync_id)
  })

  it('shows an empty state with a link to Integrations when no syncs are configured', async () => {
    const onManageIntegrations = vi.fn()
    listIntegrations.mockResolvedValue([])
    renderPanel({ onManageIntegrations })
    expect(await screen.findByText('No syncs configured.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Go to Integrations' }))
    expect(onManageIntegrations).toHaveBeenCalledTimes(1)
  })

  it('the inline Integrations link also navigates to the integrations screen', async () => {
    const onManageIntegrations = vi.fn()
    renderPanel({ onManageIntegrations })
    await userEvent.click(await screen.findByRole('button', { name: 'Integrations' }))
    expect(onManageIntegrations).toHaveBeenCalledTimes(1)
  })
})

describe('preview rendering', () => {
  it('renders the full dry run: stats, brake, tag map, tables, quarantine, as-of, pending', async () => {
    await renderWithPreview()
    expect(previewSync).toHaveBeenCalledExactlyOnceWith(SYNC.sync_id)

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
    // Honest copy (#25): pending semantics are an informational count, never a claimed queue.
    expect(screen.getByText(/nothing is routed to a review queue/)).toBeInTheDocument()

    expect(screen.getByText(/approve import of 14 columns into source/i)).toBeInTheDocument()
  })

  it('surfaces a whole-table removal so the human sees the drop before approving', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockResolvedValue({
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

  it('warns about collision-excluded tables and dropped FK joins before approve (#1)', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockResolvedValue({
      ...PREVIEW,
      collisions: [{
        table: 'orders',
        fqns: ['mysql_prod.cards_db.public.orders', 'mysql_prod.cards_db.audit.orders'],
      }],
      dropped_joins: [{
        table: 'accounts', columns: ['branch_id', 'region_id'],
        referred: ['branches.branch_id', 'branches.region_id'],
        reason: 'composite foreign key not supported',
      }],
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })

    // Both losses named, with their specifics, so approval is informed — never silent.
    expect(screen.getByText(/1 table excluded \(name collision\)/i)).toBeInTheDocument()
    expect(screen.getByText('orders')).toBeInTheDocument()
    expect(screen.getByText(
      /mysql_prod\.cards_db\.public\.orders, mysql_prod\.cards_db\.audit\.orders/,
    )).toBeInTheDocument()
    expect(screen.getByText(/held out of the import, never silently merged/)).toBeInTheDocument()
    expect(screen.getByText(/1 foreign-key relationship dropped/i)).toBeInTheDocument()
    expect(screen.getByText(/accounts\(branch_id, region_id\)/)).toBeInTheDocument()
    expect(screen.getByText(/composite foreign key not supported/)).toBeInTheDocument()
    expect(screen.getByText(/will not exist in the catalog after import/)).toBeInTheDocument()
  })

  it('renders no data-loss warning on a clean pull', async () => {
    await renderWithPreview()   // PREVIEW carries empty collisions/dropped_joins
    expect(screen.queryByText(/excluded \(name collision\)/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/foreign-key relationship/i)).not.toBeInTheDocument()
  })

  it('renders the would-hold brake with its reason', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockResolvedValue({
      ...PREVIEW,
      brake: { would_hold: true, reason: 'sync removes 8 of 10 known objects (80% > 30%)' },
    })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByText(/brake: this sync would be held/i)).toBeInTheDocument()
    expect(screen.getByText(/sync removes 8 of 10 known objects/)).toBeInTheDocument()
  })

  it('guards against double preview: the button disables while the pull is in flight', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    let release: (p: api.SyncPreview) => void = () => {}
    previewSync.mockImplementation(
      () => new Promise<api.SyncPreview>(resolve => { release = resolve }))
    renderPanel()
    const button = await screen.findByRole('button', { name: 'Preview import' })
    await userEvent.click(button)
    expect(screen.getByRole('status')).toHaveTextContent(/running the dry run/i)
    expect(button).toBeDisabled()
    await userEvent.click(button)
    expect(previewSync).toHaveBeenCalledTimes(1)
    release(PREVIEW)
    await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })
    expect(button).toBeEnabled()
  })

  it('renders OM-unreachable calmly and touches nothing', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockRejectedValue(
      new api.ApiError(502, 'OpenMetadata request failed: connect timeout'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('OpenMetadata is unreachable.')
    expect(alert).toHaveTextContent('Nothing was touched.')
  })

  it('renders a rejected token as an auth problem, not a crash', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockRejectedValue(
      new api.ApiError(401, 'OpenMetadata rejected the bot token (401)'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'OpenMetadata rejected the connector token.')
  })

  it('renders the unconfigured-token 400 with the env-var instruction', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockRejectedValue(new api.ApiError(400,
      'integration token is not configured: set the FEATUREGEN_OM_TOKEN__CORP environment variable'))
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'set the FEATUREGEN_OM_TOKEN__CORP environment variable')
  })
})

describe('remap', () => {
  it('a remap PATCHes the sync override and re-previews — never edits the payload client-side', async () => {
    const updated: api.Sync = {
      ...SYNC,
      tag_map_override: { 'Confidential.Internal': 'restricted' },
    }
    await renderWithPreview()
    patchSync.mockResolvedValue(updated)
    previewSync.mockResolvedValue({
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

    // Override PATCHed, then the fresh dry run: the remap select is gone because the fresh preview
    // has no unmapped tag left.
    await waitFor(() =>
      expect(screen.queryByLabelText('Map Confidential.Internal')).not.toBeInTheDocument())
    const tagRow = screen.getByText('Confidential.Internal').closest('tr')
    if (!tagRow) throw new Error('tag row not found')
    expect(within(tagRow).getByText('restricted')).toBeInTheDocument()
    expect(within(tagRow).getByText('mapped')).toBeInTheDocument()
    expect(patchSync).toHaveBeenCalledExactlyOnceWith(
      INTEGRATION.integration_id, SYNC.sync_id,
      { tag_map_override: { 'Confidential.Internal': 'restricted' } })
    expect(previewSync).toHaveBeenLastCalledWith(updated.sync_id)
  })

  it('remap to ignore sends the empty mapping', async () => {
    await renderWithPreview()
    patchSync.mockResolvedValue({ ...SYNC, tag_map_override: { 'Confidential.Internal': '' } })
    await userEvent.selectOptions(screen.getByLabelText('Map Confidential.Internal'), 'ignore')
    await waitFor(() => expect(patchSync).toHaveBeenCalled())
    expect(patchSync.mock.calls[0][2]).toEqual({
      tag_map_override: { 'Confidential.Internal': '' },
    })
  })
})

describe('approve flow', () => {
  it('approval demands an explicit confirm; cancel backs out without importing', async () => {
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    expect(importSync).not.toHaveBeenCalled()
    expect(screen.getByText(/will enter the catalog in one transaction/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeInTheDocument()
    expect(importSync).not.toHaveBeenCalled()
  })

  it('confirm imports the exact previewed hashes; semantics pending links to its queue', async () => {
    const onReviewQueue = vi.fn()
    const onSemanticsQueue = vi.fn()
    importSync.mockResolvedValue(IMPORT_OK)
    await renderWithPreview({ onReviewQueue, onSemanticsQueue })
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Ingested.')
    expect(status).toHaveTextContent('3 facts asserted, 0 objects changed, 1 quarantined')
    expect(status).toHaveTextContent(/first upload of 'cards'/)
    expect(importSync).toHaveBeenCalledExactlyOnceWith(
      SYNC.sync_id, SNAPSHOT_HASH, PREVIEW.local_baseline_hash)

    expect(screen.getByText('omimp_01HZYBBBBBBBBBBBBBBBBBBBBB')).toBeInTheDocument()
    // The pending count now hands off to the semantics-pending queue (#22) for the sync's
    // TARGET source; the quarantine handoff (its own queue) stays on the result callout.
    expect(screen.getByText(/13 columns need owner confirmation/i)).toBeInTheDocument()
    expect(screen.queryByText(/now in the review queue/i)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Complete semantics' }))
    expect(onSemanticsQueue).toHaveBeenCalledExactlyOnceWith('cards')

    await userEvent.click(screen.getByRole('button', { name: /review 1 quarantined row/i }))
    expect(onReviewQueue).toHaveBeenCalledExactlyOnceWith('cards')
    expect(screen.getByRole('button', { name: 'Imported' })).toBeDisabled()
  })

  it('renders a held import with the sync-shaped advice, not file wording', async () => {
    importSync.mockResolvedValue({
      result: {
        status: 'held', reason: 'overlap 20% < 60% (possible wrong source)',
        asserted: 0, changed_objects: 0, quarantined: 0, flagged: null,
      },
      import_id: 'omimp_01HELD',
      semantics_pending: 0,
    })
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    const held = await screen.findByRole('status')
    expect(held).toHaveTextContent(/held: this change removes too much/i)
    expect(held).toHaveTextContent(/overlap 20%/)
    expect(held).toHaveTextContent(/narrow the sync scope/i)
    expect(held).not.toHaveTextContent(/adjust the file/i)
  })

  it('a 409 renders the honest stale-preview notice and re-previews on request', async () => {
    importSync.mockRejectedValue(new api.ApiError(409,
      'OpenMetadata changed since this preview (snapshot hash mismatch). '
        + 'Run preview again and approve the fresh dry run.'))
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/the preview went stale/i)
    expect(notice).toHaveTextContent(/OpenMetadata or the local catalog changed/i)
    expect(notice).toHaveTextContent(/nothing was imported/i)
    // The stale dry run is no longer approvable; only a fresh preview reopens the gate.
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeDisabled()

    previewSync.mockResolvedValue({ ...PREVIEW, snapshot_hash: 'ef'.repeat(32) })
    await userEvent.click(screen.getByRole('button', { name: 'Run preview again' }))
    await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })
    expect(previewSync).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'Approve import' })).toBeEnabled()
  })

  it('shows a non-409 import failure calmly and keeps the preview', async () => {
    importSync.mockRejectedValue(
      new api.ApiError(502, 'OpenMetadata request failed: gateway timeout'))
    await renderWithPreview()
    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('OpenMetadata is unreachable.')
    expect(
      screen.getByRole('heading', { name: 'Preview: mysql_prod into source cards' }),
    ).toBeInTheDocument()
  })
})
