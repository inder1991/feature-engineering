import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { UploadScreen } from './UploadScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    uploadFile: vi.fn(),
    listIntegrations: vi.fn(),
    listSyncs: vi.fn(),
    previewSync: vi.fn(),
    importSync: vi.fn(),
    getIngestionRun: vi.fn(),
  }
})
const uploadFile = vi.mocked(api.uploadFile)
const listIntegrations = vi.mocked(api.listIntegrations)
const listSyncs = vi.mocked(api.listSyncs)
const previewSync = vi.mocked(api.previewSync)
const importSync = vi.mocked(api.importSync)
const getIngestionRun = vi.mocked(api.getIngestionRun)

// Block body (not `() => uploadFile.mockReset()`): mockReset() returns the mock fn, and Vitest
// treats a function returned from beforeEach as a per-test teardown — it would then call the mock
// after each test, producing an unawaited rejected promise (unhandled rejection) in the reject case.
beforeEach(() => {
  uploadFile.mockReset()
  listIntegrations.mockReset()
  listSyncs.mockReset()
  previewSync.mockReset()
  importSync.mockReset()
  getIngestionRun.mockReset()
  listIntegrations.mockResolvedValue([])
  listSyncs.mockResolvedValue([])
})

const result = (over: Partial<api.IngestResult>): api.IngestResult => ({
  status: 'ingested', reason: null, asserted: 0, changed_objects: 0, quarantined: 0, flagged: null, ...over })

function renderUpload(over: {
  onReviewQueue?: (s: string) => void
  onSemanticsQueue?: (s: string) => void
  onManageIntegrations?: () => void
} = {}) {
  render(
    <UploadScreen
      onReviewQueue={over.onReviewQueue ?? (() => {})}
      onSemanticsQueue={over.onSemanticsQueue ?? (() => {})}
      onManageIntegrations={over.onManageIntegrations ?? (() => {})}
    />,
  )
}

async function submit(source = 'deposits') {
  await userEvent.type(screen.getByLabelText(/source name/i), source)
  await userEvent.upload(
    screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
  await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
}

describe('upload screen', () => {
  it('shows the ingest summary with the first-upload flag', async () => {
    uploadFile.mockResolvedValue(result({
      asserted: 4, changed_objects: 1,
      flagged: "first upload of 'deposits' (9 objects) — review recommended" }))
    renderUpload()
    await submit()
    // Counts are wrapped in semantic-color spans; assert the full line via the status container,
    // which also pins the callout's role=status announcement contract.
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('4 facts asserted, 1 objects changed, 0 quarantined')
    expect(status).toHaveTextContent(/first upload of 'deposits'/)
  })

  it('shows the chosen filename in the drop target', async () => {
    renderUpload()
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'deposits-q3.csv', { type: 'text/csv' }))
    expect(screen.getByText('deposits-q3.csv')).toBeInTheDocument()
  })

  it('renders held as a brake with the reason, not an error', async () => {
    uploadFile.mockResolvedValue(result({
      status: 'held', reason: 'overlap 20% < 60% (possible wrong source)' }))
    renderUpload()
    await submit()
    const held = await screen.findByRole('status')
    expect(held).toHaveTextContent(/held: this change removes too much of the existing catalog/i)
    expect(held).toHaveTextContent(/overlap 20%/)
    expect(held).toHaveTextContent(/nothing was applied/i)
    expect(held).toHaveTextContent(/no override yet/i)
    // The backend has no confirm path: an identical re-upload is held again. The copy must not
    // promise one.
    expect(held).not.toHaveTextContent(/re-upload/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders rejected with the structural reason', async () => {
    uploadFile.mockResolvedValue(result({ status: 'rejected', reason: 'empty upload: no rows' }))
    renderUpload()
    await submit()
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent(/rejected/i)
    expect(status).toHaveTextContent(/empty upload: no rows/)
  })

  it('links quarantined rows to the review queue', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4, quarantined: 3 }))
    const onReviewQueue = vi.fn()
    renderUpload({ onReviewQueue })
    await submit()
    await userEvent.click(
      await screen.findByRole('button', { name: /review 3 quarantined rows/i }))
    expect(onReviewQueue).toHaveBeenCalledWith('deposits')
  })

  it('hands off the uploaded source even after the input is edited for the next upload', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4, quarantined: 3 }))
    const onReviewQueue = vi.fn()
    renderUpload({ onReviewQueue })
    await submit()
    const input = screen.getByLabelText(/source name/i)
    await userEvent.clear(input)
    await userEvent.type(input, 'x')
    await userEvent.click(
      await screen.findByRole('button', { name: /review 3 quarantined rows/i }))
    expect(onReviewQueue).toHaveBeenCalledWith('deposits')
  })

  it('rejects a dropped file with an unsupported extension before any request', async () => {
    renderUpload()
    const dropZone = screen.getByLabelText(/file/i).closest('label')
    if (!dropZone) throw new Error('drop zone label not found')
    fireEvent.drop(dropZone, { dataTransfer: { files: [new File(['x'], 'export.bak')] } })
    expect(await screen.findByRole('alert')).toHaveTextContent(/unsupported file type/i)
    expect(screen.queryByText('export.bak')).not.toBeInTheDocument()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('rejects a file over 25 MiB (the backend cap) before any request', async () => {
    renderUpload()
    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    const big = new File(['x'], 'big.csv', { type: 'text/csv' })
    Object.defineProperty(big, 'size', { value: 25 * 1024 * 1024 + 1 })
    await userEvent.upload(screen.getByLabelText(/file/i), big)
    expect(await screen.findByRole('alert')).toHaveTextContent(/25 MiB/)
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('shows transport errors as an alert', async () => {
    uploadFile.mockRejectedValue(
      new api.ApiError(400, 'unsupported file type (expected .csv, .xlsx, or .xlsm)'),
    )
    renderUpload()
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(/unsupported file type/)
    // No X-Ingestion-Run-Id header rode the error: there is no run to inspect, so no link.
    expect(screen.queryByRole('button', { name: /run details/i })).toBeNull()
  })

  // A failed upload still opened a run record (#14): the ApiError carries the run id from the
  // X-Ingestion-Run-Id header, and the error callout must open the run-detail panel for it.
  it('a failed upload with a run id offers "View run details" and opens the panel', async () => {
    uploadFile.mockRejectedValue(
      new api.ApiError(500, 'ingest failed', 'run-failed-1'),
    )
    getIngestionRun.mockResolvedValue({
      id: 'run-failed-1', origin_type: 'upload', catalog_source: 'deposits',
      filename: 'd.csv', actor_subject: 'user:o', actor_role_claims: ['data_owner'],
      authorization_decision: 'permitted', status: 'failed', row_count: null,
      quarantined_count: null, started_at: '2026-07-16T09:00:00+00:00', completed_at: null,
      redacted_failure_code: 'FACT_ASSERTION_ERROR',
      status_history: [
        { status: 'opened', at: '2026-07-16T09:00:00+00:00', reason_code: null },
        { status: 'failed', at: '2026-07-16T09:00:01+00:00',
          reason_code: 'FACT_ASSERTION_ERROR' },
      ],
      stages: [
        { stage: 'parse', attempt: 1, state: 'succeeded', reason_code: null, detail: null,
          started_at: null, completed_at: null },
        { stage: 'fact_assertion', attempt: 1, state: 'failed',
          reason_code: 'FACT_ASSERTION_ERROR', detail: null,
          started_at: null, completed_at: null },
      ],
    })
    renderUpload()
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(/upload failed/i)
    await userEvent.click(screen.getByRole('button', { name: 'View run details' }))
    expect(getIngestionRun).toHaveBeenCalledExactlyOnceWith('run-failed-1')
    const panel = await screen.findByRole('region', { name: /ingestion run details/i })
    expect(panel).toHaveTextContent('FACT_ASSERTION_ERROR')
    await userEvent.click(screen.getByRole('button', { name: 'Hide run details' }))
    expect(screen.queryByRole('region', { name: /ingestion run details/i })).toBeNull()
  })
})

// ---------------------------------------------------------------- the two ingest paths + gates

const INTEGRATION: api.Integration = {
  integration_id: 'intg_01HZXAAAAAAAAAAAAAAAAAAAAA',
  name: 'Corporate OpenMetadata',
  base_url: 'https://om.internal.test',
  token_env: 'FEATUREGEN_OM_TOKEN__CORP',
  tag_map: {},
  created_by: 'user:o',
  created_at: '2026-07-09T12:00:00+00:00',
  token_present: true,
}

const SYNC: api.Sync = {
  sync_id: 'sync_01HZYBBBBBBBBBBBBBBBBBBBBB',
  integration_id: INTEGRATION.integration_id,
  service_name: 'mysql_prod',
  database_filter: null,
  schema_filter: 'public',
  target_source: 'cards',
  tag_map_override: null,
  table_naming: 'table',
  created_by: 'user:o',
  created_at: '2026-07-09T12:05:00+00:00',
  last_import_at: null,
}

const PREVIEW: api.SyncPreview = {
  summary: {
    tables: 1, columns: 3, new: 1, changed: 0, unchanged: 0, removed: 0,
    would_quarantine: 0, semantics_pending: 3,
  },
  tag_map: [],
  tables: [{ table: 'accounts', status: 'new', columns: 3, quarantine: [], changes: [] }],
  collisions: [],
  dropped_joins: [],
  brake: { would_hold: false, reason: null },
  as_of_suggestions: [],
  snapshot_hash: 'ab'.repeat(32),
  local_baseline_hash: 'ef'.repeat(32),
}

function gateStates(): string[] {
  const strip = screen.getByRole('list', { name: /connector path/i })
  return within(strip)
    .getAllByRole('listitem')
    .map(g => g.getAttribute('data-state') ?? '')
}

describe('ingest paths', () => {
  it('renders the file path by default; the sync path reveals the picker and back', async () => {
    renderUpload()
    // File flow visible, no connector traffic yet (the panel mounts lazily).
    expect(screen.getByLabelText(/source name/i)).toBeVisible()
    expect(listIntegrations).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /pull from a metadata service/i }))
    expect(
      await screen.findByRole('heading', { name: 'Pull from a metadata service' }),
    ).toBeVisible()
    expect(listIntegrations).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText(/source name/i)).not.toBeVisible()

    // Back to the file path: the upload form returns, the sync panel stays mounted (hidden, so out
    // of the accessibility tree) and its state survives the toggle.
    await userEvent.click(screen.getByRole('button', { name: /upload a schema and facts file/i }))
    expect(screen.getByLabelText(/source name/i)).toBeVisible()
    expect(
      screen.getByRole('heading', { name: 'Pull from a metadata service', hidden: true }),
    ).not.toBeVisible()
    expect(listIntegrations).toHaveBeenCalledTimes(1)
  })

  it('walks the gates strip through the sync loop: pick -> review -> approve -> done', async () => {
    listIntegrations.mockResolvedValue([INTEGRATION])
    listSyncs.mockResolvedValue([SYNC])
    previewSync.mockResolvedValue(PREVIEW)
    importSync.mockResolvedValue({
      result: { status: 'ingested', reason: null, asserted: 0, changed_objects: 0, quarantined: 0, flagged: null },
      import_id: 'omimp_01HZY',
      semantics_pending: 3,
    })
    renderUpload()
    expect(gateStates()).toEqual(['active', 'todo', 'todo', 'todo'])

    await userEvent.click(screen.getByRole('button', { name: /pull from a metadata service/i }))
    // The first sync auto-selects; preview it.
    await userEvent.click(await screen.findByRole('button', { name: 'Preview import' }))
    await screen.findByRole('heading', { name: 'Preview: mysql_prod into source cards' })
    expect(gateStates()).toEqual(['done', 'done', 'active', 'todo'])

    await userEvent.click(screen.getByRole('button', { name: 'Approve import' }))
    expect(gateStates()).toEqual(['done', 'done', 'done', 'active'])

    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    await screen.findByRole('status')
    expect(gateStates()).toEqual(['done', 'done', 'done', 'done'])
  })

  it('the gates strip only tracks the sync path: a file upload leaves it untouched', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await submit()
    await screen.findByRole('status')
    expect(gateStates()).toEqual(['active', 'todo', 'todo', 'todo'])
  })
})
