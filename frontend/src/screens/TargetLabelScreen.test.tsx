import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ApiError } from '../api'
import { TargetLabelScreen } from './TargetLabelScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listCatalogs: vi.fn(),
    listTargetEntities: vi.fn(),
    proposeTarget: vi.fn(),
    describeTarget: vi.fn(),
    previewTargetSql: vi.fn(),
    registerTarget: vi.fn(),
    listTargets: vi.fn(),
  }
})
const listCatalogs = vi.mocked(api.listCatalogs)
const listTargetEntities = vi.mocked(api.listTargetEntities)
const proposeTarget = vi.mocked(api.proposeTarget)
const describeTarget = vi.mocked(api.describeTarget)
const previewTargetSql = vi.mocked(api.previewTargetSql)
const registerTarget = vi.mocked(api.registerTarget)
const listTargets = vi.mocked(api.listTargets)

const DRAFT: api.TargetDraft = {
  shape: 'state_change',
  fields: {
    name: 'tgt_npe_90d', entity: 'customer', anchor_catalog: 'cib',
    grain_ref: 'public.bo_cib_customer.cust_num',
    as_of_ref: 'public.bo_cib_customer.business_dt',
    window_days: 90, as_of_frequency: 'monthly', label_type: 'binary',
    operator: '>=', threshold: 1,
    column_ref: 'public.bo_cib_customer.cust_perf_nonperf_flg',
  },
  needs_input: ['from_values', 'to_values'],
  notes: { from_values: 'no_value_profile', to_values: 'no_value_profile' },
}

beforeEach(() => {
  vi.clearAllMocks()
  listCatalogs.mockResolvedValue({ catalogs: [{ source: 'cib', tables: 1, columns: 9 }] })
  listTargetEntities.mockResolvedValue([
    { entity: 'customer', spine_table: 'bo_cib_customer',
      spine_ref: 'public.bo_cib_customer.cust_num' },
  ])
  listTargets.mockResolvedValue([])
  proposeTarget.mockResolvedValue({ existing: [], draft: DRAFT })
  describeTarget.mockResolvedValue({ reads_as: 'tgt_npe_90d: one row per customer…',
    incomplete: null })
  previewTargetSql.mockResolvedValue({ sql: 'WITH as_of_dates AS (…)', incomplete: null })
  registerTarget.mockResolvedValue({
    definition_id: 'def-1', name: 'tgt_npe_90d', rule: {}, reads_as: 'one row per customer…',
    near_duplicates: [],
  })
})

async function proposed(props: Parameters<typeof TargetLabelScreen>[0] = {}) {
  const user = userEvent.setup()
  render(<TargetLabelScreen {...props} />)
  await waitFor(() => expect(listTargetEntities).toHaveBeenCalled())
  await user.type(screen.getByLabelText(/what are you trying to predict/i),
    'which customers go non-performing')
  await user.click(screen.getByRole('button', { name: /propose a target/i }))
  await waitFor(() => expect(proposeTarget).toHaveBeenCalled())
  return user
}

it('offers entities the CATALOG can anchor a label on, not a free text box', async () => {
  render(<TargetLabelScreen />)
  await waitFor(() => expect(listTargetEntities).toHaveBeenCalledWith('cib'))
  expect(screen.getByRole('combobox', { name: /entity/i })).toHaveValue('customer')
})

it('says a catalog cannot anchor a label rather than rendering an empty dropdown', async () => {
  listTargetEntities.mockResolvedValue([])
  render(<TargetLabelScreen />)
  expect(await screen.findByText(/no keyed spine table/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /propose a target/i })).not.toBeInTheDocument()
})

it('fills the fields the catalog justifies', async () => {
  await proposed()
  expect(screen.getByLabelText(/window \(days\)/i)).toHaveValue('90')
  expect(screen.getByLabelText(/flag column/i)).toHaveValue(
    'public.bo_cib_customer.cust_perf_nonperf_flg')
})

it('explains each BLANK in plain english rather than showing its reason code', async () => {
  await proposed()
  // BOTH blanks carry the explanation — one explained blank and one bare one is the failure.
  expect(screen.getAllByText(/nothing in the catalog records what this column contains/i))
    .toHaveLength(2)
  expect(screen.queryByText('no_value_profile')).not.toBeInTheDocument()
})

it('marks a blank field as needing input so it cannot be missed', async () => {
  await proposed()
  expect(screen.getByLabelText(/starting values/i)).toHaveAttribute('aria-invalid', 'true')
})

it('shows the SENTENCE, which is what a person actually approves', async () => {
  await proposed()
  // Matched on the label NAME too: the entity picker also reads "one row per customer", and an
  // assertion that cannot tell them apart would pass with no sentence rendered at all.
  expect(await screen.findByText(/tgt_npe_90d: one row per customer/i)).toBeInTheDocument()
})

it('shows the DERIVATION LOGIC so the person sees how the label will be built', async () => {
  const user = await proposed()
  await user.click(screen.getByRole('button', { name: /show the sql/i }))
  expect(await screen.findByText(/WITH as_of_dates AS/)).toBeInTheDocument()
})

it('keeps labels the organisation ALREADY decided on separate from the draft', async () => {
  proposeTarget.mockResolvedValue({
    existing: [{ name: 'tgt_npe_60d', description: 'credit deterioration', window_days: 60,
      match_terms: ['non-performing'] }],
    draft: DRAFT,
  })
  await proposed()
  expect(screen.getByText(/already registered/i)).toBeInTheDocument()
  expect(screen.getByText('tgt_npe_60d')).toBeInTheDocument()
})

it('reports a failed proposal instead of showing an empty form', async () => {
  proposeTarget.mockResolvedValue({ existing: [], draft: null })
  await proposed()
  expect(screen.getByText(/could not propose/i)).toBeInTheDocument()
})

it('refuses to register while a blank is still unfilled', async () => {
  await proposed()
  expect(screen.getByRole('button', { name: /register/i })).toBeDisabled()
})

it('registers the rule, the original proposal and the comment', async () => {
  const user = await proposed()
  await user.type(screen.getByLabelText(/starting values/i), 'Performing')
  await user.type(screen.getByLabelText(/outcome values/i), 'Non-performing')
  await user.type(screen.getByLabelText(/why you changed/i), '90 because the desk reviews monthly')
  // The button is gated on the SENTENCE, which is debounced: a person reads what the label means
  // and then approves it. Clicking before it catches up is not a flow anyone has.
  await waitFor(() => expect(screen.getByRole('button', { name: /register/i })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: /register/i }))
  await waitFor(() => expect(registerTarget).toHaveBeenCalled())
  const body = registerTarget.mock.calls[0][0]
  expect(body.rule.from_values).toEqual(['Performing'])
  expect(body.author_comment).toContain('desk reviews monthly')
  expect(body.proposed_draft).toEqual(DRAFT)
})

it('records what the tool proposed even after the person edits it', async () => {
  const user = await proposed()
  await user.clear(screen.getByLabelText(/window \(days\)/i))
  await user.type(screen.getByLabelText(/window \(days\)/i), '180')
  await user.type(screen.getByLabelText(/starting values/i), 'Performing')
  await user.type(screen.getByLabelText(/outcome values/i), 'Non-performing')
  await waitFor(() => expect(screen.getByRole('button', { name: /register/i })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: /register/i }))
  await waitFor(() => expect(registerTarget).toHaveBeenCalled())
  const body = registerTarget.mock.calls[0][0]
  expect(body.rule.window_days).toBe(180)
  expect((body.proposed_draft as unknown as api.TargetDraft).fields.window_days).toBe(90)
})

it('collapses a burst of typing into ONE describe call, not one per keystroke', async () => {
  const user = await proposed()
  describeTarget.mockClear()
  await user.type(screen.getByLabelText(/starting values/i), 'Performing')
  await waitFor(() => expect(describeTarget).toHaveBeenCalled())
  // Ten characters typed. Without debouncing this is ten round trips whose answers can also land
  // out of order, leaving a sentence describing a rule the person has already moved past.
  expect(describeTarget.mock.calls.length).toBeLessThan(4)
})

const EVENT_DRAFT: api.TargetDraft = {
  shape: 'event_window',
  fields: {
    name: 'tgt_start_fx_90d', entity: 'customer', anchor_catalog: 'cib',
    grain_ref: 'public.bo_cib_customer.cust_num',
    as_of_ref: 'public.bo_cib_customer.business_dt',
    window_days: 90, as_of_frequency: 'monthly', label_type: 'binary',
    operator: '>=', threshold: 1,
    event_catalog: 'ftr', event_table: 'txns', event_date_ref: 'public.txns.tran_date',
    join_left: 'public.bo_cib_customer.cust_num', join_right: 'public.txns.cif_id',
    aggregate: 'count', population_having: 'none',
    event_filters: [{ column_ref: 'public.txns.tran_crncy', op: '!=', value: 'AED' }],
  },
  needs_input: ['population_lookback_days'],
  notes: { population_lookback_days: 'not_stated' },
}

it('shows WHICH EVENTS COUNT, so a label that counts everything is visible', async () => {
  proposeTarget.mockResolvedValue({ existing: [], draft: EVENT_DRAFT })
  await proposed()
  expect(screen.getByDisplayValue('public.txns.tran_crncy')).toBeInTheDocument()
  expect(screen.getByDisplayValue('AED')).toBeInTheDocument()
})

it('says plainly when an event label counts EVERY row', async () => {
  proposeTarget.mockResolvedValue({
    existing: [],
    draft: { ...EVENT_DRAFT, fields: { ...EVENT_DRAFT.fields, event_filters: [] } },
  })
  await proposed()
  expect(screen.getByText(/counts every row/i)).toBeInTheDocument()
})

it('submits the edited filters with the rule', async () => {
  proposeTarget.mockResolvedValue({ existing: [], draft: EVENT_DRAFT })
  const user = await proposed()
  await user.clear(screen.getByDisplayValue('AED'))
  await user.type(screen.getByLabelText(/condition 1 value/i), 'USD')
  await user.type(screen.getByLabelText(/lookback/i), '180')
  await waitFor(() => expect(screen.getByRole('button', { name: /register/i })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: /register/i }))
  await waitFor(() => expect(registerTarget).toHaveBeenCalled())
  const filters = registerTarget.mock.calls[0][0].rule.event_filters as { value: string }[]
  expect(filters[0].value).toBe('USD')
})

it('says you lack the ROLE rather than blaming the catalog', async () => {
  // The default dev session is data_owner, which has no feature:generate — so this is what a
  // person actually hits on first open. Reporting "no keyed spine table" would be a wrong reason
  // for a blank: it blames the data for a permissions problem and sends them to fix the catalog.
  listTargetEntities.mockRejectedValue(new ApiError(403, 'missing permission: feature:generate'))
  render(<TargetLabelScreen />)
  expect(await screen.findByText(/feature_engineer/i)).toBeInTheDocument()
  expect(screen.queryByText(/no keyed spine table/i)).not.toBeInTheDocument()
})

// ══ progress while the model is thinking ════════════════════════════════════════════════════════
// A real call over the whole catalog takes 20-40 seconds. With only a disabled button to look at,
// a person testing this reasonably concludes it is broken and reloads — losing the draft they were
// waiting for.

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

async function startProposing() {
  const user = userEvent.setup()
  const pending = deferred<api.TargetProposal>()
  proposeTarget.mockReturnValue(pending.promise)
  render(<TargetLabelScreen />)
  await waitFor(() => expect(listTargetEntities).toHaveBeenCalled())
  await user.type(screen.getByLabelText(/what are you trying to predict/i), 'which customers…')
  await user.click(screen.getByRole('button', { name: /propose a target/i }))
  return { user, pending }
}

it('says what it is doing while the model is thinking', async () => {
  const { pending } = await startProposing()
  const status = await screen.findByRole('status')
  expect(status).toHaveTextContent(/reading/i)
  // Naming the catalog and its size is the difference between "something is happening" and
  // "this is the work I asked for".
  expect(status).toHaveTextContent(/cib/)
  expect(status).toHaveTextContent(/9 columns/)
  pending.resolve({ existing: [], draft: DRAFT })
})

it('sets an expectation for HOW LONG, so a slow call does not read as a hung one', async () => {
  const { pending } = await startProposing()
  expect(await screen.findByRole('status')).toHaveTextContent(/seconds/i)
  pending.resolve({ existing: [], draft: DRAFT })
})

it('the button says it is working rather than looking merely disabled', async () => {
  const { pending } = await startProposing()
  expect(await screen.findByRole('button', { name: /proposing/i })).toBeDisabled()
  pending.resolve({ existing: [], draft: DRAFT })
})

it('clears the progress once the draft arrives', async () => {
  const { pending } = await startProposing()
  await screen.findByRole('status')
  pending.resolve({ existing: [], draft: DRAFT })
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
})

it('clears the progress when the call FAILS, rather than appearing to run for ever', async () => {
  const { pending } = await startProposing()
  await screen.findByRole('status')
  pending.reject(new ApiError(500, 'boom'))
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  expect(screen.getByText(/boom/)).toBeInTheDocument()
})

// ══ reached from feature generation, not from a tab ══════════════════════════════════════════════

it('starts from the objective the workbench already has, rather than asking again', async () => {
  render(<TargetLabelScreen initialHypothesis="which customers go non-performing"
                            initialSource="cib" />)
  await waitFor(() => expect(listTargetEntities).toHaveBeenCalledWith('cib'))
  expect(screen.getByLabelText(/what are you trying to predict/i))
    .toHaveValue('which customers go non-performing')
})

it('hands you back to the run that needed the label', async () => {
  const onBack = vi.fn()
  const user = await proposed({ onBack })
  await user.type(screen.getByLabelText(/starting values/i), 'Performing')
  await user.type(screen.getByLabelText(/outcome values/i), 'Non-performing')
  await waitFor(() => expect(screen.getByRole('button', { name: /register this/i })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: /register this/i }))
  await waitFor(() => expect(registerTarget).toHaveBeenCalled())
  await user.click(await screen.findByRole('button', { name: /back to feature generation/i }))
  expect(onBack).toHaveBeenCalled()
})

it('offers no way back when it was NOT reached from a run', async () => {
  const user = await proposed()
  await user.type(screen.getByLabelText(/starting values/i), 'Performing')
  await user.type(screen.getByLabelText(/outcome values/i), 'Non-performing')
  await waitFor(() => expect(screen.getByRole('button', { name: /register this/i })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: /register this/i }))
  await waitFor(() => expect(registerTarget).toHaveBeenCalled())
  expect(screen.queryByRole('button', { name: /back to feature generation/i })).toBeNull()
})
