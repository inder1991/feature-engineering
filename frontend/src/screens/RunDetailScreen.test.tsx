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
  return {
    ...actual,
    getFeatureRunDetail: vi.fn(),
    prepareRunCode: vi.fn(),
    retryRunAuthoring: vi.fn(),
  }
})
const getFeatureRunDetail = vi.mocked(api.getFeatureRunDetail)
const prepareRunCode = vi.mocked(api.prepareRunCode)
const retryRunAuthoring = vi.mocked(api.retryRunAuthoring)

const RUN_ID = 'grun_01M02SAZQQQQ'

// The retry answer for a row where the question is not asked at all — an attempt that bought
// something, or one still in flight. Absence, never a refusal, so there is nothing to explain,
// and nothing to warn about either.
const NO_RETRY = { retryable: false, retry_blockers: [], retry_warnings: [] }

// A run whose only draft SUCCEEDED and was then withdrawn (the §6.7 two-axis case), and a rail
// carrying one worked stage plus two sockets with their reasons. One attempt, so it is also its
// candidate's current answer — the shape every run had before 1107 made a second attempt possible.
const DRAFT_1: api.RunAuthoringRow = {
  formula_draft_id: 'd1', considered_revision_id: 'c', option_id: 'o1', state: 'READY',
  rail_state: 'SUCCEEDED', eligibility: 'withdrawn', retirement_reason: 'CANDIDATE_SUPERSEDED',
  retirement_replacement_draft_id: null, ...NO_RETRY,
}

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
  authoring: { current: [{ ...DRAFT_1, resolved: true }], history: [DRAFT_1] },
  jobs: [],
  // The run in this fixture has one candidate and it already has an answer, so the gesture has
  // nothing to prepare — the tests that exercise it widen `choose_candidates` themselves.
  prepare_code: { available: true, reason_code: null, detail: null },
  rail: [
    { stage: 'CHOOSE_CANDIDATES', state: 'SUCCEEDED', reason_code: null },
    { stage: 'BIND_SELECTIONS', state: 'IN_PROGRESS', reason_code: null,
      detail: '2 of 5 bound — accumulating' },
    { stage: 'GENERATE_PREVIEW', state: 'UNAVAILABLE',
      reason_code: 'BUILD_SET_DECLARATION_WITHHELD_PRE_PIN' },
    { stage: 'TRAIN_MODEL', state: 'UNAVAILABLE', reason_code: 'SUBSYSTEM_NOT_BUILT' },
  ],
}

beforeEach(() => {
  getFeatureRunDetail.mockReset()
  getFeatureRunDetail.mockResolvedValue(DETAIL)
  prepareRunCode.mockReset()
  retryRunAuthoring.mockReset()
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

it('renders the rail honestly and both authoring axes, and offers ONE write', async () => {
  render(<RunDetailScreen runId="grun_01M02SAZQQQQ" />)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.getByText('BUILD_SET_DECLARATION_WITHHELD_PRE_PIN')).toBeInTheDocument()
  expect(screen.getByText(/SUCCEEDED/)).toBeInTheDocument()          // outcome axis
  expect(screen.getByText(/Withdrawn/)).toBeInTheDocument()          // eligibility axis
  // The absent controls are still the design: nothing here re-runs, executes or forks a run, and
  // retry is an ATTEMPT's affair — offered only where the server says a bought-nothing attempt may
  // be bought again, which this READY draft is not.
  expect(screen.queryAllByRole('button').filter(b =>
    /re-run|execute|fork|retry/i.test(b.textContent ?? ''))).toHaveLength(0)
  expect(screen.getByRole('button', { name: /Prepare formulas/ })).toBeInTheDocument()
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

// A candidate retried after a failure — the shape migration 1107 made possible (the money guard
// covers ANSWERS, so a failed draft stops holding the identity slot and a governed retry can write
// a second draft against it). Both attempts are real rows; only one is where the candidate stands.
const ATTEMPT_FAILED: api.RunAuthoringRow = {
  formula_draft_id: 'd0', considered_revision_id: 'c', option_id: 'o1', state: 'FAILED',
  rail_state: 'FAILED', eligibility: 'current', retirement_reason: null,
  retirement_replacement_draft_id: null, ...NO_RETRY,
}
const ATTEMPT_READY: api.RunAuthoringRow = {
  ...DRAFT_1, formula_draft_id: 'd2', eligibility: 'current', retirement_reason: null,
}

it('shows every attempt and marks the one that is the candidate’s current answer', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL,
    authoring: {
      current: [{ ...ATTEMPT_READY, resolved: true }],
      history: [ATTEMPT_FAILED, ATTEMPT_READY],
    },
  })
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d2')).toBeInTheDocument())

  // NOTHING IS HIDDEN. The failure that was retried past stays on the record with its own two
  // axes — a screen that showed only the current answer would erase what the platform actually
  // did, and the attempt is what an operator quotes when they ask why a retry was approved.
  const earlier = rowOf('d0')
  expect(within(earlier).getAllByText('FAILED')).toHaveLength(2)   // state + outcome, both stored
  expect(within(earlier).getByText('Earlier attempt')).toBeInTheDocument()
  expect(earlier).toHaveClass('attempt-earlier')
  expect(within(earlier).queryByText('Current answer')).toBeNull()

  // ...and the record says which of the two is where the candidate STANDS. The server decided
  // that; this screen renders the row it named and folds no second opinion out of the states.
  const answer = rowOf('d2')
  expect(within(answer).getByText('Current answer')).toBeInTheDocument()
  expect(answer).toHaveClass('attempt-current')
})

it('never calls the latest attempt an answer when the platform bought nothing', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL,
    authoring: {
      current: [{ ...ATTEMPT_FAILED, resolved: false }],
      history: [ATTEMPT_FAILED],
    },
  })
  render(<RunDetailScreen runId={RUN_ID} />)

  // `resolved: false` is the candidate standing on nothing: every attempt failed or was cancelled.
  // The row is still the current reading — an empty one would read as "never tried".
  await waitFor(() => expect(screen.getByText(/no answer bought/)).toBeInTheDocument())
  expect(rowOf('d0')).toHaveClass('attempt-current')
  expect(screen.queryByText('Current answer')).toBeNull()
})

it('renders a pre-spine run as an honest gap in the record, not as a failure', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL, pre_spine: true, identity: null, display_name: null, owner_subject: null,
    intent: null, authoring: { current: [], history: [] },
    milestones: { choose_candidates: [], bind_selections: [] },
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
  // ▲ AND THE PREPARE SECTION SAYS THE SAME KIND OF NOTHING. It used to answer this exact run —
  // zero chosen candidates — with "every candidate this run chose already has a formula attempt",
  // a claim about a record that is simply empty. This test had the run in front of it and asserted
  // only the OTHER absences, so the invented one stood unread.
  expect(screen.getByTestId('prepare-nothing-chosen'))
    .toHaveTextContent(/Nothing has been chosen for this run yet/i)
  expect(screen.queryByText(/already has a formula attempt/i)).toBeNull()
})

it('still says every chosen candidate has an attempt when that is what happened', async () => {
  // The OTHER absence, and it is a different fact: this run DID choose a candidate (o1) and that
  // candidate DOES have an answer. The sentence is true here — which is why it was the only one on
  // offer, and why the zero-chosen run inherited a claim about choices nobody made.
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())

  expect(screen.getByTestId('prepare-nothing-waiting'))
    .toHaveTextContent(/already has a formula attempt/i)
  expect(screen.queryByTestId('prepare-nothing-chosen')).toBeNull()
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

// ── the milestone's own count, which used to be computed and then dropped ────────────────────────
it('renders a stage’s count beside the stage it belongs to', async () => {
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('BIND_SELECTIONS')).toBeInTheDocument())
  // A number derived by the server and never shown is indistinguishable from one never derived —
  // the run page carried this string in every response and rendered none of it.
  expect(within(rowOf('BIND_SELECTIONS')).getByText('2 of 5 bound — accumulating'))
    .toBeInTheDocument()
})

// ── the ONE gesture ─────────────────────────────────────────────────────────────────────────────
// A run with two chosen candidates, one of which already has an answer: only the other is offered.
const TWO_CHOSEN: api.FeatureRunDetail = {
  ...DETAIL,
  milestones: {
    ...DETAIL.milestones,
    choose_candidates: [
      { option_id: 'o1', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:00+00:00' },
      { option_id: 'o2', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:01+00:00' },
      // The SAME candidate chosen twice is one thing to prepare, not two checkboxes.
      { option_id: 'o2', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:02+00:00' },
    ],
  },
}

it('offers only the chosen candidates that have no answer yet, once each', async () => {
  getFeatureRunDetail.mockResolvedValue(TWO_CHOSEN)
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  // o1 already carries a current answer (DRAFT_1) — asking for it again would be a second purchase
  // of something the run already has.
  expect(screen.queryAllByRole('checkbox')).toHaveLength(1)
})

it('sends the candidates a person picked, and never fires from anything but the click', async () => {
  getFeatureRunDetail.mockResolvedValue(TWO_CHOSEN)
  prepareRunCode.mockResolvedValue({
    job_id: 'cgj-new', created: true, declaration_source_job_id: 'cgj-old',
    detail: 'the job was recorded; the worker drives it',
    run: { ...TWO_CHOSEN, jobs: [{ job_id: 'cgj-new', status: 'REQUESTED',
      requested_at: '2026-08-23T01:00:00+00:00',
      actions: [{ action: 'AUTHOR_FORMULA', state: 'PENDING' }] }] },
  })
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())

  // Rendering alone must spend nothing: the whole screen has loaded and no request has gone out.
  expect(prepareRunCode).not.toHaveBeenCalled()
  const button = screen.getByRole('button', { name: /Prepare formulas/ })
  expect(button).toBeDisabled()            // nothing picked yet — a no-op button would be a lie

  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(button)

  expect(prepareRunCode).toHaveBeenCalledWith(RUN_ID, { option_ids: ['o2'] })
  // The run comes back READ from the store, so the job list below is the store's own answer.
  await waitFor(() => expect(screen.getByText('cgj-new')).toBeInTheDocument())
  expect(screen.getByTestId('prepare-status')).toHaveTextContent('cgj-old')
})

it('collects the ceiling from the SERVER’s numbers when the LLM authors', async () => {
  getFeatureRunDetail.mockResolvedValue(TWO_CHOSEN)
  prepareRunCode.mockRejectedValueOnce(new api.ApiError(
    409, 'this job authors with the LLM, and spend is an authorized act', null,
    'COST_AUTHORIZATION_MISSING',
    { code: 'COST_AUTHORIZATION_MISSING', llm_members: 1, estimated_provider_calls: 45 }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))

  // The enforcement's own budget, quoted back — never a number this browser computed.
  await waitFor(() => expect(screen.getByText(/up to 45 provider calls/)).toBeInTheDocument())
  expect(screen.getByText(/authors with the LLM/)).toBeInTheDocument()
  // ...and the act cannot proceed on an unconfirmed ceiling.
  expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeDisabled()

  prepareRunCode.mockResolvedValue({
    job_id: 'cgj-paid', created: true, declaration_source_job_id: 'cgj-old',
    detail: 'the job was recorded', run: TWO_CHOSEN,
  })
  await userEvent.type(screen.getByLabelText(/Token ceiling/), '200000')
  await userEvent.type(screen.getByLabelText(/Cost ceiling/), '12.50')
  await userEvent.click(screen.getByRole('button', { name: /cost ceiling/ }))

  await waitFor(() => expect(prepareRunCode).toHaveBeenCalledTimes(2))
  expect(prepareRunCode.mock.calls[1][1]).toMatchObject({
    option_ids: ['o2'],
    spend_approval: { max_calls: 45, max_tokens: 200000, max_cost: '12.50', currency: 'USD' },
  })
})

it('disables the gesture with the server’s own sentence when its entrance would refuse', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...TWO_CHOSEN,
    prepare_code: {
      available: false, reason_code: 'PRE_SPINE_NOT_ACTIONABLE',
      detail: 'this run predates the identity spine, so no frozen considered set was recorded',
    },
  })
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByTestId('prepare-unavailable')).toBeInTheDocument())
  // Verbatim, code and sentence: this screen holds no policy and owns no words for a refusal.
  expect(screen.getByTestId('prepare-unavailable')).toHaveTextContent('PRE_SPINE_NOT_ACTIONABLE')
  expect(screen.getByTestId('prepare-unavailable'))
    .toHaveTextContent('no frozen considered set was recorded')
  expect(screen.getByRole('button', { name: /Prepare formulas/ })).toBeDisabled()
  // The checkbox goes with it: a control that could not be acted on would invite the click anyway.
  expect(screen.getByRole('checkbox', { name: /o2/ })).toBeDisabled()
  // ...and the pair holds. Trying anyway reaches no request at all — the guard is what stops the
  // gesture, not the greying, and a screen that only LOOKED disabled would still send this.
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  expect(prepareRunCode).not.toHaveBeenCalled()
})

it('shows a refusal in the server’s words and records nothing of its own', async () => {
  getFeatureRunDetail.mockResolvedValue(TWO_CHOSEN)
  prepareRunCode.mockRejectedValue(new api.ApiError(
    409, 'no code-generation job has ever been requested for this run', null,
    'RUN_BUILD_DECLARATION_ABSENT',
    { code: 'RUN_BUILD_DECLARATION_ABSENT' }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))

  expect(await screen.findByRole('alert'))
    .toHaveTextContent('no code-generation job has ever been requested for this run')
})

// ── the jobs bridge ─────────────────────────────────────────────────────────────────────────────
const WITH_JOB: api.FeatureRunDetail = {
  ...DETAIL,
  jobs: [{
    job_id: 'cgj_1', status: 'AUTHORING', requested_at: '2026-08-23T00:00:00+00:00',
    actions: [{ action: 'AUTHOR_FORMULA', state: 'PERFORMED' },
      { action: 'GENERATE_PREVIEW', state: 'PENDING' }],
  }],
}

it('renders each job’s status and every act it performs, verbatim', async () => {
  getFeatureRunDetail.mockResolvedValue(WITH_JOB)
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByText('cgj_1')).toBeInTheDocument())
  const row = rowOf('cgj_1')
  expect(within(row).getByText('AUTHORING')).toBeInTheDocument()
  // SEVERAL acts, because one job performs several — 1111 refused to collapse them into one word.
  expect(within(row).getByText(/AUTHOR_FORMULA performed · GENERATE_PREVIEW pending/))
    .toBeInTheDocument()
  // Flag off (the default in tests): the workspace is not routed, so there is no link to it.
  expect(within(row).queryByRole('link')).toBeNull()
})

it('links a job to the generation workspace only where that workspace is routed', async () => {
  vi.stubEnv('VITE_CODE_GENERATION', '1')
  getFeatureRunDetail.mockResolvedValue(WITH_JOB)
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByRole('link', { name: 'cgj_1' })).toBeInTheDocument())
  expect(screen.getByRole('link', { name: 'cgj_1' }))
    .toHaveAttribute('href', '#/code-generation?job_id=cgj_1')
  vi.unstubAllEnvs()
})

it('states WHICH choices carry a formula, and leaves HOW MANY to the rail', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL,
    milestones: {
      ...DETAIL.milestones,
      bind_selections: [
        { binding_id: 'b1', selection_revision_id: 's1', formula_draft_id: 'd1',
          considered_revision_id: 'c', option_id: 'o1', recorded_at: '2026-08-23T00:00:00+00:00' },
        // The SAME selection re-pinned after a re-author: two rows, ONE choice. A count rendered
        // from these rows would say 2 beside a rail that says "2 of 5" for a different reason.
        { binding_id: 'b2', selection_revision_id: 's1', formula_draft_id: 'd2',
          considered_revision_id: 'c', option_id: 'o1', recorded_at: '2026-08-23T00:01:00+00:00' },
      ],
    },
  })
  render(<RunDetailScreen runId={RUN_ID} />)

  await waitFor(() => expect(screen.getByText(/Formulas are pinned to/)).toBeInTheDocument())
  // No second number under the same heading: the rail already answered "how many", and these rows
  // answer a different question.
  expect(screen.queryByText(/2 selection binding/)).toBeNull()
  expect(within(rowOf('BIND_SELECTIONS')).getByText('2 of 5 bound — accumulating'))
    .toBeInTheDocument()
})

// ── review fixes: the quote describes a SET, and an answer belongs to the run it asked about ─────
const THREE_CHOSEN: api.FeatureRunDetail = {
  ...DETAIL,
  milestones: {
    ...DETAIL.milestones,
    choose_candidates: [
      { option_id: 'o2', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:01+00:00' },
      { option_id: 'o3', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:02+00:00' },
    ],
  },
}

it('retires a cost quote the moment the set it described changes', async () => {
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  prepareRunCode.mockRejectedValueOnce(new api.ApiError(
    409, 'confirm the ceiling', null, 'COST_AUTHORIZATION_MISSING',
    { code: 'COST_AUTHORIZATION_MISSING', llm_members: 1, estimated_provider_calls: 45 }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())

  // Quote ONE candidate, and confirm the ceiling it quoted.
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  await waitFor(() => expect(screen.getByText(/up to 45 provider calls/)).toBeInTheDocument())
  await userEvent.type(screen.getByLabelText(/Token ceiling/), '200000')
  await userEvent.type(screen.getByLabelText(/Cost ceiling/), '12.50')

  // ...then add a second candidate. The quote was for ONE draft; sending it now would have the
  // person authorize 45 calls for work the server never quoted at 45.
  prepareRunCode.mockRejectedValueOnce(new api.ApiError(
    409, 'confirm the ceiling', null, 'COST_AUTHORIZATION_MISSING',
    { code: 'COST_AUTHORIZATION_MISSING', llm_members: 2, estimated_provider_calls: 90 }))
  await userEvent.click(screen.getByRole('checkbox', { name: /o3/ }))
  expect(screen.queryByText(/up to 45 provider calls/)).toBeNull()
  expect(screen.getByRole('button', { name: /records a code-generation job/ })).toBeInTheDocument()

  // The next click RE-ASKS, and the ceiling that goes out is the one quoted for the new set.
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  await waitFor(() => expect(screen.getByText(/up to 90 provider calls/)).toBeInTheDocument())
  prepareRunCode.mockResolvedValue({
    job_id: 'cgj-2', created: true, declaration_source_job_id: 'cgj-old',
    detail: 'recorded', run: THREE_CHOSEN,
  })
  await userEvent.click(screen.getByRole('button', { name: /cost ceiling/ }))
  await waitFor(() => expect(prepareRunCode).toHaveBeenCalledTimes(3))
  expect(prepareRunCode.mock.calls[2][1]).toMatchObject({
    option_ids: ['o2', 'o3'], spend_approval: { max_calls: 90 },
  })
})

it('refuses to confirm a ceiling that is not a number', async () => {
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  prepareRunCode.mockRejectedValue(new api.ApiError(
    409, 'confirm the ceiling', null, 'COST_AUTHORIZATION_MISSING',
    { code: 'COST_AUTHORIZATION_MISSING', llm_members: 1, estimated_provider_calls: 45 }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  await waitFor(() => expect(screen.getByText(/up to 45 provider calls/)).toBeInTheDocument())

  await userEvent.type(screen.getByLabelText(/Cost ceiling/), '12.50')
  // Free text: `Number('twelve')` is NaN, NaN serialises to `null`, and the server answers a raw
  // 422 about a missing field instead of the number the person thought they had set.
  await userEvent.type(screen.getByLabelText(/Token ceiling/), 'twelve')
  expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeDisabled()
  // Zero is the same refusal for the same reason: a ceiling of nothing authorizes nothing.
  await userEvent.clear(screen.getByLabelText(/Token ceiling/))
  await userEvent.type(screen.getByLabelText(/Token ceiling/), '0')
  expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeDisabled()

  await userEvent.clear(screen.getByLabelText(/Token ceiling/))
  await userEvent.type(screen.getByLabelText(/Token ceiling/), '200000')
  expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeEnabled()
})

// ▲ THE COST FIELD'S TWIN OF THE SAME DEFECT (carried from Task 4). `max_cost` travels as a STRING
// and lands in a NUMERIC column, so 'twelve' was not a 422 about a missing field — it was a raw 500
// out of the driver's cast. A trim check accepts every one of these.
it('refuses to confirm a COST ceiling that is not a positive number', async () => {
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  prepareRunCode.mockRejectedValue(new api.ApiError(
    409, 'confirm the ceiling', null, 'COST_AUTHORIZATION_MISSING',
    { code: 'COST_AUTHORIZATION_MISSING', llm_members: 1, estimated_provider_calls: 45 }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  await waitFor(() => expect(screen.getByText(/up to 45 provider calls/)).toBeInTheDocument())
  await userEvent.type(screen.getByLabelText(/Token ceiling/), '200000')

  for (const typed of ['twelve', '0', '-5']) {
    await userEvent.clear(screen.getByLabelText(/Cost ceiling/))
    await userEvent.type(screen.getByLabelText(/Cost ceiling/), typed)
    expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeDisabled()
  }
  await userEvent.clear(screen.getByLabelText(/Cost ceiling/))
  await userEvent.type(screen.getByLabelText(/Cost ceiling/), '12.50')
  expect(screen.getByRole('button', { name: /cost ceiling/ })).toBeEnabled()
  expect(prepareRunCode).toHaveBeenCalledTimes(1)     // the refused ceilings sent nothing
})

it('never lands a prepare answer under a run the screen has since moved to', async () => {
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  let land: (v: unknown) => void = () => {}
  prepareRunCode.mockImplementation(() => new Promise(resolve => { land = resolve }) as never)
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))

  // App does not key this screen, so a deep link to another run changes the prop under the
  // in-flight request.
  getFeatureRunDetail.mockResolvedValue({ ...DETAIL, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))

  land({
    job_id: 'cgj-stale', created: true, declaration_source_job_id: 'cgj-old',
    detail: 'recorded',
    run: { ...THREE_CHOSEN, jobs: [{ job_id: 'cgj-stale', status: 'REQUESTED',
      requested_at: '2026-08-23T01:00:00+00:00', actions: [] }] },
  })

  // The previous run's record — and the job it just minted — must not appear under this address.
  await waitFor(() => expect(screen.getByText(/No code-generation job has been requested/))
    .toBeInTheDocument())
  expect(screen.queryByText('cgj-stale')).toBeNull()
  expect(screen.queryByTestId('prepare-status')).toBeNull()
})

it('does not leave the new run’s controls greyed out by the previous run’s request', async () => {
  // ▲ `busy` was the ONE piece of gesture state missing from the run-change reset list. A deep
  // link away while a POST is still in flight left the NEW run's checkbox and its one button
  // disabled by a request belonging to a run this screen has left — and with nothing on screen to
  // explain it, because the only sentence that ever explains a disabled control here is the
  // server's, and the server refused nothing.
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  prepareRunCode.mockImplementation(() => new Promise(() => {}) as never)   // never lands
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))
  // In flight, and correctly disabled WHILE this run is the one on screen.
  expect(screen.getByRole('button', { name: /Prepare formulas/ })).toBeDisabled()

  getFeatureRunDetail.mockResolvedValue({ ...THREE_CHOSEN, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))

  // The new run is usable: nothing here is waiting on anything.
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeEnabled())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  expect(screen.getByRole('button', { name: /Prepare formulas/ })).toBeEnabled()
})

// ▲ AND THE ERROR BRANCH, which the success case above does not cover (carried from Task 4). A
// refusal of the PREVIOUS run's gesture landing here would paint an alert about work this run
// never asked for — and the reader has no way to tell whose refusal they are reading.
it('never paints a refusal that belongs to a run the screen has since left', async () => {
  getFeatureRunDetail.mockResolvedValue(THREE_CHOSEN)
  let refuse: (e: unknown) => void = () => {}
  prepareRunCode.mockImplementation(() => new Promise((_, reject) => { refuse = reject }) as never)
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByRole('checkbox', { name: /o2/ })).toBeInTheDocument())
  await userEvent.click(screen.getByRole('checkbox', { name: /o2/ }))
  await userEvent.click(screen.getByRole('button', { name: /Prepare formulas/ }))

  getFeatureRunDetail.mockResolvedValue({ ...DETAIL, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))

  refuse(new api.ApiError(409, 'no code-generation job has ever been requested for this run', null,
    'RUN_BUILD_DECLARATION_ABSENT', { code: 'RUN_BUILD_DECLARATION_ABSENT' }))

  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.queryByRole('alert')).toBeNull()
  // ...and a cost quote is the same stale answer wearing a different shape: it would put the
  // previous run's ceiling form under this address.
  expect(screen.queryByText(/Confirm the AI cost ceiling/)).toBeNull()
})

// ── the retry gesture (spec §R4.2) ──────────────────────────────────────────────────────────────
// A candidate whose only attempt bought nothing. Whether it may be bought AGAIN is the server's
// derivation — an approved regeneration, a ceiling with budget left, no withdrawal, no live attempt
// holding the identity slot — and this screen renders that answer rather than folding its own.
const OFFERED: api.RunAuthoringRow = { ...ATTEMPT_FAILED, retryable: true, retry_blockers: [] }
const REFUSED: api.RunAuthoringRow = {
  ...ATTEMPT_FAILED,
  retryable: false,
  retry_blockers: [{
    code: 'FORMULA_DRAFT_NOT_AN_ANSWER',
    detail: 'the previous attempt at this exact formula failed; re-authoring spends again, so it '
      + 'needs an approved, cost-confirmed regeneration exception',
  }],
}

function detailWith(row: api.RunAuthoringRow): api.FeatureRunDetail {
  return { ...DETAIL, authoring: { current: [{ ...row, resolved: false }], history: [row] } }
}

it('offers Retry only where the server says the attempt may be bought again', async () => {
  getFeatureRunDetail.mockResolvedValue(detailWith(OFFERED))
  retryRunAuthoring.mockResolvedValue({
    formula_draft_id: 'd9', created: true, formula_strategy: 'LLM_AUTHORED',
    strategy_warnings: [], detail: 'the retry was recorded; a worker authors it',
    run: detailWith({ ...ATTEMPT_FAILED, ...NO_RETRY }),
  })
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())

  // Rendering alone must spend nothing: the row is on screen and no request has gone out.
  expect(retryRunAuthoring).not.toHaveBeenCalled()
  const retry = screen.getByRole('button', { name: /Retry attempt d0/ })
  expect(retry).toBeEnabled()

  await userEvent.click(retry)

  expect(retryRunAuthoring).toHaveBeenCalledTimes(1)
  expect(retryRunAuthoring).toHaveBeenCalledWith(RUN_ID, { formula_draft_id: 'd0' })
  // The run comes back READ from the store, so the attempt table below is the store's own answer.
  await waitFor(() => expect(screen.getByTestId('retry-status')).toHaveTextContent('d9'))
})

it('disables Retry with the server’s own blockers, and the click reaches no request', async () => {
  getFeatureRunDetail.mockResolvedValue(detailWith(REFUSED))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())

  const row = rowOf('d0')
  // Verbatim, code and sentence: this screen holds no policy and owns no words for a refusal.
  expect(within(row).getByText('FORMULA_DRAFT_NOT_AN_ANSWER')).toBeInTheDocument()
  expect(within(row).getByText(/cost-confirmed regeneration exception/)).toBeInTheDocument()
  expect(within(row).getByRole('button', { name: /Retry attempt d0/ })).toBeDisabled()

  // ...and the pair holds. A screen that only LOOKED disabled would still send this.
  await userEvent.click(within(row).getByRole('button', { name: /Retry attempt d0/ }))
  expect(retryRunAuthoring).not.toHaveBeenCalled()
})

it('offers no retry control where the question is not asked at all', async () => {
  // A READY attempt bought an answer, so "retry" is not the question — and `retry_blockers` is
  // empty because there is nothing to explain. A greyed-out button here would invite the reader to
  // hunt for a refusal nobody made.
  getFeatureRunDetail.mockResolvedValue(detailWith({ ...DRAFT_1, eligibility: 'current',
    retirement_reason: null }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d1')).toBeInTheDocument())

  expect(within(rowOf('d1')).queryByRole('button')).toBeNull()
})

// A candidate somebody WITHDREW, whose retry an approved regeneration nevertheless names. The
// server offers the click and attaches the governed WARN that says what it overrides.
const OVERRIDDEN: api.RunAuthoringRow = {
  ...ATTEMPT_FAILED,
  retryable: true,
  retry_blockers: [],
  retry_warnings: [{
    code: 'RETIREMENT_OVERRIDDEN',
    detail: 'somebody withdrew this candidate, and an approved regeneration NAMES that withdrawal '
      + '— so the retry is admissible and will proceed over a deliberate withdrawal',
  }],
}

it('says what a governed override overrides, beside a control it does NOT disable', async () => {
  // ▲ THE WARN DISPOSITION, RENDERED. `RETIREMENT_OVERRIDDEN` reached nobody: the server kept only
  // the blockers half of its own (blockers, warnings) answer, so a retryable candidate under a
  // governance override looked exactly like one nobody had ever withdrawn. It must render — and it
  // must NOT render as a refusal: the whole content of a WARN is proceed-with-knowledge.
  getFeatureRunDetail.mockResolvedValue(detailWith(OVERRIDDEN))
  retryRunAuthoring.mockResolvedValue({
    formula_draft_id: 'd9', created: true, formula_strategy: 'LLM_AUTHORED',
    strategy_warnings: ['RETIREMENT_OVERRIDDEN'],
    detail: 'the retry was recorded; a worker authors it',
    run: detailWith({ ...ATTEMPT_FAILED, ...NO_RETRY }),
  })
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())

  const row = rowOf('d0')
  // Verbatim, code and sentence — this screen owns no vocabulary for a governed decision.
  expect(within(row).getByText('RETIREMENT_OVERRIDDEN')).toBeInTheDocument()
  expect(within(row).getByText(/will proceed over a deliberate withdrawal/)).toBeInTheDocument()
  // ...and it is a WARNING, not a blocker: it lives in its own element, and the button is ENABLED.
  expect(within(row).getByTestId('retry-warning')).toBeInTheDocument()
  expect(within(row).getByRole('button', { name: /Retry attempt d0/ })).toBeEnabled()

  // ▲ AND THE ACT'S OWN WARNINGS SURVIVE THE ANSWER. The 202 carries `strategy_warnings`, which
  // were computed, sent, and thrown away here — indistinguishable from never having been derived,
  // for the one person entitled to know a governance override just fired.
  await userEvent.click(within(row).getByRole('button', { name: /Retry attempt d0/ }))
  await waitFor(() =>
    expect(screen.getByTestId('retry-status')).toHaveTextContent('RETIREMENT_OVERRIDDEN'))
  expect(screen.getByTestId('retry-status')).toHaveTextContent('d9')
})

it('shows a retry refusal in the server’s words and records nothing of its own', async () => {
  getFeatureRunDetail.mockResolvedValue(detailWith(OFFERED))
  retryRunAuthoring.mockRejectedValue(new api.ApiError(
    409, 'a live attempt already holds this identity', null, 'RETRY_ATTEMPT_ALREADY_LIVE',
    { code: 'RETRY_ATTEMPT_ALREADY_LIVE' }))
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())

  await userEvent.click(screen.getByRole('button', { name: /Retry attempt d0/ }))

  expect(await screen.findByRole('alert'))
    .toHaveTextContent('a live attempt already holds this identity')
  expect(screen.queryByTestId('retry-status')).toBeNull()
})

it('never lands a retry answer under a run the screen has since moved to', async () => {
  getFeatureRunDetail.mockResolvedValue(detailWith(OFFERED))
  let land: (v: unknown) => void = () => {}
  retryRunAuthoring.mockImplementation(() => new Promise(resolve => { land = resolve }) as never)
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: /Retry attempt d0/ }))

  getFeatureRunDetail.mockResolvedValue({ ...DETAIL, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))

  land({
    formula_draft_id: 'd-stale', created: true, formula_strategy: 'LLM_AUTHORED',
    strategy_warnings: [], detail: 'recorded', run: detailWith(OFFERED),
  })

  await waitFor(() => expect(screen.getByText('d1')).toBeInTheDocument())
  expect(screen.queryByTestId('retry-status')).toBeNull()
  expect(screen.queryByText('d0')).toBeNull()
})

// ▲ AND THE RETRY ERROR BRANCH, the twin of the prepare one. Deleting its guard leaves every other
// test green: a refusal about the PREVIOUS run's attempt would paint an alert here, and the reader
// has no way to tell that the sentence is about a draft this run does not have.
it('never paints a retry refusal that belongs to a run the screen has left', async () => {
  getFeatureRunDetail.mockResolvedValue(detailWith(OFFERED))
  let refuse: (e: unknown) => void = () => {}
  retryRunAuthoring.mockImplementation(
    () => new Promise((_, reject) => { refuse = reject }) as never)
  const { rerender } = render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('d0')).toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: /Retry attempt d0/ }))

  getFeatureRunDetail.mockResolvedValue({ ...DETAIL, generation_run_id: 'grun_other' })
  rerender(<RunDetailScreen runId="grun_other" />)
  await waitFor(() => expect(getFeatureRunDetail).toHaveBeenCalledWith('grun_other'))

  refuse(new api.ApiError(409, 'a live attempt already holds this identity', null,
    'RETRY_ATTEMPT_ALREADY_LIVE', { code: 'RETRY_ATTEMPT_ALREADY_LIVE' }))

  await waitFor(() => expect(screen.getByText('d1')).toBeInTheDocument())
  expect(screen.queryByRole('alert')).toBeNull()
})

// ── the stage cell, when a stage has BOTH a reason and a count (carried from Task 4) ─────────────
it('separates a stage’s reason code from its count instead of running them together', async () => {
  getFeatureRunDetail.mockResolvedValue({
    ...DETAIL,
    rail: [{ stage: 'BIND_SELECTIONS', state: 'UNAVAILABLE', detail: '0 of 3 bound',
      reason_code: 'BUILD_SET_DECLARATION_WITHHELD_PRE_PIN' }],
  })
  render(<RunDetailScreen runId={RUN_ID} />)
  await waitFor(() => expect(screen.getByText('BIND_SELECTIONS')).toBeInTheDocument())

  // Concatenated, the two read as one nonsense token: `…PRE_PIN0 of 3 bound`.
  const why = rowOf('BIND_SELECTIONS').querySelectorAll('td')[2]
  expect(why.textContent).toBe('BUILD_SET_DECLARATION_WITHHELD_PRE_PIN — 0 of 3 bound')
})
