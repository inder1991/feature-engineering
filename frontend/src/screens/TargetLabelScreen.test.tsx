import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
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

async function proposed() {
  const user = userEvent.setup()
  render(<TargetLabelScreen />)
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
