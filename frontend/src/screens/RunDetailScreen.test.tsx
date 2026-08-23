import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RunDetailScreen } from './RunDetailScreen'

// importOriginal rather than the bare factory the brief sketched: the screen narrows its catch
// with `instanceof ApiError`, and a factory returning only getFeatureRunDetail would leave
// ApiError undefined on the mock — the 404 test would then throw inside the catch instead of
// exercising the path it exists to pin. Every assertion of the brief's test is kept verbatim.
vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getFeatureRunDetail: vi.fn() }
})
const getFeatureRunDetail = vi.mocked(api.getFeatureRunDetail)

const RUN_ID = 'grun_01M02SAZQQQQ'

// The brief's payload, verbatim: a run whose only draft SUCCEEDED and was then withdrawn (the
// §6.7 two-axis case), and a rail carrying one worked stage plus two sockets with their reasons.
const DETAIL: api.FeatureRunDetail = {
  generation_run_id: RUN_ID, pre_spine: false, owner_subject: 'priya',
  display_name: null, description: null,
  intent: { intent_id: 'i1', hypothesis: 'Retail churn' },
  identity: { run_identity_hash: 'h', considered_revision_id: 'c', metadata_snapshot_id: 's' },
  milestones: {
    choose_candidates: [
      { option_id: 'o1', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:00+00:00' }],
    bind_selections: [],
  },
  authoring: [{
    formula_draft_id: 'd1', option_id: 'o1', state: 'READY',
    rail_state: 'SUCCEEDED', eligibility: 'withdrawn',
    retirement_reason: 'CANDIDATE_SUPERSEDED',
  }],
  rail: [
    { stage: 'CHOOSE_CANDIDATES', state: 'SUCCEEDED', reason_code: null },
    { stage: 'GENERATE_PREVIEW', state: 'UNAVAILABLE',
      reason_code: 'BUILD_SET_DECLARATION_WITHHELD_PRE_PIN' },
    { stage: 'TRAIN_MODEL', state: 'UNAVAILABLE', reason_code: 'SUBSYSTEM_NOT_BUILT' },
  ],
}

beforeEach(() => {
  getFeatureRunDetail.mockReset()
  getFeatureRunDetail.mockResolvedValue(DETAIL)
})

// jsdom implements no clipboard, so the copy test installs one onto the SHARED navigator. Put back
// whatever was there (usually: nothing) so a later test never inherits this mock.
const clipboardBefore = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  if (clipboardBefore) Object.defineProperty(navigator, 'clipboard', clipboardBefore)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

function rowOf(cellText: string): HTMLElement {
  const row = screen.getByText(cellText).closest('tr')
  if (row === null) throw new Error(`'${cellText}' is not in a table row`)
  return row
}

it('renders the rail honestly and both authoring axes, with no trigger buttons', async () => {
  render(<RunDetailScreen runId="grun_01M02SAZQQQQ" />)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.getByText('BUILD_SET_DECLARATION_WITHHELD_PRE_PIN')).toBeInTheDocument()
  expect(screen.getByText(/SUCCEEDED/)).toBeInTheDocument()          // outcome axis
  expect(screen.getByText(/Withdrawn/)).toBeInTheDocument()          // eligibility axis
  expect(screen.queryAllByRole('button').filter(b =>
    /run|re-run|retry|generate|execute|fork/i.test(b.textContent ?? ''))).toHaveLength(0)
})

it('puts every reason code beside the stage it explains, and none beside the rest', async () => {
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('GENERATE_PREVIEW')).toBeInTheDocument())

  const socket = rowOf('GENERATE_PREVIEW')
  expect(within(socket).getByText('Unavailable')).toBeInTheDocument()
  // Verbatim: the server owns the policy sentence, so this screen neither translates the code nor
  // explains it in words of its own.
  expect(within(socket).getByText('BUILD_SET_DECLARATION_WITHHELD_PRE_PIN')).toBeInTheDocument()
  expect(within(rowOf('TRAIN_MODEL')).getByText('SUBSYSTEM_NOT_BUILT')).toBeInTheDocument()

  // A stage that actually ran carries no reason, and absence renders as absence.
  const worked = rowOf('CHOOSE_CANDIDATES')
  expect(within(worked).getByText('Succeeded')).toBeInTheDocument()
  expect(within(worked).getByText('—')).toBeInTheDocument()
})

it('keeps the outcome axis intact on a draft the eligibility axis calls withdrawn', async () => {
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d1')).toBeInTheDocument())

  const draft = rowOf('d1')
  // The stored state and its rail fold are HISTORY: a withdrawn draft is not rewritten to BLOCKED,
  // because that would destroy the record of what happened.
  expect(within(draft).getByText('READY')).toBeInTheDocument()
  expect(within(draft).getByText('SUCCEEDED')).toBeInTheDocument()
  // Eligibility is derived NOW, and it is the only place this truth lives: the rail's
  // AUTHOR_FORMULA stage carries the outcome axis alone.
  expect(within(draft).getByText('Withdrawn — CANDIDATE_SUPERSEDED')).toBeInTheDocument()
})

it('renders a pre-spine run as an honest gap in the record, not as a failure', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL, pre_spine: true, identity: null, display_name: null, owner_subject: null,
    intent: null, authoring: [], milestones: { choose_candidates: [], bind_selections: [] },
  })
  render(<RunDetailScreen runId="fgr_legacy01" />)

  await waitFor(() => expect(screen.getByText(/Pre-spine/i)).toBeInTheDocument())
  expect(screen.getByText(/no run identity was recorded/i)).toBeInTheDocument()
  expect(screen.getByText('No hypothesis recorded')).toBeInTheDocument()
  // Every absence states itself; nothing is invented to fill a column.
  expect(screen.getByRole('heading', { level: 2, name: '—' })).toBeInTheDocument()
  expect(screen.getByText(/No candidates are recorded/i)).toBeInTheDocument()
  expect(screen.getByText(/No formula drafts are recorded/i)).toBeInTheDocument()
  expect(screen.queryByText(/failed/i)).toBeNull()
})

it('states the missing hypothesis of a dangling intent id while still showing the id', async () => {
  // There is no FK from the run to the intent, so the server can hand back an intent_id whose
  // intent row is gone — `hypothesis` is typed non-null but arrives null (Task 10, deferred note).
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL, intent: { intent_id: 'i_dangling', hypothesis: null as unknown as string },
  })
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByText('No hypothesis recorded')).toBeInTheDocument())
  // Unlike the LIST — where the id would stand in for a heading it cannot answer — the detail is
  // the record view: the recorded id is shown as itself, beside the stated absence.
  expect(screen.getByText('i_dangling')).toBeInTheDocument()
})

it('reports a 404 without inventing the reason the server refused to give', async () => {
  getFeatureRunDetail.mockRejectedValue(new api.ApiError(404, 'Not Found'))
  render(<RunDetailScreen runId={RUN_ID} />)

  expect(await screen.findByRole('alert')).toHaveTextContent('Run not found.')
  // Absence and denial are deliberately indistinguishable on this route; a UI that guessed which
  // one happened would leak exactly what the 404 exists to hide.
  expect(screen.queryByText(/permission|access|denied|not yours/i)).toBeNull()
})

it('shows the server sentence for any other failure', async () => {
  getFeatureRunDetail.mockRejectedValue(new api.ApiError(500, 'the database is having a moment'))
  render(<RunDetailScreen runId={RUN_ID} />)

  expect(await screen.findByRole('alert')).toHaveTextContent('the database is having a moment')
})

it('copies the full id, not the truncation on screen', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByText('grun_01M02SAZ…')).toBeInTheDocument())
  expect(screen.getByText('grun_01M02SAZ…')).toHaveAttribute('title', RUN_ID)
  await userEvent.click(screen.getByRole('button', { name: `Copy run id ${RUN_ID}` }))

  expect(writeText).toHaveBeenCalledWith(RUN_ID)
  await waitFor(() => expect(screen.getByTestId('copy-status')).toHaveTextContent(RUN_ID))
})

it('loads the run it was pointed at, and reloads when pointed at another', async () => {
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  expect(screen.getByRole('status')).toHaveTextContent(/Loading run/i)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(getFeatureRunDetail).toHaveBeenCalledWith(RUN_ID)

  // App does not key this screen, so a run -> run deep link changes the prop in place: the fetch
  // has to follow the id, or the reader keeps reading the previous run under a new address.
  getFeatureRunDetail.mockResolvedValue({ ...DETAIL, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))
})
