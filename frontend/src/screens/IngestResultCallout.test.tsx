import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { IngestResultCallout, summarizeStages } from './IngestResultCallout'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getIngestionRun: vi.fn() }
})
const getIngestionRun = vi.mocked(api.getIngestionRun)

// Block body (same trap UploadScreen.test.tsx documents): mockReset() returns the mock fn, and
// Vitest treats a function returned from beforeEach as a per-test teardown — it would then CALL
// the mock after each test and await its rejected promise in the reject case.
beforeEach(() => {
  getIngestionRun.mockReset()
})

const result = (over: Partial<api.IngestResult> = {}): api.IngestResult => ({
  status: 'ingested', reason: null, asserted: 4, changed_objects: 0, quarantined: 0,
  flagged: null, ...over,
})

const stage = (name: string, state: string): api.IngestionStage => ({
  stage: name, state, reason_code: null, detail: null, started_at: null, completed_at: null,
})

// The full IngestionRun wire shape (backend get_run keys: id, origin_type, catalog_source,
// status, stages) — typed against the api interface so a field-name drift fails typecheck.
const run = (id: string, stages: api.IngestionStage[]): api.IngestionRun => ({
  id, origin_type: 'upload', catalog_source: 'deposits', status: 'ingested', stages,
})

function renderCallout(res: api.IngestResult) {
  render(<IngestResultCallout result={res} source="deposits" onReviewQueue={() => {}} />)
}

describe('ingest result stage summary', () => {
  it('renders one compact summary line from the run stages', async () => {
    getIngestionRun.mockResolvedValue(run('run-1', [
      stage('parse', 'succeeded'),
      stage('validation', 'succeeded'),
      stage('drift', 'failed'),
      stage('enrich_concept', 'succeeded'),
      stage('enrich_definition', 'succeeded'),
      stage('enrich_domain', 'succeeded'),
      stage('pass_b', 'disabled'),
      stage('pass_c', 'succeeded'),
      stage('projection_drain', 'lagged'),
    ]))
    renderCallout(result({ ingestion_run_id: 'run-1' }))
    expect(getIngestionRun).toHaveBeenCalledWith('run-1')
    // The whole line, exactly — enrichment folds to one word, quiet stages stay quiet, and the
    // failed stage is called out once at the end.
    const line = await screen.findByText(
      (_, el) =>
        el?.tagName === 'P' &&
        el.textContent === 'Enriched · Pass B off · Pass C on · projection lagged · drift failed',
    )
    expect(line).toBeInTheDocument()
    // Warn tone on the trouble segments; the quiet ones carry no warn styling.
    expect(screen.getByText('drift failed')).toHaveStyle({ fontWeight: '600' })
    expect(screen.getByText('projection lagged')).toHaveStyle({ fontWeight: '600' })
    expect(screen.getByText('Enriched')).not.toHaveStyle({ fontWeight: '600' })
  })

  it('degrades gracefully when the run fetch fails — core result still shows', async () => {
    getIngestionRun.mockRejectedValue(new api.ApiError(500, 'boom'))
    renderCallout(result({ ingestion_run_id: 'run-2' }))
    expect(screen.getByText('Ingested.')).toBeInTheDocument()
    await waitFor(() => expect(getIngestionRun).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/Enriched|Pass B|Pass C|projection/)).toBeNull()
  })

  it('renders nothing extra for an all-quiet run', async () => {
    getIngestionRun.mockResolvedValue(
      run('run-3', [stage('parse', 'succeeded'), stage('validation', 'succeeded')]))
    renderCallout(result({ ingestion_run_id: 'run-3' }))
    await waitFor(() => expect(getIngestionRun).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/Enriched|Pass B|Pass C|projection/)).toBeNull()
  })

  it('never fetches without a run id, and never for a non-ingested result', () => {
    renderCallout(result())
    expect(screen.getByText('Ingested.')).toBeInTheDocument()
    renderCallout(result({ status: 'held', reason: 'too much removed', ingestion_run_id: 'run-4' }))
    expect(getIngestionRun).not.toHaveBeenCalled()
  })
})

// The backend persists quarantine rows on held/rejected too (#12): both branches must offer the
// review-queue handoff and must not claim "nothing was applied" when the queue changed.
describe('held/rejected quarantine handoff', () => {
  it('held with quarantined rows shows the count, honest copy, and the review-queue button', async () => {
    const onReviewQueue = vi.fn()
    render(
      <IngestResultCallout
        result={result({ status: 'held', reason: 'removes 8 of 10 objects', quarantined: 2 })}
        source="deposits"
        onReviewQueue={onReviewQueue}
      />,
    )
    const callout = screen.getByRole('status')
    expect(callout).toHaveTextContent(/held: this change removes too much/i)
    // Honest copy: the review queue DID change, so the blanket claim must not render.
    expect(callout).not.toHaveTextContent('Nothing was applied.')
    expect(callout).toHaveTextContent('No catalog changes were applied.')
    expect(callout).toHaveTextContent(/2 rows were quarantined for review/)
    await userEvent.click(screen.getByRole('button', { name: 'Review 2 quarantined rows' }))
    expect(onReviewQueue).toHaveBeenCalledExactlyOnceWith('deposits')
  })

  it('rejected with quarantined rows shows the count and the review-queue button', async () => {
    const onReviewQueue = vi.fn()
    render(
      <IngestResultCallout
        result={result({ status: 'rejected', reason: 'unrecognized headers', quarantined: 1 })}
        source="deposits"
        onReviewQueue={onReviewQueue}
      />,
    )
    const callout = screen.getByRole('status')
    expect(callout).toHaveTextContent('Rejected.')
    expect(callout).toHaveTextContent(/1 row was quarantined for review/)
    await userEvent.click(screen.getByRole('button', { name: 'Review 1 quarantined row' }))
    expect(onReviewQueue).toHaveBeenCalledExactlyOnceWith('deposits')
  })

  it('held/rejected with an empty queue keep the plain copy and offer no button', () => {
    render(
      <IngestResultCallout
        result={result({ status: 'held', reason: 'too much removed' })}
        source="deposits"
        onReviewQueue={() => {}}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Nothing was applied.')
    expect(screen.queryByRole('button', { name: /quarantined row/ })).toBeNull()
  })
})

describe('summarizeStages', () => {
  it('folds mixed enrichment to its worst state and voices skipped passes', () => {
    expect(
      summarizeStages([
        stage('enrich_concept', 'succeeded'),
        stage('enrich_definition', 'partial'),
        stage('pass_b', 'skipped_no_client'),
        stage('pass_c', 'not_applicable'),
        stage('projection_drain', 'succeeded'),
      ]),
    ).toEqual([
      { text: 'enrichment partial', warn: true },
      { text: 'Pass B skipped', warn: false },
    ])
  })

  it('stays quiet on unknown stages in healthy states, warns on audit_degraded', () => {
    expect(
      summarizeStages([stage('some_new_stage', 'succeeded'), stage('brake', 'audit_degraded')]),
    ).toEqual([{ text: 'brake audit-degraded', warn: true }])
  })
})
