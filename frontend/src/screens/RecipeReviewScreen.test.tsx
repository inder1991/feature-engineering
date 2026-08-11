import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RecipeReviewScreen } from './RecipeReviewScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getRecipeReviewSummary: vi.fn(),
    getRecipeDetail: vi.fn(),
    getRecipeReviews: vi.fn(),
    postRecipeReview: vi.fn(),
  }
})
const getRecipeReviewSummary = vi.mocked(api.getRecipeReviewSummary)
const getRecipeDetail = vi.mocked(api.getRecipeDetail)
const getRecipeReviews = vi.mocked(api.getRecipeReviews)
const postRecipeReview = vi.mocked(api.postRecipeReview)

// ── fixtures — shaped exactly as routes/recipe_review.py emits them ─────────────────────────────

const HASH = 'a'.repeat(64)
const OLD_HASH = 'b'.repeat(64)

function validity(over: Partial<api.RecipeReviewValidity> = {}): api.RecipeReviewValidity {
  return {
    current: false,
    required_roles: ['banking_sme', 'data_semantic_owner', 'formula_engineering'],
    approved_roles: [],
    missing_roles: ['banking_sme', 'data_semantic_owner', 'formula_engineering'],
    blocking_decisions: [],
    single_identity_violation: false,
    ...over,
  }
}

function row(over: Partial<api.RecipeReviewSummaryRow> = {}): api.RecipeReviewSummaryRow {
  return {
    recipe_id: 'posted_debit_amount',
    family: 'transaction_foundation',
    readiness: 'FORMULA_AUTHORABLE',
    computation_kind: 'deterministic_formula',
    display_label: 'Posted debit amount',
    leakage_classification: 'standard',
    recipe_revision_hash: HASH,
    validity: validity(),
    ...over,
  }
}

const SUMMARY: api.RecipeReviewSummary = {
  recipes: [
    row(),
    row({
      recipe_id: 'utilization_level', family: 'credit', display_label: 'Utilization level',
      validity: validity({
        current: true,
        approved_roles: ['banking_sme', 'data_semantic_owner', 'formula_engineering'],
        missing_roles: [],
      }),
    }),
    row({
      recipe_id: 'card_testing_velocity', family: 'fraud', display_label: 'Card testing velocity',
      validity: validity({
        missing_roles: ['data_semantic_owner', 'formula_engineering'],
        blocking_decisions: ['banking_sme:changes_required'],
      }),
    }),
  ],
  total: 3,
}

const DETAIL: api.RecipeDetail = {
  recipe_revision_hash: HASH,
  required_reviewer_roles: ['banking_sme', 'data_semantic_owner', 'formula_engineering'],
  recipe: {
    recipe_id: 'posted_debit_amount',
    revision: 1,
    family: 'transaction_foundation',
    primary_objective: 'transaction_analytics.flow.outflow',
    supporting_objectives: [],
    business_definition: 'Total posted debit amount over the window.',
    decision_context: 'The foundation outflow measure.',
    computation_kind: 'deterministic_formula',
    readiness: 'FORMULA_AUTHORABLE',
    source_grain: 'transaction',
    output_grain: 'account',
    output: {
      output_id: 'posted_debit_amount', display_label: 'Posted debit amount',
      output_type: 'measure_aggregate', additivity: 'additive', unit_kind: 'monetary',
      unit_policy: '', currency_policy: 'account reporting currency via governed conversion',
      null_input_policy: 'null amounts are excluded and counted',
      empty_population_policy: 'zero with populated flag', zero_denominator_policy: '',
      valid_range: '', aggregation_over_entity: 'sum', aggregation_over_time: 'sum',
    },
    operands: [
      { role: 'amount', concept: 'transaction_amount', operand_class: 'measure', required: true,
        economic_role: '', status_policy_ref: 'eligible_status:posted_only',
        temporal_role: '', unit_expectation: 'monetary', currency_expectation: 'per_row' },
      { role: 'event_ts', concept: 'event_timestamp', operand_class: 'temporal', required: true,
        economic_role: '', status_policy_ref: '', temporal_role: 'event_time',
        unit_expectation: '', currency_expectation: '' },
    ],
    parameters: [{ name: 'window', parameter_class: 'semantic', allowed_values: [30, 90],
      identity_projection: 'window={value}d', governed_policy_ref: '' }],
    temporal: {
      anchor_kind: 'observation_window', window_basis: 'event time', window_unit: 'days',
      window_parameter: 'window', timezone_policy: 'booking timezone', calendar_policy: '',
      cutoff_inclusivity: 'inclusive', snapshot_policy: '', late_arrival_policy: 'not set here',
    },
    eligibility: {
      included: 'posted debit transactions', excluded: 'reversals net out',
      policy_refs: ['direction_sign:debit_credit_authority', 'reversal_correction:net_before_sum'],
    },
    leakage: { classification: 'standard', permitted_stages: [], prohibited_stages: [] },
    formula: { formula_schema_version: 'formula-v2', expectation_ref: 'posted_debit_amount',
      result_class: 'sum' },
    conceptual_reason: '',
    model_feature_ref: '',
    replaces_legacy_ids: ['total_debit_amount'],
  },
}

function reviewsFixture(over: Partial<api.RecipeReviews> = {}): api.RecipeReviews {
  return {
    recipe_id: 'posted_debit_amount',
    recipe_revision_hash: HASH,
    validity: validity(),
    events: [],
    ...over,
  }
}

beforeEach(() => {
  getRecipeReviewSummary.mockReset()
  getRecipeDetail.mockReset()
  getRecipeReviews.mockReset()
  postRecipeReview.mockReset()
  getRecipeReviewSummary.mockResolvedValue(SUMMARY)
  getRecipeDetail.mockResolvedValue(DETAIL)
  getRecipeReviews.mockResolvedValue(reviewsFixture())
})

describe('RecipeReviewScreen', () => {
  it('lists every recipe grouped by family, with the three honest validity states', async () => {
    render(<RecipeReviewScreen />)
    expect(
      await screen.findByText('3 governed recipes · 1 fully approved at their current revision'),
    ).toBeInTheDocument()
    const queue = screen.getByRole('complementary', { name: 'Recipe queue' })
    expect(within(queue).getByText('transaction foundation · 1')).toBeInTheDocument()
    expect(within(queue).getByText('Awaiting all 3 roles')).toBeInTheDocument()
    // Scoped to the row: 'Approved' also exists as a filter <option>.
    const approvedRow = within(queue).getByRole('button', { name: /Utilization level/ })
    expect(within(approvedRow).getByText('Approved')).toBeInTheDocument()
    // A recorded changes_required decision is a BLOCK with the role and decision named — the
    // queue never renders it as generic failure.
    expect(within(queue).getByText('Blocked · banking sme:changes required')).toBeInTheDocument()
  })

  it('filters by review status client-side over the one fetch', async () => {
    render(<RecipeReviewScreen />)
    await screen.findByText('Posted debit amount')
    await userEvent.selectOptions(screen.getByLabelText('Review status'), 'approved')
    expect(screen.queryByText('Posted debit amount')).not.toBeInTheDocument()
    expect(screen.getByText('Utilization level')).toBeInTheDocument()
    expect(getRecipeReviewSummary).toHaveBeenCalledTimes(1)
  })

  it('opens the definition a reviewer signs: contract fields, required approvals, history', async () => {
    render(<RecipeReviewScreen />)
    await userEvent.click(await screen.findByText('Posted debit amount'))
    const panel = screen.getByRole('region', { name: 'Recipe under review' })
    expect(
      await within(panel).findByText('Total posted debit amount over the window.'),
    ).toBeInTheDocument()
    // Every required role renders with its state — "no decision yet" is the honest start.
    expect(within(panel).getAllByText('no decision yet')).toHaveLength(3)
    // The contract, not a paraphrase: policies, operands, formula reference.
    expect(
      within(panel).getByText('account reporting currency via governed conversion'),
    ).toBeInTheDocument()
    expect(within(panel).getByText('direction_sign:debit_credit_authority')).toBeInTheDocument()
    expect(within(panel).getByText(/eligible_status:posted_only/)).toBeInTheDocument()
    expect(within(panel).getByText('formula-v2')).toBeInTheDocument()
    expect(
      within(panel).getByText('No decisions recorded yet — unreviewed is the starting state.'),
    ).toBeInTheDocument()
  })

  it('records a decision against the on-screen revision hash, then re-reads the fold', async () => {
    postRecipeReview.mockResolvedValue({ event_id: 'rre-1', recipe_revision_hash: HASH })
    getRecipeReviews
      .mockResolvedValueOnce(reviewsFixture())
      .mockResolvedValueOnce(reviewsFixture({
        validity: validity({ approved_roles: ['banking_sme'],
          missing_roles: ['data_semantic_owner', 'formula_engineering'] }),
        events: [{ event_id: 'rre-1', recipe_revision_hash: HASH, decision: 'approved',
          reviewer: 'user:priya', reviewer_role: 'banking_sme', rationale: 'shape is right',
          gold_corpus_refs: [], policy_dependencies: [], supersedes_event_id: null }],
      }))
    render(<RecipeReviewScreen />)
    await userEvent.click(await screen.findByText('Posted debit amount'))
    const panel = screen.getByRole('region', { name: 'Recipe under review' })
    await within(panel).findByText('Total posted debit amount over the window.')

    // The submit stays disabled until a role AND a rationale exist — an unattributed or
    // unexplained decision is not recordable from here.
    const button = within(panel).getByRole('button', { name: 'Record decision' })
    expect(button).toBeDisabled()
    await userEvent.selectOptions(within(panel).getByLabelText('Reviewer role'), 'banking_sme')
    expect(button).toBeDisabled()
    await userEvent.type(within(panel).getByLabelText('Rationale'), 'shape is right')
    expect(button).toBeEnabled()
    await userEvent.click(button)

    await waitFor(() => expect(postRecipeReview).toHaveBeenCalledWith('posted_debit_amount', {
      decision: 'approved',
      reviewerRole: 'banking_sme',
      reviewedRevisionHash: HASH,
      rationale: 'shape is right',
    }))
    // The fold is re-read, never predicted: reviews again AND the queue again.
    await within(panel).findByText('approved — user:priya')
    expect(getRecipeReviewSummary).toHaveBeenCalledTimes(2)
  })

  it('surfaces a 409 as the server said it and offers a reload of the current definition', async () => {
    postRecipeReview.mockRejectedValue(new api.ApiError(
      409, 'the definition changed since you reviewed it: reviewed aaaa…, live cccc… — '
        + 're-review the current revision'))
    render(<RecipeReviewScreen />)
    await userEvent.click(await screen.findByText('Posted debit amount'))
    const panel = screen.getByRole('region', { name: 'Recipe under review' })
    await within(panel).findByText('Total posted debit amount over the window.')
    await userEvent.selectOptions(within(panel).getByLabelText('Reviewer role'), 'banking_sme')
    await userEvent.type(within(panel).getByLabelText('Rationale'), 'looks right')
    await userEvent.click(within(panel).getByRole('button', { name: 'Record decision' }))
    expect(
      await within(panel).findByText(/the definition changed since you reviewed it/),
    ).toBeInTheDocument()
    await userEvent.click(
      within(panel).getByRole('button', { name: 'Reload the current definition' }))
    await waitFor(() => expect(getRecipeDetail).toHaveBeenCalledTimes(2))
  })

  it('marks history recorded at an earlier revision rather than blending it in', async () => {
    getRecipeReviews.mockResolvedValue(reviewsFixture({
      events: [{ event_id: 'rre-0', recipe_revision_hash: OLD_HASH, decision: 'approved',
        reviewer: 'user:omar', reviewer_role: 'banking_sme', rationale: 'pre-edit approval',
        gold_corpus_refs: [], policy_dependencies: [], supersedes_event_id: null }],
    }))
    render(<RecipeReviewScreen />)
    await userEvent.click(await screen.findByText('Posted debit amount'))
    const panel = screen.getByRole('region', { name: 'Recipe under review' })
    expect(
      await within(panel).findByText(`earlier revision ${OLD_HASH.slice(0, 12)}…`),
    ).toBeInTheDocument()
    // And the checklist still says nobody decided at THIS revision.
    expect(within(panel).getAllByText('no decision yet')).toHaveLength(3)
  })

  it('surfaces a load failure as the server said it, not as an empty queue', async () => {
    getRecipeReviewSummary.mockRejectedValue(new api.ApiError(403, 'missing permission catalog:read'))
    render(<RecipeReviewScreen />)
    expect(await screen.findByText('missing permission catalog:read')).toBeInTheDocument()
    expect(screen.queryByText(/governed recipes/)).not.toBeInTheDocument()
  })
})
