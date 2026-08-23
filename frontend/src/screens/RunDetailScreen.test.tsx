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
  return { ...actual, getFeatureRunDetail: vi.fn(), prepareRunCode: vi.fn() }
})
const getFeatureRunDetail = vi.mocked(api.getFeatureRunDetail)
const prepareRunCode = vi.mocked(api.prepareRunCode)

const RUN_ID = 'grun_01M02SAZQQQQ'

// A run whose only draft SUCCEEDED and was then withdrawn (the §6.7 two-axis case), and a rail
// carrying one worked stage plus two sockets with their reasons. One attempt, so it is also its
// candidate's current answer — the shape every run had before 1107 made a second attempt possible.
const DRAFT_1: api.RunAuthoringRow = {
  formula_draft_id: 'd1', considered_revision_id: 'c', option_id: 'o1', state: 'READY',
  rail_state: 'SUCCEEDED', eligibility: 'withdrawn', retirement_reason: 'CANDIDATE_SUPERSEDED',
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
  // The absent controls are still the design: preparing formulas is the ONE thing this screen can
  // write, and nothing here re-runs, retries, executes or forks a run.
  expect(screen.queryAllByRole('button').filter(b =>
    /re-run|retry|execute|fork/i.test(b.textContent ?? ''))).toHaveLength(0)
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
