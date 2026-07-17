import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RunDetailPanel } from './RunDetailPanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getIngestionRun: vi.fn() }
})
const getIngestionRun = vi.mocked(api.getIngestionRun)

// Block body (same trap the sibling tests document): mockReset() returns the mock fn, and a
// function returned from beforeEach becomes a per-test teardown that would CALL the mock.
beforeEach(() => {
  getIngestionRun.mockReset()
})

const stage = (
  name: string,
  state: string,
  over: Partial<api.IngestionStage> = {},
): api.IngestionStage => ({
  stage: name, attempt: 1, state, reason_code: null, detail: null,
  started_at: null, completed_at: null, ...over,
})

// The full manifest as GET /ingestion-runs/{id} returns it — typed against the api interface
// so a field-name drift fails typecheck.
const RUN: api.IngestionRun = {
  id: 'run-9',
  origin_type: 'upload',
  catalog_source: 'deposits',
  filename: 'deposits-q3.csv',
  actor_subject: 'user:owner',
  actor_role_claims: ['data_owner', 'platform_admin'],
  authorization_decision: 'permitted',
  status: 'failed',
  row_count: 12,
  quarantined_count: 2,
  started_at: '2026-07-16T09:00:00+00:00',
  completed_at: '2026-07-16T09:00:03+00:00',
  redacted_failure_code: 'FACT_ASSERTION_ERROR',
  status_history: [
    { status: 'opened', at: '2026-07-16T09:00:00+00:00', reason_code: null },
    { status: 'failed', at: '2026-07-16T09:00:03+00:00', reason_code: 'FACT_ASSERTION_ERROR' },
  ],
  stages: [
    stage('parse', 'succeeded', {
      started_at: '2026-07-16T09:00:00+00:00', completed_at: '2026-07-16T09:00:00.400+00:00',
    }),
    stage('fact_assertion', 'failed', { reason_code: 'FACT_ASSERTION_ERROR' }),
    stage('enrich_concept', 'not_run'),
    stage('pass_b', 'disabled'),
  ],
}

describe('RunDetailPanel', () => {
  it('renders the full manifest: header facts, status history, and the stages table', async () => {
    getIngestionRun.mockResolvedValue(RUN)
    render(<RunDetailPanel runId="run-9" onClose={() => {}} />)
    expect(getIngestionRun).toHaveBeenCalledExactlyOnceWith('run-9')

    const panel = await screen.findByRole('region', { name: /ingestion run details/i })
    // Header facts: source + origin, file, actor + roles, authorization, status, counts, times,
    // and the redacted failure code.
    expect(panel).toHaveTextContent('deposits')
    expect(panel).toHaveTextContent('deposits-q3.csv')
    expect(panel).toHaveTextContent('user:owner')
    expect(panel).toHaveTextContent('roles: data_owner, platform_admin')
    expect(panel).toHaveTextContent('permitted')
    expect(panel).toHaveTextContent('12 rows · 2 quarantined')
    expect(panel).toHaveTextContent('2026-07-16 09:00:00+00:00')
    expect(panel).toHaveTextContent('FACT_ASSERTION_ERROR')

    // The run-status chip carries the danger fill AND the word — never color alone. Scoped to
    // the header facts (the stages table below has its own 'failed' chip).
    const statusChip = screen.getByText('failed', { selector: '.kv .badge' })
    expect(statusChip).toHaveClass('run-failed')

    // Status history in recorded order.
    const history = screen.getAllByRole('listitem')
    expect(history[0]).toHaveTextContent('opened')
    expect(history[1]).toHaveTextContent('failed · FACT_ASSERTION_ERROR')

    // Stages table: stage · state chip · reason · timing. The failed stage warns, not_run and
    // disabled stay muted — each chip still says its state in words.
    const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('parse')
    expect(within(rows[0]).getByText('succeeded')).toHaveClass('badge', 'ok')
    expect(rows[0]).toHaveTextContent('400 ms')
    expect(within(rows[1]).getByText('failed')).toHaveClass('badge', 'stage-warn')
    expect(rows[1]).toHaveTextContent('FACT_ASSERTION_ERROR')
    const notRun = within(rows[2]).getByText('not run')
    expect(notRun).toHaveClass('badge')
    expect(notRun).not.toHaveClass('stage-warn')
    expect(notRun).not.toHaveClass('ok')
    expect(within(rows[3]).getByText('disabled')).not.toHaveClass('stage-warn')
  })

  // #15 (A1-minimal): row_count is a parsed-row count, not an asserted-fact count — an FTR
  // upload with no grain/as-of asserts 0 facts while row_count is 126, so "126 asserted" on a
  // fully-rejected run was a lie. The label must say "rows"; the full count model is A2.
  it('labels counts honestly: rows · quarantined, never "asserted"', async () => {
    getIngestionRun.mockResolvedValue({
      ...RUN, status: 'rejected', row_count: 126, quarantined_count: 126,
    })
    render(<RunDetailPanel runId="run-9" onClose={() => {}} />)
    const panel = await screen.findByRole('region', { name: /ingestion run details/i })
    expect(panel).toHaveTextContent('126 rows · 126 quarantined')
    expect(panel).not.toHaveTextContent('asserted')
  })

  it('shows a loading line until the fetch resolves', () => {
    getIngestionRun.mockReturnValue(new Promise(() => {}))
    render(<RunDetailPanel runId="run-9" onClose={() => {}} />)
    expect(screen.getByText(/loading run/i)).toBeInTheDocument()
  })

  it('says so when the run 404s', async () => {
    getIngestionRun.mockRejectedValue(new api.ApiError(404, 'not found'))
    render(<RunDetailPanel runId="run-gone" onClose={() => {}} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Run run-gone was not found — nothing is recorded under this id.',
    )
  })

  it('surfaces other fetch failures with the transport detail', async () => {
    getIngestionRun.mockRejectedValue(new api.ApiError(500, 'boom'))
    render(<RunDetailPanel runId="run-9" onClose={() => {}} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the run. boom')
  })

  it('Close hands control back to the opener', async () => {
    getIngestionRun.mockResolvedValue(RUN)
    const onClose = vi.fn()
    render(<RunDetailPanel runId="run-9" onClose={onClose} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})
