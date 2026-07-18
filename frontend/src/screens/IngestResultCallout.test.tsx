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
  stage: name, attempt: 1, state, reason_code: null, detail: null,
  started_at: null, completed_at: null,
})

// The full IngestionRun wire shape (backend get_run keys) — typed against the api interface so
// a field-name drift fails typecheck. Runs exist for every outcome: ingested/held/rejected/failed.
const run = (
  id: string,
  stages: api.IngestionStage[],
  over: Partial<api.IngestionRun> = {},
): api.IngestionRun => ({
  id, origin_type: 'upload', catalog_source: 'deposits', filename: 'deposits.csv',
  actor_subject: 'user:o', actor_role_claims: ['data_owner'],
  authorization_decision: 'permitted', status: 'ingested', row_count: 4,
  quarantined_count: 0, started_at: '2026-07-16T09:00:00+00:00',
  completed_at: '2026-07-16T09:00:02+00:00', redacted_failure_code: null,
  status_history: [], stages, ...over,
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

  it('never fetches without a run id — any status', () => {
    renderCallout(result())
    renderCallout(result({ status: 'held', reason: 'too much removed' }))
    renderCallout(result({ status: 'rejected', reason: 'unrecognized headers' }))
    expect(getIngestionRun).not.toHaveBeenCalled()
  })

  // Held/rejected runs carry stages too now — including not_run for what never got a chance.
  // The same compact line renders under the reason; quiet not_run stages stay quiet.
  it('held with a run id fetches and shows the stage summary', async () => {
    getIngestionRun.mockResolvedValue(run('run-5', [
      stage('parse', 'succeeded'),
      stage('brake', 'failed'),
      stage('enrich_concept', 'not_run'),
      stage('pass_b', 'not_run'),
    ], { status: 'held' }))
    renderCallout(result({ status: 'held', reason: 'too much removed', ingestion_run_id: 'run-5' }))
    expect(getIngestionRun).toHaveBeenCalledWith('run-5')
    expect(await screen.findByText('brake failed')).toHaveStyle({ fontWeight: '600' })
    // not_run stays quiet in the one-line summary (the full table lives in the run panel).
    expect(screen.queryByText(/not run/)).toBeNull()
  })

  it('rejected with a run id fetches and shows the stage summary', async () => {
    getIngestionRun.mockResolvedValue(run('run-6', [
      stage('validation', 'partial'),
      stage('drift', 'not_run'),
    ], { status: 'rejected' }))
    renderCallout(
      result({ status: 'rejected', reason: 'unrecognized headers', ingestion_run_id: 'run-6' }))
    expect(getIngestionRun).toHaveBeenCalledWith('run-6')
    expect(await screen.findByText('validation partial')).toHaveStyle({ fontWeight: '600' })
  })
})

// Every outcome with a run id gets the same door into the full manifest.
describe('view run details', () => {
  it.each(['ingested', 'held', 'rejected'] as const)(
    '%s result offers "View run details" and toggles the panel',
    async status => {
      getIngestionRun.mockResolvedValue(
        run('run-7', [stage('parse', 'succeeded')], { status }))
      renderCallout(result({ status, reason: status === 'ingested' ? null : 'why',
        ingestion_run_id: 'run-7' }))
      const button = screen.getByRole('button', { name: 'View run details' })
      expect(button).toHaveAttribute('aria-expanded', 'false')
      await userEvent.click(button)
      expect(await screen.findByRole('heading', { name: 'Ingestion run' })).toBeInTheDocument()
      const hide = screen.getByRole('button', { name: 'Hide run details' })
      expect(hide).toHaveAttribute('aria-expanded', 'true')
      await userEvent.click(hide)
      expect(screen.queryByRole('heading', { name: 'Ingestion run' })).toBeNull()
    },
  )

  it('offers no run-details button without a run id', () => {
    renderCallout(result())
    expect(screen.queryByRole('button', { name: /run details/i })).toBeNull()
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

// MF-5: the truthful second line — objects stored (tables · columns), containment edges, join
// candidates, and the Pass B proposed/abstained split. Rendered only when the backend sent the
// additive counts; a pre-MF-5 result (no objects_stored) stays a single line.
describe('MF-5 truthful counts line', () => {
  it('renders the objects/edges/join/Pass B breakdown when the counts are present', () => {
    renderCallout(result({
      objects_stored: 5, tables: 2, columns: 3, containment_edges: 3,
      facts_asserted: 4, join_candidates: 0, passb_proposed: 2, passb_abstained: 1,
    }))
    expect(screen.getByRole('status')).toHaveTextContent(
      '5 objects stored (2 tables · 3 columns), 3 containment edges, ' +
        '0 join candidates · Pass B: 2 proposed, 1 abstained',
    )
  })

  it('uses singular nouns for a one-table, one-column, one-candidate upload', () => {
    renderCallout(result({
      objects_stored: 2, tables: 1, columns: 1, containment_edges: 1,
      facts_asserted: 1, join_candidates: 1, passb_proposed: 0, passb_abstained: 0,
    }))
    expect(screen.getByRole('status')).toHaveTextContent(
      '2 objects stored (1 table · 1 column), 1 containment edge, ' +
        '1 join candidate · Pass B: 0 proposed, 0 abstained',
    )
  })

  it('stays a single line for a pre-MF-5 result with no counts', () => {
    renderCallout(result())
    const callout = screen.getByRole('status')
    expect(callout).toHaveTextContent('4 facts asserted, 0 objects changed, 0 quarantined')
    expect(callout).not.toHaveTextContent('objects stored')
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
