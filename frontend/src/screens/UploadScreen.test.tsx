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
    listCatalogs: vi.fn(),
    getCatalogProfile: vi.fn(),
  }
})
const uploadFile = vi.mocked(api.uploadFile)
const listIntegrations = vi.mocked(api.listIntegrations)
const listSyncs = vi.mocked(api.listSyncs)
const previewSync = vi.mocked(api.previewSync)
const importSync = vi.mocked(api.importSync)
const getIngestionRun = vi.mocked(api.getIngestionRun)
const listCatalogs = vi.mocked(api.listCatalogs)
const getCatalogProfile = vi.mocked(api.getCatalogProfile)

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
  listCatalogs.mockReset()
  getCatalogProfile.mockReset()
  listIntegrations.mockResolvedValue([])
  listSyncs.mockResolvedValue([])
  listCatalogs.mockResolvedValue({ catalogs: [] })
  // The default is the flag-off / unknown-catalog answer: the narrative routes 404 while
  // FEATUREGEN_DATASET_PROFILES is off, and 404 for a catalog the caller cannot see. Every test
  // that is not ABOUT the prefill therefore runs against a screen that found nothing to prefill.
  getCatalogProfile.mockRejectedValue(new api.ApiError(404, 'Not Found'))
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

// ---------------------------------------------------------------- the catalog narrative section

// The narrative used to be a raw-JSON textarea. It is now structured fields, and the ONE thing
// that must not have changed is the wire: the multipart `catalog_profile_json` part the server
// parses (overlay/upload/catalog_profiles.parse_narrative_payload).

async function openNarrative() {
  await userEvent.click(screen.getByText('Describe this catalog (recommended)'))
}

// The part `uploadFile` was last called with — the third argument, exactly as it goes on the wire.
function sentPart(): string | undefined {
  return uploadFile.mock.calls.at(-1)?.[2]
}

describe('catalog narrative section', () => {
  it('is closed by default, is framed as recommended, and never gates the upload button', async () => {
    renderUpload()
    const summary = screen.getByText('Describe this catalog (recommended)')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    // Nothing is fetched for a section nobody opened.
    expect(listCatalogs).not.toHaveBeenCalled()

    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()

    await openNarrative()
    // Opening it — and leaving it untouched — changes nothing about the upload.
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()
    expect(screen.getByText(/shown as the catalog's name and description/i)).toBeInTheDocument()
    expect(listCatalogs).toHaveBeenCalledTimes(1)
  })

  it('serializes the fields to the SAME part the raw-JSON textarea used to send', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText('Name'), 'Funds transfers')
    await userEvent.type(screen.getByLabelText('Description'), 'Outbound payments.')
    await userEvent.type(
      screen.getByLabelText('Business context'), 'Core banking; Compliance-owned.')
    await userEvent.type(screen.getByLabelText('Business domains'), 'Payments{Enter}')
    await submit()

    // SERIALIZATION EQUIVALENCE. `legacy` is what a person hand-wrote into the old textarea for
    // this same content and what the old screen posted verbatim. The comparison is on the parsed
    // objects because JSON.stringify emits no whitespace: the server json.loads() the part, so
    // equal parses are the same request — same keys, same values, same types, nothing extra.
    const legacy = '{"display_name": "Funds transfers", "description": "Outbound payments.", '
      + '"business_context": "Core banking; Compliance-owned.", "business_domains": ["Payments"]}'
    expect(JSON.parse(sentPart() as string)).toEqual(JSON.parse(legacy))
  })

  it('omits the part entirely when every field is empty — absence stays absence', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    // Whitespace is not authorship: a present-but-empty `{}` part would make the server write a
    // narrative revision made of nothing, so it must never be sent.
    await userEvent.type(screen.getByLabelText('Name'), '   ')
    await submit()
    expect(uploadFile).toHaveBeenLastCalledWith(expect.any(File), 'deposits', undefined)
  })

  it('adds typed and suggested domains as chips, and removes them', async () => {
    listCatalogs.mockResolvedValue({ catalogs: [
      { source: 'cust', tables: 2, columns: 9, display_name: 'Customer', has_profile: true },
      { source: 'compliance', tables: 1, columns: 4, display_name: null, has_profile: false },
    ] })
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()

    // Suggestions are the catalogs this caller can already see: a described one by its name, an
    // undescribed one by its upper-cased slug (there is no per-catalog label to invent).
    await screen.findByRole('button', { name: 'Add domain Customer' })
    await userEvent.click(screen.getByRole('button', { name: 'Add domain COMPLIANCE' }))
    // A chosen suggestion stops being offered, and is now a removable chip.
    expect(screen.queryByRole('button', { name: 'Add domain COMPLIANCE' })).toBeNull()
    await userEvent.type(screen.getByLabelText('Business domains'), 'Payments{Enter}')
    await submit()
    expect(JSON.parse(sentPart() as string))
      .toEqual({ business_domains: ['COMPLIANCE', 'Payments'] })

    await userEvent.click(screen.getByRole('button', { name: 'Remove domain COMPLIANCE' }))
    expect(screen.getByRole('button', { name: 'Add domain COMPLIANCE' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(JSON.parse(sentPart() as string)).toEqual({ business_domains: ['Payments'] })
  })

  it('keeps text typed into the domain box that was never Entered', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    // No Enter: the box is blurred by moving on to the rest of the form. Dropping it silently
    // would lose a domain the author watched themselves type.
    await userEvent.type(screen.getByLabelText('Business domains'), 'Payments')
    await submit()
    expect(JSON.parse(sentPart() as string)).toEqual({ business_domains: ['Payments'] })
  })

  it('counts characters against the server bound and refuses the upload once over it', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    await openNarrative()

    const name = screen.getByLabelText('Name')
    // Paste, not type: 201 keystrokes is 201 renders.
    await userEvent.click(name)
    await userEvent.paste('n'.repeat(200))
    expect(screen.getByText('200 / 200')).toBeInTheDocument()
    // Exactly AT the bound is fine — a mirror that refuses what the server takes is worse than
    // no mirror at all.
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()

    await userEvent.paste('n')
    // The server's own wording, so the text a person fixes is the text the 400 would have carried.
    expect(screen.getByText('display_name exceeds the 200-character bound')).toBeInTheDocument()
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled()
    expect(screen.getByText(/shorten the catalog description above/i)).toBeInTheDocument()
    expect(uploadFile).not.toHaveBeenCalled()

    // Back under the bound: nothing is left blocked.
    await userEvent.clear(name)
    await userEvent.paste('Deposits')
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(JSON.parse(sentPart() as string)).toEqual({ display_name: 'Deposits' })
  })

  it('blocks on the 64 KiB whole-part bound while every field is under its own', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    await openNarrative()

    // Text pasted out of a mainframe extract, field separators and all. U+0001 is one character to
    // both bounds checks, but JSON escapes it to the six ASCII bytes \u0001 — so 4000 + 4000
    // IN-BOUND characters already serialize to ~48 KiB, and a dozen domains carry it over 64 KiB.
    // No per-field counter can see this; only the whole-part check can.
    const SEP = '\u0001'
    await userEvent.click(screen.getByLabelText('Description'))
    await userEvent.paste(SEP.repeat(4000))
    await userEvent.click(screen.getByLabelText('Business context'))
    await userEvent.paste(SEP.repeat(4000))
    const overBound = () => screen.queryByText('catalog_profile_json exceeds the 64 KiB bound')
    const domains = screen.getByLabelText('Business domains')
    // Up to the 32-item bound, so this loop can never run away — and stops the moment the whole
    // part crosses, which is the thing being asserted.
    for (let i = 0; i < 32 && !overBound(); i++) {
      await userEvent.click(domains)
      await userEvent.paste(`${i}${SEP.repeat(199)}`)
      await userEvent.keyboard('{Enter}')
    }
    expect(overBound()).toBeInTheDocument()
    // Every individual field is still inside its own bound: this is the only failing check.
    expect(screen.queryByText(/exceeds the 4000-character bound/)).toBeNull()
    expect(screen.queryByText(/exceeds the 32-item bound/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('counts CODE POINTS, as the server does, not UTF-16 units', async () => {
    renderUpload()
    await openNarrative()
    // 2000 astral characters are 4000 UTF-16 units but 2000 characters to Python's len() — the
    // naive .length mirror would refuse here what the server accepts.
    await userEvent.click(screen.getByLabelText('Description'))
    await userEvent.paste('\u{1F3E6}'.repeat(2000))
    expect(screen.getByText('2000 / 4000')).toBeInTheDocument()
    expect(screen.queryByText(/exceeds the 4000-character bound/)).toBeNull()
  })

  it('survives a FAILED file upload, so the retry keeps the typing', async () => {
    uploadFile.mockRejectedValue(new api.ApiError(400, 'unsupported file type'))
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText('Name'), 'Funds transfers')
    await userEvent.type(
      screen.getByLabelText('Business context'), 'Core banking; Compliance-owned.')
    await userEvent.type(screen.getByLabelText('Business domains'), 'Payments{Enter}')
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(/upload failed/i)

    // The file is what failed. The paragraph about the catalog — which only this person could
    // write, and only they have — is still there.
    expect(screen.getByLabelText('Name')).toHaveValue('Funds transfers')
    expect(screen.getByLabelText('Business context'))
      .toHaveValue('Core banking; Compliance-owned.')
    expect(screen.getByRole('button', { name: 'Remove domain Payments' })).toBeInTheDocument()

    // …and the retry carries it, unchanged and unretyped.
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(JSON.parse(sentPart() as string)).toEqual({
      display_name: 'Funds transfers',
      business_context: 'Core banking; Compliance-owned.',
      business_domains: ['Payments'],
    })
  })

  it('an EMPTY section never blocks, however the bounds are set', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    await submit()
    expect(uploadFile).toHaveBeenLastCalledWith(expect.any(File), 'deposits', undefined)
  })

  it('a suggestion list that cannot be read leaves free-text entry working, with no error', async () => {
    listCatalogs.mockRejectedValue(new api.ApiError(500, 'boom'))
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    expect(screen.queryByRole('alert')).toBeNull()
    await userEvent.type(screen.getByLabelText('Business domains'), 'Payments{Enter}')
    await submit()
    expect(JSON.parse(sentPart() as string)).toEqual({ business_domains: ['Payments'] })
  })
})

// ---------------------------------------------------------------- re-upload prefill

const DESCRIBED: api.CatalogProfile = {
  source: 'ftr',
  pointer_version: 3,
  profile: {
    catalog_source: 'ftr',
    display_name: 'FTR compliance glossary',
    description: 'Financial transaction reporting terms.',
    business_context: 'Owned by financial crime operations.',
    business_domains: ['Compliance'],
    producer: 'human',
    strength: 'confirmed',
    lifecycle: 'active',
    producer_ref: 'user:priya',
    ingestion_run_id: null,
    content_hash: 'a'.repeat(64),
    revision_id: `cpr_${'a'.repeat(64)}`,
  },
}

describe('re-uploading into a catalog somebody has already described', () => {
  it('prefills the current narrative, and says so without claiming a change', async () => {
    getCatalogProfile.mockResolvedValue(DESCRIBED)
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')

    expect(await screen.findByDisplayValue('FTR compliance glossary')).toBeInTheDocument()
    expect(screen.getByLabelText('Description'))
      .toHaveValue('Financial transaction reporting terms.')
    expect(screen.getByLabelText('Business context'))
      .toHaveValue('Owned by financial crime operations.')
    expect(screen.getByRole('button', { name: 'Remove domain Compliance' })).toBeInTheDocument()
    // TRUTHFUL: the store keys a revision by a content hash over these values, so re-sending the
    // identical words resolves to the same revision id and writes no new version.
    expect(screen.getByTestId('narrative-unchanged'))
      .toHaveTextContent(/unchanged — nothing will be re-versioned/i)
  })

  it('drops the unchanged note the moment a single word changes', async () => {
    getCatalogProfile.mockResolvedValue(DESCRIBED)
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await screen.findByTestId('narrative-unchanged')

    await userEvent.type(screen.getByLabelText('Description'), ' Updated.')
    expect(screen.queryByTestId('narrative-unchanged')).toBeNull()
  })

  it('is debounced: one lookup for a typed name, not one per keystroke', async () => {
    getCatalogProfile.mockResolvedValue(DESCRIBED)
    renderUpload()
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await vi.waitFor(() => expect(getCatalogProfile).toHaveBeenCalled())
    expect(getCatalogProfile).toHaveBeenCalledExactlyOnceWith('ftr')
  })

  it('never writes over words the author has already typed', async () => {
    getCatalogProfile.mockResolvedValue(DESCRIBED)
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText('Description'), 'My own words.')
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await vi.waitFor(() => expect(getCatalogProfile).toHaveBeenCalledWith('ftr'))

    // The draft only this person has survives the lookup — the CatalogNarrativePanel 409 rule.
    expect(screen.getByLabelText('Description')).toHaveValue('My own words.')
    expect(screen.getByLabelText('Name')).toHaveValue('')
    // …and nothing claims their words are the catalog's current ones.
    expect(screen.queryByTestId('narrative-unchanged')).toBeNull()
  })

  it('degrades silently to an empty form when the narrative cannot be read', async () => {
    // 404 while the profiles flag is off, 404 for a catalog outside this caller's scope, 500 for a
    // broken read: none is the uploader's fault, and none may put error styling on an optional
    // section (the no-blocked rule).
    getCatalogProfile.mockRejectedValue(new api.ApiError(500, 'boom'))
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await vi.waitFor(() => expect(getCatalogProfile).toHaveBeenCalledWith('ftr'))

    expect(screen.getByLabelText('Name')).toHaveValue('')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('narrative-unchanged')).toBeNull()
    // The upload is exactly as available as it is on a first upload of a brand-new catalog.
    expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
    expect(uploadFile).toHaveBeenLastCalledWith(expect.any(File), 'ftr', undefined)
  })

  it('treats an existing-but-undescribed catalog as nothing to prefill', async () => {
    getCatalogProfile.mockResolvedValue({ source: 'ftr', pointer_version: 0, profile: null })
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await vi.waitFor(() => expect(getCatalogProfile).toHaveBeenCalledWith('ftr'))
    expect(screen.getByLabelText('Name')).toHaveValue('')
    // An empty form is not "unchanged": there is nothing there to be unchanged from.
    expect(screen.queryByTestId('narrative-unchanged')).toBeNull()
  })

  it('sends the prefilled narrative back unchanged rather than dropping it', async () => {
    getCatalogProfile.mockResolvedValue(DESCRIBED)
    uploadFile.mockResolvedValue(result({ asserted: 4 }))
    renderUpload()
    await openNarrative()
    await userEvent.type(screen.getByLabelText(/source name/i), 'ftr')
    await screen.findByTestId('narrative-unchanged')
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }))

    expect(JSON.parse(sentPart() as string)).toEqual({
      display_name: 'FTR compliance glossary',
      description: 'Financial transaction reporting terms.',
      business_context: 'Owned by financial crime operations.',
      business_domains: ['Compliance'],
    })
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
