import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { WorkbenchScreen } from './WorkbenchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    recommendFeatures: vi.fn(),
    contractConsideredSet: vi.fn(),
    contractDraft: vi.fn(),
    contractConfirm: vi.fn(),
    refineCandidate: vi.fn(),
    featureRecipe: vi.fn(),
    leakageCheck: vi.fn(),
    registerFeature: vi.fn(),
    featureFreshness: vi.fn(),
  }
})
const recommendFeatures = vi.mocked(api.recommendFeatures)
const contractConsideredSet = vi.mocked(api.contractConsideredSet)
const contractDraft = vi.mocked(api.contractDraft)
const contractConfirm = vi.mocked(api.contractConfirm)
const refineCandidate = vi.mocked(api.refineCandidate)
const featureRecipe = vi.mocked(api.featureRecipe)
const registerFeature = vi.mocked(api.registerFeature)
const featureFreshness = vi.mocked(api.featureFreshness)

beforeEach(() => {
  recommendFeatures.mockReset()
  contractConsideredSet.mockReset()
  contractDraft.mockReset()
  contractConfirm.mockReset()
  refineCandidate.mockReset()
  featureRecipe.mockReset()
  registerFeature.mockReset()
  featureFreshness.mockReset()
})

// derives_pairs deliberately names a catalog ('cards') that differs from the source the tests
// type into the scope row ('deposits'): registration lineage must come from the backend pairs,
// never from the typed source.
const IDEA: api.FeatureIdea = {
  name: 'avg_balance', description: 'average balance per customer',
  derives_from: ['public.accounts.balance'], aggregation: 'avg', grain_table: 'customers',
  derives_pairs: [['cards', 'public.accounts.balance']],
  verification: 'DESIGN-CHECKED', critic_note: '',
  rationale: 'falling balances signal a customer preparing to leave',
}

const IDEA_SPEC: api.FeatureSpecIn = {
  name: 'avg_balance', description: 'average balance per customer',
  grain_table: 'customers', aggregation: 'avg', as_of_column: null,
  derives_from: [{ catalog_source: 'cards', object_ref: 'public.accounts.balance' }],
}

const OTHER_IDEA: api.FeatureIdea = {
  name: 'txn_count', description: 'transactions per customer',
  derives_from: ['public.transactions.id'], aggregation: 'count', grain_table: 'customers',
  derives_pairs: [['cards', 'public.transactions.id']],
  // rationale left blank: the LLM omitted a causal note, so no Why line should render for it.
  verification: 'DESIGN-CHECKED', critic_note: '', rationale: '',
}

const OTHER_IDEA_SPEC: api.FeatureSpecIn = {
  name: 'txn_count', description: 'transactions per customer',
  grain_table: 'customers', aggregation: 'count', as_of_column: null,
  derives_from: [{ catalog_source: 'cards', object_ref: 'public.transactions.id' }],
}

const FRESH: api.FeatureFreshness = { fresh: true, stale_sources: [] }

// IDEA revised under 'use a 30 day window': name, description, and aggregation change; the
// derives pairs stay identical, so the diff must mark that field unchanged.
const REVISED: api.FeatureIdea = {
  name: 'avg_balance_30d', description: '30 day average balance',
  derives_from: ['public.accounts.balance'], aggregation: 'avg_30d', grain_table: 'customers',
  derives_pairs: [['cards', 'public.accounts.balance']],
  verification: 'DESIGN-CHECKED', critic_note: '',
  rationale: 'a shorter window reacts faster',
}

function idea(name: string): api.FeatureIdea {
  return {
    name, description: `${name} per customer`,
    derives_from: ['public.accounts.balance'], aggregation: 'avg', grain_table: 'customers',
    derives_pairs: [['deposits', 'public.accounts.balance']],
    verification: 'DESIGN-CHECKED', critic_note: '', rationale: '',
  }
}

// A one-set response renders the flat list exactly as before the sets model.
function singleSetRound(
  ideas: api.FeatureIdea[],
  rejections: api.Rejection[] = [],
): api.FeatureSetsResult {
  return { sets: [{ lens: 'temporal', features: ideas }], recommendation: null, rejections }
}

const TEMPORAL_ONLY = idea('days_since_last_txn')
const RATIO_ONLY = idea('balance_to_limit_ratio')
// The overlapping feature: present in both sets on purpose (strong signals earn their place in
// several theses); it must render as ONE candidate with an In 2 sets chip.
const SHARED = idea('txn_count_shared')

const CAVEAT =
  'advisory only: a fit/coverage judgment over the metadata, not a performance prediction; '
  + 'confirm the winner with a backtest once features are computed'

function multiSetRound(rejections: api.Rejection[] = []): api.FeatureSetsResult {
  return {
    sets: [
      { lens: 'temporal', features: [TEMPORAL_ONLY, SHARED] },
      { lens: 'ratio', features: [RATIO_ONLY, SHARED] },
    ],
    recommendation: {
      recommended_lens: 'temporal',
      reasoning: 'recency signals move earliest for a churn horizon',
      caveat: CAVEAT,
    },
    rejections,
  }
}

function recipeWith(joinPath: api.JoinStep[]): api.Recipe {
  return {
    intent: 'total spend per customer', grain_table: 'customers',
    derives_from: ['public.transactions.amount'], aggregation: 'sum', as_of_column: null,
    join_path: joinPath,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

interface Scope {
  source?: string
  entity?: string
  target?: string
}

// The governed generate path types a hypothesis and calls contractConsideredSet; this is the
// hypothesis the tests type, asserted back in the call.
const HYPOTHESIS = 'balance draining precedes churn'

// Wrap a recommend-sets-shaped round as the considered-set response the governed paths return:
// the same validated sets as `alternatives`, plus a server-side intent_id. BOTH the initial
// generate AND whole-round feedback now call contractConsideredSet, so a feedback round wraps its
// response through this helper too (the round-shape helpers stay reusable for either path).
function considered(round: api.FeatureSetsResult): api.ConsideredSetResp {
  return {
    intent_id: 'int_1', anchor: null, alternatives: round.sets,
    recommendation: round.recommendation, rejections: round.rejections,
  }
}

async function renderAndGenerate(
  ideas: api.FeatureIdea[],
  scope: Scope = {},
  rejections: api.Rejection[] = [],
) {
  contractConsideredSet.mockResolvedValue(considered(singleSetRound(ideas, rejections)))
  render(<WorkbenchScreen />)
  if (scope.source) {
    await userEvent.type(screen.getByLabelText('Catalog source'), scope.source)
  }
  if (scope.entity) {
    await userEvent.type(screen.getByLabelText('Entity'), scope.entity)
  }
  if (scope.target) {
    await userEvent.type(screen.getByLabelText('Target column'), scope.target)
  }
  await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
  await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
}

async function renderAndGenerateSets(round: api.FeatureSetsResult) {
  contractConsideredSet.mockResolvedValue(considered(round))
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
  await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
}

async function selectCandidate(name: string) {
  await userEvent.click(await screen.findByRole('checkbox', { name: `Select ${name}` }))
}

async function registerSelection(count: number) {
  const plural = count === 1 ? 'feature' : 'features'
  await userEvent.click(
    screen.getByRole('button', { name: `Approve and register ${count} ${plural}` }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
}

async function openDescribe() {
  await userEvent.click(screen.getByRole('button', { name: /write definitions myself/i }))
}

async function draftFeature(description: string) {
  await userEvent.type(screen.getByLabelText('Describe the feature you want'), description)
  await userEvent.click(screen.getByRole('button', { name: 'Draft candidate' }))
}

async function renderAndDraft(joinPath: api.JoinStep[] = []) {
  featureRecipe.mockResolvedValue(recipeWith(joinPath))
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
  await openDescribe()
  await draftFeature('total spend per customer')
  expect(await screen.findByText('Draft')).toBeInTheDocument()
}

function gateState(title: string): string | null | undefined {
  const strip = screen.getByRole('list', { name: 'Where you are in the loop' })
  return within(strip).getByText(title).closest('[data-state]')?.getAttribute('data-state')
}

describe('gates strip', () => {
  it('advances only with real state, from goal to approval', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([IDEA])))
    render(<WorkbenchScreen />)
    // No goal yet: stating it is the current step, everything downstream is upcoming.
    expect(gateState('State the goal')).toBe('active')
    expect(gateState('Propose in sets')).toBe('todo')
    expect(gateState('Compare, mix, give feedback')).toBe('todo')
    expect(gateState('You approve')).toBe('todo')
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    // Goal alone is not the whole brief: the gate stays active until the hypothesis is given too
    // (else it would falsely promise the next step while Generate silently no-ops — bug_004).
    expect(gateState('State the goal')).toBe('active')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    expect(gateState('State the goal')).toBe('done')
    expect(gateState('Propose in sets')).toBe('active')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(gateState('Propose in sets')).toBe('done')
    expect(gateState('Compare, mix, give feedback')).toBe('active')
    expect(gateState('You approve')).toBe('todo')
    await selectCandidate('avg_balance')
    expect(gateState('Compare, mix, give feedback')).toBe('done')
    expect(gateState('You approve')).toBe('active')
    await registerSelection(1)
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(gateState('You approve')).toBe('done')
  })

  it('names the actor on every gate and keeps the mockup copy', () => {
    render(<WorkbenchScreen />)
    const strip = screen.getByRole('list', { name: 'Where you are in the loop' })
    expect(within(strip).getAllByText('You')).toHaveLength(3)
    expect(within(strip).getByText('Engine')).toBeInTheDocument()
    expect(within(strip).getByText('Nothing generates without your intent.')).toBeInTheDocument()
    expect(
      within(strip).getByText('One set per strategy lens, all safety-checked.'),
    ).toBeInTheDocument()
    expect(
      within(strip).getByText('Take a set or pick a la carte across sets.'),
    ).toBeInTheDocument()
    expect(
      within(strip).getByText('Nothing registers without your click, under your name.'),
    ).toBeInTheDocument()
  })
})

describe('generation', () => {
  it('passes the hypothesis, goal, and every scope field through to the considered-set call', async () => {
    await renderAndGenerate([], {
      source: 'deposits', entity: 'customer', target: 'public.labels.churned',
    })
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn', {
      catalogSource: 'deposits', entity: 'customer', targetRef: 'public.labels.churned',
    })
  })

  it('leaves blank scope fields undefined in the considered-set call', async () => {
    await renderAndGenerate([])
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn', {
      catalogSource: undefined, entity: undefined, targetRef: undefined,
    })
  })

  it('shows the empty note only after a generation round returns nothing', async () => {
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([])))
    render(<WorkbenchScreen />)
    expect(screen.queryByText(/no grounded candidates/i)).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
  })

  it('applies only the latest generation round when responses arrive out of order', async () => {
    const first = deferred<api.ConsideredSetResp>()
    const second = deferred<api.ConsideredSetResp>()
    contractConsideredSet
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const { container } = render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    // Round 1 is in flight: the path card swaps to Generating and disables (no casual re-submit).
    expect(screen.getByRole('button', { name: /generating/i })).toBeDisabled()
    // The disabled card blocks the button, so a second round can only arrive as a re-submit
    // (StrictMode remount, programmatic) — exactly the race the sequence guard defends against.
    const form = container.querySelector('form')
    if (!form) throw new Error('generation form not found')
    await act(async () => {
      fireEvent.submit(form)
    })
    await act(async () => {
      second.resolve(considered(singleSetRound([OTHER_IDEA])))
    })
    expect(await screen.findByText('txn_count')).toBeInTheDocument()
    // The stale first response resolves late and must not overwrite the newer round.
    await act(async () => {
      first.resolve(considered(singleSetRound([IDEA])))
    })
    expect(screen.getByText('txn_count')).toBeInTheDocument()
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
  })

  it('shows the honest 503 notice and never falls back to the plain recommend endpoint', async () => {
    // 503 means no LLM provider on the deployment: /features/recommend would fail identically,
    // so a silent fallback would only fake capability.
    contractConsideredSet.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
    expect(recommendFeatures).not.toHaveBeenCalled()
  })

  it('the example chip fills the goal, but Generate stays disabled until a hypothesis is given', async () => {
    render(<WorkbenchScreen />)
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'predict churn' }))
    expect(screen.getByLabelText('Prediction goal')).toHaveValue('predict churn')
    // Goal alone must NOT enable Generate: generate() also requires a hypothesis, so an enabled
    // button here would be a silent no-op on click (bug_004). It enables only once both are present.
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Hypothesis'), 'balance drains then they leave')
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeEnabled()
    expect(contractConsideredSet).not.toHaveBeenCalled()
  })
})

describe('multiple sets', () => {
  it('renders one summary card per set with the advisory pick and its caveat', async () => {
    await renderAndGenerateSets(multiSetRound())
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    expect(screen.getByText('Ratio set')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Proposed feature sets' })).toBeInTheDocument()
    // Exactly one Recommended chip, on the advisory pick.
    expect(screen.getAllByText('Recommended')).toHaveLength(1)
    // Both cards carry the honest all-design-checked meta line.
    expect(screen.getAllByText(/2 features · all design-checked/)).toHaveLength(2)
    // Advisory panel: the pick, the reasoning, and the backend caveat verbatim.
    expect(screen.getByText(/Engine's pick: Temporal\./)).toBeInTheDocument()
    expect(screen.getByText(/recency signals move earliest for a churn horizon/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(CAVEAT.slice(0, 40)))).toBeInTheDocument()
  })

  it('opens on the recommended set and switches the detail list per card', async () => {
    await renderAndGenerateSets(multiSetRound())
    expect(await screen.findByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.getByText('txn_count_shared')).toBeInTheDocument()
    expect(screen.queryByText('balance_to_limit_ratio')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /temporal set/i }))
      .toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    expect(screen.getByText('balance_to_limit_ratio')).toBeInTheDocument()
    expect(screen.getByText('txn_count_shared')).toBeInTheDocument()
    expect(screen.queryByText('days_since_last_txn')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ratio set/i }))
      .toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /temporal set/i }))
      .toHaveAttribute('aria-pressed', 'false')
  })

  it('take this set selects every unregistered feature in it', async () => {
    await renderAndGenerateSets(multiSetRound())
    await screen.findByText('days_since_last_txn')
    await userEvent.click(screen.getByRole('button', { name: 'Take this set (Temporal)' }))
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    expect(screen.getByText('from the Temporal set')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select days_since_last_txn' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Select txn_count_shared' })).toBeChecked()
    // The card meta reflects the tray.
    expect(screen.getByText(/2 in your tray/)).toBeInTheDocument()
  })

  it('mixes picks across sets: selection survives switching and the tray names the mix', async () => {
    await renderAndGenerateSets(multiSetRound())
    await selectCandidate('days_since_last_txn')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByText('from the Temporal set')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    // The temporal pick is kept while another set is showing.
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    await selectCandidate('balance_to_limit_ratio')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    expect(
      screen.getByText(
        'mixed from 2 sets · each feature was safety-checked at generation; your approval '
        + 'registers them individually',
      ),
    ).toBeInTheDocument()
    // Switching back leaves both picks intact.
    await userEvent.click(screen.getByRole('button', { name: /temporal set/i }))
    expect(screen.getByRole('checkbox', { name: 'Select days_since_last_txn' })).toBeChecked()
    expect(screen.getByText('2 selected')).toBeInTheDocument()
  })

  it('renders an overlapping feature as one candidate with the In N sets chip', async () => {
    await renderAndGenerateSets(multiSetRound())
    await screen.findByText('txn_count_shared')
    // One chip in the temporal view; the set-only features carry none.
    expect(screen.getAllByText('In 2 sets')).toHaveLength(1)
    await selectCandidate('txn_count_shared')
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    // Same candidate in the other view: still selected, still one selection.
    expect(screen.getByRole('checkbox', { name: 'Select txn_count_shared' })).toBeChecked()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
  })

  it('registers an overlapping feature once and flips its row in every set view', async () => {
    registerFeature.mockResolvedValue('feat_20')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerateSets(multiSetRound())
    await selectCandidate('txn_count_shared')
    await registerSelection(1)
    expect(await screen.findByText('feat_20')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    expect(screen.getByText('feat_20')).toBeInTheDocument()
    expect(
      screen.queryByRole('checkbox', { name: 'Select txn_count_shared' }),
    ).not.toBeInTheDocument()
  })

  it('registers a cross-set mix as one batch, whichever view is showing', async () => {
    registerFeature.mockResolvedValueOnce('feat_21').mockResolvedValueOnce('feat_22')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerateSets(multiSetRound())
    await selectCandidate('days_since_last_txn')
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    await selectCandidate('balance_to_limit_ratio')
    await registerSelection(2)
    // The ratio view shows its own registration; the temporal pick registered off-view.
    expect(await screen.findByText('feat_22')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
    await userEvent.click(screen.getByRole('button', { name: /temporal set/i }))
    expect(screen.getByText('feat_21')).toBeInTheDocument()
  })

  it('drops empty sets from the compare row', async () => {
    await renderAndGenerateSets({
      sets: [
        { lens: 'unary', features: [] },
        { lens: 'temporal', features: [TEMPORAL_ONLY] },
        { lens: 'ratio', features: [RATIO_ONLY] },
      ],
      recommendation: {
        recommended_lens: 'temporal',
        reasoning: 'recency signals move earliest for a churn horizon',
        caveat: CAVEAT,
      },
      rejections: [],
    })
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    expect(screen.getByText('Ratio set')).toBeInTheDocument()
    expect(screen.queryByText('Unary set')).not.toBeInTheDocument()
  })

  it('renders a single-set response as the flat list with no compare row', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Proposed features' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /take this set/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/lens ·/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/engine's pick/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/in your tray/i)).not.toBeInTheDocument()
  })

  it('shows the empty note and the rejections when every set comes back empty', async () => {
    contractConsideredSet.mockResolvedValue(considered({
      sets: [{ lens: 'unary', features: [] }],
      recommendation: null,
      rejections: [
        { name: 'nps_score_avg', reason: 'no such column exists in any catalog', code: 'UNGROUNDED' },
      ],
    }))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
    expect(screen.getByText('1 rejected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show' }))
    expect(screen.getByText('nps_score_avg')).toBeInTheDocument()
  })
})

describe('rejections panel', () => {
  const REJECTIONS: api.Rejection[] = [
    { name: 'days_to_churn', reason: 'derives from the target column public.labels.churned', code: 'LEAKAGE' },
    { name: 'next_month_balance', reason: 'uses information from after the prediction time', code: 'LEAKAGE' },
    { name: 'card_spend_total', reason: 'source cards has no fresh upload inside 24 hours', code: 'STALE' },
    { name: 'nps_score_avg', reason: 'no such column exists in any catalog', code: 'UNGROUNDED' },
  ]

  it('summarizes the round with per-code tallies and reveals the rows on Show', async () => {
    await renderAndGenerate([IDEA], {}, REJECTIONS)
    expect(await screen.findByText('4 rejected')).toBeInTheDocument()
    expect(screen.getByText(
      'The safety gauntlet rejected 4 candidates across all lenses: '
      + 'leakage 2 · stale source 1 · ungrounded 1.',
    )).toBeInTheDocument()
    // Rows stay hidden until asked for.
    expect(screen.queryByText('days_to_churn')).not.toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: 'Show' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', 'wb-rej-list')
    await userEvent.click(toggle)
    expect(screen.getByText('days_to_churn')).toBeInTheDocument()
    expect(screen.getByText('days_to_churn').closest('ul'))
      .toHaveAttribute('id', 'wb-rej-list')
    expect(
      screen.getByText('derives from the target column public.labels.churned'),
    ).toBeInTheDocument()
    // Per-row code chips, in words.
    expect(screen.getAllByText('leakage')).toHaveLength(2)
    expect(screen.getByText('stale source')).toBeInTheDocument()
    expect(screen.getByText('ungrounded')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('words an unfamiliar rejection code instead of showing the enum token', async () => {
    await renderAndGenerate([IDEA], {}, [
      { name: 'avg_balance_2', reason: 'no revision was produced', code: 'NO_REVISION' },
    ])
    await userEvent.click(await screen.findByRole('button', { name: 'Show' }))
    expect(screen.getByText('no revision')).toBeInTheDocument()
    expect(screen.queryByText('NO_REVISION')).not.toBeInTheDocument()
  })

  it('omits the panel when the gauntlet rejected nothing', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText(/safety gauntlet/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show' })).not.toBeInTheDocument()
  })
})

describe('selection and registration', () => {
  it('registers a selected candidate only after the explicit approval confirm, with lineage from the backend pairs', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA], { source: 'deposits' })
    // Lineage display comes from derives_pairs ('cards'), not the typed source ('deposits').
    expect(await screen.findByText('cards:public.accounts.balance')).toBeInTheDocument()
    await selectCandidate('avg_balance')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.getByText(
      'Your approval writes these features into the registry with their lineage, under your '
      + 'name.',
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    expect(registerFeature).toHaveBeenCalledWith(IDEA_SPEC)
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    expect(screen.getByText('feat_01')).toBeInTheDocument()
    expect(featureFreshness).toHaveBeenCalledWith('feat_01')
    expect(screen.getByText('fresh')).toBeInTheDocument()
    // The registered row swaps its checkbox for the ok state.
    expect(screen.queryByRole('checkbox', { name: 'Select avg_balance' })).not.toBeInTheDocument()
  })

  it('sends exactly one register request when confirm is double-clicked in flight', async () => {
    const pending = deferred<string>()
    registerFeature.mockImplementation(() => pending.promise)
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    const confirm = screen.getByRole('button', { name: 'Confirm approval' })
    await userEvent.click(confirm)
    await userEvent.click(confirm)
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(confirm).toBeDisabled()
    await act(async () => {
      pending.resolve('feat_01')
    })
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
  })

  it('registers a batch of two sequentially, in candidate order, one request each', async () => {
    registerFeature.mockResolvedValueOnce('feat_01').mockResolvedValueOnce('feat_02')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 2 features' }))
    expect(screen.getByText(
      'Your approval writes these features into the registry with their lineage, under your '
      + 'name.',
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    expect(await screen.findByText('feat_02')).toBeInTheDocument()
    expect(screen.getByText('feat_01')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
    expect(registerFeature).toHaveBeenNthCalledWith(1, IDEA_SPEC)
    expect(registerFeature).toHaveBeenNthCalledWith(2, OTHER_IDEA_SPEC)
    expect(screen.getAllByText('fresh')).toHaveLength(2)
  })

  it('continues the batch past a failure and keeps the failed candidate selected for retry', async () => {
    registerFeature
      .mockRejectedValueOnce(new api.ApiError(409, 'feature name already registered'))
      .mockResolvedValueOnce('feat_02')
      .mockResolvedValueOnce('feat_03')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    await registerSelection(2)
    // First candidate failed inline; the second still registered.
    expect(await screen.findByText('feature name already registered')).toBeInTheDocument()
    expect(screen.getByText('feat_02')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
    // The failed candidate stays selected, ready to retry.
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeChecked()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    await registerSelection(1)
    expect(await screen.findByText('feat_03')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(3)
    expect(registerFeature).toHaveBeenNthCalledWith(3, IDEA_SPEC)
    expect(screen.queryByText('feature name already registered')).not.toBeInTheDocument()
  })

  it('cancel backs out of the confirm step without registering', async () => {
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(
      screen.getByRole('button', { name: 'Approve and register 1 feature' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeChecked()
  })

  it('does not resurrect registered state when a regeneration reuses a name', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    // Second round returns a candidate with the same LLM-chosen name: it was never registered.
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    const checkbox = await screen.findByRole('checkbox', { name: 'Select avg_balance' })
    expect(checkbox).not.toBeChecked()
    expect(screen.queryByText(/registered/i)).not.toBeInTheDocument()
  })

  it('marks a stale registration with its stale sources', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue({ fresh: false, stale_sources: ['cards'] })
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText('stale: cards')).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
  })

  it('locks the generate path and scope fields while a batch is confirming or in flight', async () => {
    const pending = deferred<string>()
    registerFeature.mockImplementation(() => pending.promise)
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    // Confirm step: no new round and no scope edit can pull rows out from under the approval.
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeDisabled()
    expect(screen.getByLabelText('Catalog source')).toBeDisabled()
    expect(screen.getByLabelText('Entity')).toBeDisabled()
    expect(screen.getByLabelText('Target column')).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    // Still locked while the batch is in flight.
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeDisabled()
    expect(screen.getByLabelText('Catalog source')).toBeDisabled()
    await act(async () => {
      pending.resolve('feat_60')
    })
    expect(await screen.findByText('feat_60')).toBeInTheDocument()
    // The lock releases with the batch.
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeEnabled()
    expect(screen.getByLabelText('Catalog source')).toBeEnabled()
  })

  it('omits the freshness chip silently when the freshness call fails', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockRejectedValue(new api.ApiError(500, 'freshness unavailable'))
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText(/registered/i)).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
    expect(screen.queryByText(/stale:/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('approval vocabulary', () => {
  it('opens the candidate section with the approval sentence', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText(
      'Nothing below enters the catalog without your approval.',
    )).toBeInTheDocument()
  })

  it('keeps the approval sentence on a drafts-only list', async () => {
    await renderAndDraft()
    expect(
      screen.getByText('Nothing below enters the catalog without your approval.'),
    ).toBeInTheDocument()
  })

  it('give feedback toggles the inline box with the mockup copy', async () => {
    await renderAndGenerate([IDEA])
    const button = await screen.findByRole('button', { name: 'Give feedback' })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByLabelText('What should change')).not.toBeInTheDocument()
    await userEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByLabelText('What should change')).toBeInTheDocument()
    expect(screen.getByText(
      'Your feedback runs the engine once, re-checks safety, and is recorded under your name. '
      + '3 rounds per candidate, then it is back in your hands.',
    )).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Send feedback for one revision · round 1 of 3' }),
    ).toBeInTheDocument()
    await userEvent.click(button)
    expect(screen.queryByLabelText('What should change')).not.toBeInTheDocument()
    expect(refineCandidate).not.toHaveBeenCalled()
  })
})

describe('scope changes', () => {
  it('editing the goal keeps candidates', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Prediction goal'), ' next quarter')
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText(/scope changed/i)).not.toBeInTheDocument()
  })

  it('editing the catalog source clears generated candidates and drafts', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    // Drafts were snapshotted against the previous source: a source edit clears everything.
    await userEvent.type(screen.getByLabelText('Catalog source'), 'x')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.queryByText('total_spend_per_customer')).not.toBeInTheDocument()
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Scope changed. Regenerate to refresh candidates.')
  })

  it('editing the entity clears generated candidates but keeps drafts', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.getByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('editing the target clears generated candidates and the screening note', async () => {
    await renderAndGenerate([IDEA], { target: 'public.labels.churned' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText(/leaky candidates were rejected/i)).toBeInTheDocument()
    // Candidates were screened against the previous target: any edit voids them.
    await userEvent.type(screen.getByLabelText('Target column'), '2')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.queryByText(/leaky candidates were rejected/i)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('clears the selection when the scope changes', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByText('2 selected')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Approve and register 2 features' }),
    ).not.toBeInTheDocument()
  })

  it('editing the entity clears the sets row and the rejections panel too', async () => {
    await renderAndGenerateSets(multiSetRound([
      { name: 'days_to_churn', reason: 'derives from the target column', code: 'LEAKAGE' },
    ]))
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    expect(screen.getByText('1 rejected')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByText('Temporal set')).not.toBeInTheDocument()
    expect(screen.queryByText('1 rejected')).not.toBeInTheDocument()
    expect(screen.queryByText(/engine's pick/i)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })
})

describe('described drafts', () => {
  it('the write-definitions path toggles the composer and reflects aria-pressed', async () => {
    render(<WorkbenchScreen />)
    const card = screen.getByRole('button', { name: /write definitions myself/i })
    expect(card).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByLabelText('Describe the feature you want')).not.toBeInTheDocument()
    await userEvent.click(card)
    expect(card).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Describe the feature you want')).toBeInTheDocument()
    await userEvent.click(card)
    expect(card).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByLabelText('Describe the feature you want')).not.toBeInTheDocument()
  })

  it('drafts a candidate and registers it with the snapshot-source pairs', async () => {
    registerFeature.mockResolvedValue('feat_09')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndDraft([
      { from_ref: 'public.transactions.account_id', to_ref: 'public.accounts.id', cardinality: 'N:1' },
    ])
    expect(featureRecipe).toHaveBeenCalledWith('total spend per customer', 'deposits')
    // The suggested name is a slug of the description, editable before selection.
    expect(screen.getByLabelText('Name')).toHaveValue('total_spend_per_customer')
    // Lineage display uses the drafted-against snapshot, not live context.
    expect(screen.getByText('deposits:public.transactions.amount')).toBeInTheDocument()
    await selectCandidate('total_spend_per_customer')
    await registerSelection(1)
    expect(registerFeature).toHaveBeenCalledWith({
      name: 'total_spend_per_customer', description: 'total spend per customer',
      grain_table: 'customers', aggregation: 'sum', as_of_column: null,
      derives_from: [{ catalog_source: 'deposits', object_ref: 'public.transactions.amount' }],
    })
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('feat_09')).toBeInTheDocument()
    expect(screen.getByText('fresh')).toBeInTheDocument()
  })

  it('requires a name before a draft can be selected, and registers under the edited name', async () => {
    registerFeature.mockResolvedValue('feat_10')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndDraft()
    await selectCandidate('total_spend_per_customer')
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    // Blanking the name deselects the draft and blocks selection until it is named again.
    await userEvent.clear(screen.getByLabelText('Name'))
    expect(screen.queryByText('1 selected')).not.toBeInTheDocument()
    const checkbox = screen.getByRole('checkbox', { name: 'Select unnamed draft' })
    expect(checkbox).toBeDisabled()
    expect(checkbox).not.toBeChecked()
    expect(screen.getByText('Name this draft to select it for registration.')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Name'), 'spend_90d')
    await selectCandidate('spend_90d')
    await registerSelection(1)
    expect(registerFeature).toHaveBeenCalledWith(expect.objectContaining({ name: 'spend_90d' }))
    expect(await screen.findByText('feat_10')).toBeInTheDocument()
  })

  it('disables drafting until a catalog source is set', async () => {
    render(<WorkbenchScreen />)
    await openDescribe()
    await userEvent.type(
      screen.getByLabelText('Describe the feature you want'), 'total spend per customer')
    expect(screen.getByRole('button', { name: 'Draft candidate' })).toBeDisabled()
    expect(screen.getByText(/set catalog source above/i)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    expect(screen.getByRole('button', { name: 'Draft candidate' })).toBeEnabled()
    expect(featureRecipe).not.toHaveBeenCalled()
  })

  it('accumulates drafts so several described features register together', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    registerFeature.mockResolvedValueOnce('feat_11').mockResolvedValueOnce('feat_12')
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await draftFeature('total spend per customer')
    await draftFeature('active days per customer')
    expect(await screen.findByText('active_days_per_customer')).toBeInTheDocument()
    expect(screen.getAllByText('Draft')).toHaveLength(2)
    await selectCandidate('total_spend_per_customer')
    await selectCandidate('active_days_per_customer')
    await registerSelection(2)
    expect(await screen.findByText('feat_11')).toBeInTheDocument()
    expect(screen.getByText('feat_12')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(2)
  })

  it('gates the describe path behind the same missing-provider notice', async () => {
    featureRecipe.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await draftFeature('total spend per customer')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
    expect(screen.queryByText('Draft')).not.toBeInTheDocument()
  })

  it('renders the draft join path with a fan-out warning', async () => {
    await renderAndDraft([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:N' },
      { from_ref: 'public.accounts.id', to_ref: 'public.transactions.account_id', cardinality: '1:N' },
    ])
    expect(screen.getAllByText('(1:N)')).toHaveLength(2)
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('flags a lowercase 1:n hop as fan-out', async () => {
    await renderAndDraft([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:n' },
    ])
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('names unknown cardinality instead of rendering it as calm', async () => {
    await renderAndDraft([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
      { from_ref: 'public.customers.cust_id', to_ref: 'public.segments.cust_id', cardinality: null },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.getByText(/cardinality unknown/)).toBeInTheDocument()
    expect(screen.getByText(/cannot be ruled out/i)).toBeInTheDocument()
  })

  it('stays calm on an all-N:1 join path', async () => {
    await renderAndDraft([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/cannot be ruled out/i)).not.toBeInTheDocument()
  })
})

describe('batch describe composer', () => {
  const THREE_LINES =
    'total spend per customer{Enter}days since last transaction{Enter}active accounts per customer'

  async function typeDescribe(text: string) {
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await userEvent.type(screen.getByLabelText('Describe the feature you want'), text)
  }

  it('drafts one candidate per line, in line order, against the snapshot source', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await typeDescribe(THREE_LINES)
    // The live label counts the non-empty lines before submit.
    expect(screen.getByRole('button', { name: 'Draft 3 candidates' })).toBeEnabled()
    await userEvent.click(screen.getByRole('button', { name: 'Draft 3 candidates' }))
    expect(await screen.findByText('active_accounts_per_customer')).toBeInTheDocument()
    expect(featureRecipe).toHaveBeenCalledTimes(3)
    expect(featureRecipe).toHaveBeenNthCalledWith(1, 'total spend per customer', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(2, 'days since last transaction', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(3, 'active accounts per customer', 'deposits')
    expect(screen.getAllByText('Draft')).toHaveLength(3)
    // Candidates append in line order. The candidate list is the second list on the page
    // (the gates strip is the first).
    const list = screen.getAllByRole('list')
      .map(el => el.textContent ?? '')
      .find(text => text.includes('total_spend_per_customer')) ?? ''
    expect(list.indexOf('total_spend_per_customer'))
      .toBeLessThan(list.indexOf('days_since_last_transaction'))
    expect(list.indexOf('days_since_last_transaction'))
      .toBeLessThan(list.indexOf('active_accounts_per_customer'))
    // A clean batch clears the textarea fully; the composer stays open.
    expect(screen.getByLabelText('Describe the feature you want')).toHaveValue('')
  })

  it('isolates a failed line: the rest still draft and only the failed line stays to retry', async () => {
    featureRecipe
      .mockResolvedValueOnce(recipeWith([]))
      .mockRejectedValueOnce(new api.ApiError(422, 'no column matches that description'))
      .mockResolvedValueOnce(recipeWith([]))
    await typeDescribe(THREE_LINES)
    await userEvent.click(screen.getByRole('button', { name: 'Draft 3 candidates' }))
    expect(await screen.findByText('active_accounts_per_customer')).toBeInTheDocument()
    expect(screen.getByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.queryByText('days_since_last_transaction')).not.toBeInTheDocument()
    expect(screen.getAllByText('Draft')).toHaveLength(2)
    // The rejected line is called out inline and left in the textarea for a retry.
    expect(screen.getByText('Line 2: no column matches that description')).toBeInTheDocument()
    expect(screen.getByLabelText('Describe the feature you want'))
      .toHaveValue('days since last transaction')
  })

  it('drafts each line once when the submit is double-clicked in flight', async () => {
    const first = deferred<api.Recipe>()
    const second = deferred<api.Recipe>()
    featureRecipe
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    await typeDescribe('total spend per customer{Enter}days since last transaction')
    const button = screen.getByRole('button', { name: 'Draft 2 candidates' })
    await userEvent.click(button)
    // Line 1's recipe is pending; the button is disabled and a second submit is a no-op.
    expect(button).toBeDisabled()
    expect(button).toHaveTextContent('Drafting')
    await userEvent.click(button)
    expect(featureRecipe).toHaveBeenCalledTimes(1)
    await act(async () => {
      first.resolve(recipeWith([]))
    })
    await act(async () => {
      second.resolve(recipeWith([]))
    })
    expect(await screen.findByText('days_since_last_transaction')).toBeInTheDocument()
    // Exactly one call per line: the in-flight double-submit never started a second batch.
    expect(featureRecipe).toHaveBeenCalledTimes(2)
    expect(featureRecipe).toHaveBeenNthCalledWith(1, 'total spend per customer', 'deposits')
    expect(featureRecipe).toHaveBeenNthCalledWith(2, 'days since last transaction', 'deposits')
  })
})

describe('verification stamp and rationale', () => {
  it('renders the causal rationale when present and omits it when the LLM left it blank', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(
      await screen.findByText(/falling balances signal a customer preparing to leave/i),
    ).toBeInTheDocument()
    // OTHER_IDEA carries an empty rationale, so exactly one Why line renders across the list.
    expect(screen.getAllByText(/^Why:/)).toHaveLength(1)
  })

  it('stamps generated candidates design-checked and shows the honest help line once', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    // One soft stamp per generated candidate, from the backend verification field (lowercased).
    expect(screen.getAllByText('design-checked')).toHaveLength(2)
    // The explanation is one help line for the whole list, not repeated per row.
    expect(screen.getAllByText(/structurally safe against leakage/i)).toHaveLength(1)
  })

  it('never stamps drafts and hides the help line on a drafts-only list', async () => {
    // renderAndDraft never generates, so no candidate passed the gauntlet.
    await renderAndDraft()
    expect(screen.getByText('Draft')).toBeInTheDocument()
    expect(screen.queryByText('design-checked')).not.toBeInTheDocument()
    expect(screen.queryByText(/structurally safe against leakage/i)).not.toBeInTheDocument()
  })

  it('leaves a described draft as DRAFT only alongside a stamped generated candidate', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    // The generated candidate keeps its lone stamp; the draft carries none.
    expect(screen.getAllByText('design-checked')).toHaveLength(1)
    expect(screen.getByText('Draft')).toBeInTheDocument()
  })
})

describe('whole-round feedback', () => {
  async function submitSetFeedback(instruction: string, round = 1) {
    await userEvent.type(screen.getByLabelText('Feedback on the whole round'), instruction)
    await userEvent.click(screen.getByRole('button', {
      name: `Regenerate with feedback · round ${round} of 3`,
    }))
  }

  it('regenerates with the feedback and the original goal, pinning the selection', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA], {
      source: 'deposits', entity: 'customer', target: 'public.labels.churned',
    })
    await selectCandidate('avg_balance')
    // The goal input is edited after the round: feedback still reruns the ROUND's objective.
    await userEvent.type(screen.getByLabelText('Prediction goal'), ' fast')
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([idea('inactivity_days')])))
    await submitSetFeedback('more behavioral signals')
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    // Feedback routes through considered-set with the ROUND's snapshotted hypothesis + objective
    // (the goal input now reads 'predict churn fast') plus the instruction as `feedback`.
    expect(contractConsideredSet).toHaveBeenLastCalledWith(HYPOTHESIS, 'predict churn', {
      catalogSource: 'deposits', entity: 'customer', targetRef: 'public.labels.churned',
      feedback: 'more behavioral signals',
    })
    // The selected candidate is pinned: kept, still selected. The unselected one is replaced.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('Kept')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeChecked()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.queryByText('txn_count')).not.toBeInTheDocument()
    // The action is recorded, attributed, and countable.
    expect(screen.getByText(
      'Set feedback round 1 of 3 · recorded · from user:dev · "more behavioral signals" · '
      + 'kept 1 selected, replaced 1',
    )).toBeInTheDocument()
    // The counter advanced and the input cleared for the next instruction.
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 2 of 3',
    })).toBeInTheDocument()
    expect(screen.getByLabelText('Feedback on the whole round')).toHaveValue('')
  })

  it('keeps registered rows through a round and counts only selected pins as kept', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([idea('inactivity_days')])))
    await submitSetFeedback('fewer balance aggregates')
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    // The registered row survives untouched (its Registered state is its mark, no Kept chip);
    // the unselected candidate was replaced; nothing re-registers.
    expect(screen.getByText('feat_01')).toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText('Kept')).not.toBeInTheDocument()
    expect(screen.queryByText('txn_count')).not.toBeInTheDocument()
    expect(screen.getByText(/kept 0 selected, replaced 1/)).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
  })

  it('keeps pinned candidates visible across set views after a multi-set round', async () => {
    await renderAndGenerateSets(multiSetRound())
    await selectCandidate('days_since_last_txn')
    contractConsideredSet.mockResolvedValueOnce(considered(multiSetRound()))
    await submitSetFeedback('sharper recency signals')
    expect(await screen.findByText('Kept')).toBeInTheDocument()
    // Previous round held 3 candidates; the pin stayed, 2 were replaced.
    expect(screen.getByText(/kept 1 selected, replaced 2/)).toBeInTheDocument()
    // The kept row shows in the temporal view and after switching to the ratio view.
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select days_since_last_txn' })).toBeChecked()
  })

  it('disables the channel after three rounds with the exhausted note', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    for (let round = 1; round <= 3; round++) {
      contractConsideredSet.mockResolvedValueOnce(
        considered(singleSetRound([idea(`signal_${round}`)])))
      await submitSetFeedback(`round ${round} note`, round)
      expect(await screen.findByText(`signal_${round}`)).toBeInTheDocument()
    }
    expect(screen.getByLabelText('Feedback on the whole round')).toBeDisabled()
    expect(screen.getByRole('button', { name: /regenerate with feedback/i })).toBeDisabled()
    expect(screen.getByText(
      'Rounds exhausted. Approve, edit by hand, or restate the goal.',
    )).toBeInTheDocument()
    // All three rounds stay on the record.
    expect(screen.getAllByText(/Set feedback round \d of 3 · recorded/)).toHaveLength(3)
    // The initial generate plus 3 feedback rounds all run through considered-set: 4 calls.
    expect(contractConsideredSet).toHaveBeenCalledTimes(4)
  })

  it('sends exactly one regenerate when the form is double-submitted in flight', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    await submitSetFeedback('one note')
    expect(screen.getByRole('button', { name: 'Regenerating…' })).toBeDisabled()
    const form = screen.getByLabelText('Feedback on the whole round').closest('form')
    if (!form) throw new Error('feedback form not found')
    await act(async () => {
      fireEvent.submit(form)
    })
    // 1 generate + 1 feedback flight = 2 considered-set calls; the in-flight double-submit
    // added nothing.
    expect(contractConsideredSet).toHaveBeenCalledTimes(2)
    await act(async () => {
      pending.resolve(considered(singleSetRound([idea('inactivity_days')])))
    })
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    expect(contractConsideredSet).toHaveBeenCalledTimes(2)
  })

  it('a stale feedback response never overwrites a newer generation round', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    await submitSetFeedback('one note')
    // A fresh engine round outranks the in-flight feedback round. Both run through considered-set;
    // the fresh generate's response is queued next and the stale feedback resolves afterward.
    contractConsideredSet.mockResolvedValueOnce(considered(singleSetRound([OTHER_IDEA])))
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('txn_count')).toBeInTheDocument()
    await act(async () => {
      pending.resolve(considered(singleSetRound([idea('stale_signal')])))
    })
    expect(screen.queryByText('stale_signal')).not.toBeInTheDocument()
    expect(screen.getByText('txn_count')).toBeInTheDocument()
    // The fresh round starts a fresh allowance with no record of the discarded round.
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    })).toBeInTheDocument()
    expect(screen.queryByText(/Set feedback round/)).not.toBeInTheDocument()
  })

  it('discards a feedback round that resolves after a scope edit', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    await submitSetFeedback('one note')
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    await act(async () => {
      pending.resolve(considered(singleSetRound([idea('stale_signal')])))
    })
    // The response was for the previous scope: nothing applies, nothing is recorded.
    expect(screen.queryByText('stale_signal')).not.toBeInTheDocument()
    expect(screen.queryByText(/Set feedback round/)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('a scope edit resets the round counter with everything else', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    contractConsideredSet.mockResolvedValueOnce(considered(singleSetRound([idea('signal_1')])))
    await submitSetFeedback('one note')
    expect(await screen.findByText('signal_1')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 2 of 3',
    })).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Entity'), 'c')
    expect(screen.queryByLabelText('Feedback on the whole round')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    })).toBeInTheDocument()
    expect(screen.queryByText(/Set feedback round \d of 3 · recorded/)).not.toBeInTheDocument()
  })

  it('surfaces the missing-provider notice and consumes no round on failure', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    contractConsideredSet.mockRejectedValueOnce(new api.ApiError(503, 'not configured'))
    await submitSetFeedback('one note')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
    // The round never ran: candidates stay, the counter holds, nothing is recorded.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    })).toBeInTheDocument()
    expect(screen.queryByText(/Set feedback round/)).not.toBeInTheDocument()
  })

  it('offers no whole-round feedback on a drafts-only list', async () => {
    await renderAndDraft()
    expect(screen.queryByLabelText('Feedback on the whole round')).not.toBeInTheDocument()
  })

  it('drops a set whose every candidate collided with pins instead of rendering an empty card', async () => {
    const threeSets = (): api.FeatureSetsResult => ({
      sets: [
        { lens: 'temporal', features: [TEMPORAL_ONLY] },
        { lens: 'ratio', features: [RATIO_ONLY] },
        { lens: 'unary', features: [idea('flag_high_balance')] },
      ],
      recommendation: {
        recommended_lens: 'temporal',
        reasoning: 'recency signals move earliest for a churn horizon',
        caveat: CAVEAT,
      },
      rejections: [],
    })
    await renderAndGenerateSets(threeSets())
    await screen.findByText('Temporal set')
    await selectCandidate('days_since_last_txn')
    contractConsideredSet.mockResolvedValueOnce(considered(threeSets()))
    await submitSetFeedback('sharper signals')
    await screen.findByText('Kept')
    // The temporal set's only candidate collided with the pin: no empty card renders...
    expect(screen.queryByText('Temporal set')).not.toBeInTheDocument()
    expect(screen.getByText('Ratio set')).toBeInTheDocument()
    expect(screen.getByText('Unary set')).toBeInTheDocument()
    // ...and the recommended-but-emptied lens never becomes the active view.
    expect(screen.getByRole('button', { name: /ratio set/i }))
      .toHaveAttribute('aria-pressed', 'true')
    // The kept pick stays visible in the surviving views.
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
  })

  it('a kept row never claims the currently-viewed lens in the tray note', async () => {
    await renderAndGenerateSets(multiSetRound())
    await selectCandidate('days_since_last_txn')
    expect(screen.getByText('from the Temporal set')).toBeInTheDocument()
    contractConsideredSet.mockResolvedValueOnce(considered(multiSetRound()))
    await submitSetFeedback('sharper recency signals')
    await screen.findByText('Kept')
    // The pinned pick left the sets model: its origin is neutral, so the note reads kept.
    expect(screen.getByText('kept from an earlier round')).toBeInTheDocument()
    expect(screen.queryByText('from the Temporal set')).not.toBeInTheDocument()
    // Reselecting the kept row while a set view shows must not stamp the viewed lens.
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select days_since_last_txn' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select days_since_last_txn' }))
    expect(screen.getByText('kept from an earlier round')).toBeInTheDocument()
    expect(screen.queryByText(/from the (Temporal|Ratio) set/)).not.toBeInTheDocument()
  })

  it('locks both feedback channels while the tray is confirming approval', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await userEvent.type(screen.getByLabelText('Feedback on the whole round'), 'one note')
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    expect(screen.getByRole('button', { name: /regenerate with feedback/i })).toBeDisabled()
    for (const button of screen.getAllByRole('button', { name: 'Give feedback' })) {
      expect(button).toBeDisabled()
    }
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: /regenerate with feedback/i })).toBeEnabled()
    for (const button of screen.getAllByRole('button', { name: 'Give feedback' })) {
      expect(button).toBeEnabled()
    }
    // The initial generate ran on considered-set; the locked channel means no feedback round
    // ever fired, so considered-set was called exactly once.
    expect(contractConsideredSet).toHaveBeenCalledTimes(1)
    expect(registerFeature).not.toHaveBeenCalled()
  })
})

describe('per-candidate feedback', () => {
  async function openRefineAndSend(instruction: string, round = 1) {
    await userEvent.click(screen.getByRole('button', { name: 'Give feedback' }))
    await userEvent.type(screen.getByLabelText('What should change'), instruction)
    await userEvent.click(screen.getByRole('button', {
      name: `Send feedback for one revision · round ${round} of 3`,
    }))
  }

  it('sends one revision request carrying the candidate, instruction, and scope', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA], { source: 'deposits' })
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    expect(await screen.findByText('Re-checked after revision')).toBeInTheDocument()
    expect(refineCandidate).toHaveBeenCalledWith(
      {
        name: 'avg_balance', description: 'average balance per customer',
        derives_from: ['public.accounts.balance'], aggregation: 'avg',
        grain_table: 'customers',
      },
      'use a 30 day window', 'deposits', null, null, 'predict churn')
    expect(refineCandidate).toHaveBeenCalledTimes(1)
    // The revision is recorded and attributed.
    expect(
      screen.getByText('recorded · from user:dev · "use a 30 day window"'),
    ).toBeInTheDocument()
    // Field-level diff: changed fields old struck through, new inserted.
    expect(screen.getByText('avg_balance', { selector: 'del' })).toBeInTheDocument()
    expect(screen.getByText('avg_balance_30d', { selector: 'ins' })).toBeInTheDocument()
    expect(screen.getByText('avg', { selector: 'del' })).toBeInTheDocument()
    expect(screen.getByText('avg_30d', { selector: 'ins' })).toBeInTheDocument()
    // The derives pairs are identical: marked unchanged, never silently omitted.
    expect(screen.getAllByText('unchanged')).toHaveLength(1)
    // A suggestion is never a registration, and the candidate itself is untouched so far.
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeInTheDocument()
  })

  it('approve revision replaces the candidate, keeps selection, and registers the revised spec', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    registerFeature.mockResolvedValue('feat_31')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await userEvent.click(await screen.findByRole('button', { name: 'Approve revision' }))
    // The row now carries the revised data plus the chip; the selection survived.
    expect(screen.getByText('avg_balance_30d')).toBeInTheDocument()
    expect(screen.getByText('Revised · R1')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance_30d' })).toBeChecked()
    // Registration still takes the explicit confirm, and uses the REVISED spec with lineage
    // from the revised backend pairs.
    expect(registerFeature).not.toHaveBeenCalled()
    await registerSelection(1)
    expect(registerFeature).toHaveBeenCalledWith({
      name: 'avg_balance_30d', description: '30 day average balance',
      grain_table: 'customers', aggregation: 'avg_30d', as_of_column: null,
      derives_from: [{ catalog_source: 'cards', object_ref: 'public.accounts.balance' }],
    })
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('feat_31')).toBeInTheDocument()
  })

  it('revert to original discards the revision but the round stays consumed', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to original' }))
    expect(screen.queryByText('avg_balance_30d')).not.toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText('Revised · R1')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve revision' })).not.toBeInTheDocument()
    // The engine ran: the round is consumed either way.
    expect(screen.getByRole('button', {
      name: 'Send feedback for one revision · round 2 of 3',
    })).toBeInTheDocument()
  })

  it('keeps Approve revision and Revert inert while a register batch is confirming or in flight', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    const pending = deferred<string>()
    registerFeature.mockImplementation(() => pending.promise)
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await screen.findByRole('button', { name: 'Approve revision' })
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    // Confirm step: both revision actions lock with the feedback channels.
    expect(screen.getByRole('button', { name: 'Approve revision' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revert to original' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm approval' }))
    // In flight: an Approve revision click is inert, even force-dispatched past the disabled
    // attribute, so the batch writes the ORIGINAL spec.
    const approve = screen.getByRole('button', { name: 'Approve revision' })
    expect(approve).toBeDisabled()
    await userEvent.click(approve)
    fireEvent.click(approve)
    await act(async () => {
      pending.resolve('feat_50')
    })
    expect(await screen.findByText('feat_50')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(registerFeature).toHaveBeenCalledWith(IDEA_SPEC)
    // The registered row shows exactly what was written: the original, never the revision.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText('avg_balance_30d')).not.toBeInTheDocument()
    expect(screen.queryByText('Revised · R1')).not.toBeInTheDocument()
  })

  it('announces the pending revision block as a status region', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await screen.findByText('Re-checked after revision')
    const status = screen.getAllByRole('status').find(el =>
      el.textContent?.includes('Re-checked after revision'))
    expect(status).toBeTruthy()
  })

  it('moves focus to the candidate row when Approve revision unmounts the block', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await userEvent.click(await screen.findByRole('button', { name: 'Approve revision' }))
    const row = screen.getByText('avg_balance_30d').closest('li')
    expect(row).not.toBeNull()
    expect(row).toHaveFocus()
  })

  it('moves focus to the candidate row when Revert to original unmounts the block', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    await userEvent.click(await screen.findByRole('button', { name: 'Revert to original' }))
    const row = screen.getByText('avg_balance').closest('li')
    expect(row).not.toBeNull()
    expect(row).toHaveFocus()
  })

  it('renders a gauntlet rejection as a danger line, consuming the round, changing nothing', async () => {
    refineCandidate.mockResolvedValue({
      rejected: { reason: 'leaks target', code: 'LEAKAGE' },
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use the churn label')
    expect(await screen.findByText(
      /rejected this revision: leaks target \(leakage\)\. The round is consumed/,
    )).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve revision' })).not.toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Send feedback for one revision · round 2 of 3',
    })).toBeInTheDocument()
  })

  it('disables per-candidate feedback after three rounds', async () => {
    refineCandidate.mockResolvedValue({
      rejected: { reason: 'leaks target', code: 'LEAKAGE' },
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Give feedback' }))
    for (let round = 1; round <= 3; round++) {
      const input = screen.getByLabelText('What should change')
      await userEvent.clear(input)
      await userEvent.type(input, `round ${round} note`)
      await userEvent.click(screen.getByRole('button', {
        name: `Send feedback for one revision · round ${round} of 3`,
      }))
      expect(await screen.findByText(/rejected this revision/)).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: 'Rounds exhausted' })).toBeDisabled()
    expect(screen.getByLabelText('What should change')).toBeDisabled()
    expect(refineCandidate).toHaveBeenCalledTimes(3)
  })

  it('sends exactly one refine when the box is double-submitted in flight', async () => {
    const pending = deferred<api.RefineResult>()
    refineCandidate.mockImplementationOnce(() => pending.promise)
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    expect(screen.getByRole('button', { name: 'Requesting revision…' })).toBeDisabled()
    const form = screen.getByLabelText('What should change').closest('form')
    if (!form) throw new Error('refine form not found')
    await act(async () => {
      fireEvent.submit(form)
    })
    expect(refineCandidate).toHaveBeenCalledTimes(1)
    await act(async () => {
      pending.resolve({ revised: REVISED })
    })
    expect(await screen.findByText('Re-checked after revision')).toBeInTheDocument()
    expect(refineCandidate).toHaveBeenCalledTimes(1)
  })

  it('registered rows take no feedback', async () => {
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await registerSelection(1)
    await screen.findByText('feat_01')
    // Only the unregistered row still offers the action.
    expect(screen.getAllByRole('button', { name: 'Give feedback' })).toHaveLength(1)
    const registeredRow = screen.getByText('feat_01').closest('li')
    if (!registeredRow) throw new Error('registered row not found')
    expect(
      within(registeredRow).queryByRole('button', { name: 'Give feedback' }),
    ).not.toBeInTheDocument()
  })

  it('drafts offer no engine feedback: a draft is revised by editing its line', async () => {
    await renderAndDraft()
    expect(screen.queryByRole('button', { name: 'Give feedback' })).not.toBeInTheDocument()
  })

  it('drops a revision that arrives after its candidate registered', async () => {
    const pending = deferred<api.RefineResult>()
    refineCandidate.mockImplementationOnce(() => pending.promise)
    registerFeature.mockResolvedValue('feat_40')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    // The human registers the row while the engine is still revising it.
    await selectCandidate('avg_balance')
    await registerSelection(1)
    expect(await screen.findByText('feat_40')).toBeInTheDocument()
    await act(async () => {
      pending.resolve({ revised: REVISED })
    })
    // The registered row is immutable: no revision block, no approve, the original data.
    expect(screen.queryByRole('button', { name: 'Approve revision' })).not.toBeInTheDocument()
    expect(screen.queryByText('avg_balance_30d')).not.toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(registerFeature).toHaveBeenCalledWith(IDEA_SPEC)
  })

  it('surfaces the missing-provider notice on refine and consumes no round', async () => {
    refineCandidate.mockRejectedValue(new api.ApiError(503, 'not configured'))
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/ai assist is not configured/i)
    expect(screen.getByRole('button', {
      name: 'Send feedback for one revision · round 1 of 3',
    })).toBeInTheDocument()
  })

  it('a kept candidate keeps its consumed refine rounds through a whole-round regeneration', async () => {
    refineCandidate.mockResolvedValue({
      rejected: { reason: 'leaks target', code: 'LEAKAGE' },
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    await openRefineAndSend('use the churn label')
    expect(await screen.findByText(/rejected this revision/)).toBeInTheDocument()
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([idea('inactivity_days')])))
    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'more behavioral signals')
    await userEvent.click(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    }))
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    // The pinned row kept its refine counter: the next revision is round 2, not a reset.
    expect(screen.getByText('Kept')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Send feedback for one revision · round 2 of 3',
    })).toBeInTheDocument()
  })

  it('a refined candidate is not governable (its idea diverged from the persisted snapshot)', async () => {
    refineCandidate.mockResolvedValue({ revised: REVISED })
    await renderAndGenerate([IDEA])
    await selectCandidate('avg_balance')
    // Fresh, the candidate is governable.
    expect(screen.getByRole('button', { name: 'Govern 1' })).toBeInTheDocument()
    // Approve a revision: approveRevision mutates the idea IN PLACE, so it no longer matches the
    // considered-set snapshot the server reconstructs the choice from. Governing it would 422 (name
    // changed) or silently mint pre-refine data (name kept) — bug_001. It must drop out of Govern.
    await openRefineAndSend('use a 30 day window')
    await userEvent.click(await screen.findByRole('button', { name: 'Approve revision' }))
    expect(screen.getByText('Revised · R1')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Govern/ })).not.toBeInTheDocument()
    // Register stays available (it uses the revised spec directly, no snapshot reconstruction).
    expect(
      screen.getByRole('button', { name: 'Approve and register 1 feature' }),
    ).toBeInTheDocument()
    expect(contractDraft).not.toHaveBeenCalled()
  })
})

describe('govern', () => {
  // A ContractDraft for avg_balance, mirroring IDEA. contractDraft returns it wrapped; the
  // server-side intent from the considered-set mock is 'int_1' (see `considered`).
  const AVG_DRAFT: api.ContractDraft = {
    feature_name: 'avg_balance', definition: 'average balance per customer',
    grain_table: 'customers', aggregation: 'avg', as_of_column: null,
    derives_from: ['public.accounts.balance'], target_ref: null,
    derives_pairs: [['cards', 'public.accounts.balance']], join_path: [],
  }

  it('governs a selected generated candidate through draft + confirm into a signed contract', async () => {
    contractDraft.mockResolvedValue({ draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1' })
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_1', feature_id: 'feat_1', feature_name: 'avg_balance', version: 1,
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    // Govern is offered because a governing intent exists and the pick is generated.
    await userEvent.click(screen.getByRole('button', { name: 'Govern 1' }))
    expect(screen.getByText(
      'Governing runs the safety gauntlet and mints a signed contract per feature — a design '
      + 'check, not a proof it predicts well.',
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    // The row shows the minted contract; the two-gate flow ran with the intent from generate.
    expect(await screen.findByText(/governed/i)).toBeInTheDocument()
    expect(screen.getByText('contract_1')).toBeInTheDocument()
    expect(contractDraft).toHaveBeenCalledWith('int_1', 'alternative', 'avg_balance')
    expect(contractConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ feature_name: 'avg_balance' }), 'int_1')
    expect(contractDraft).toHaveBeenCalledTimes(1)
    expect(contractConfirm).toHaveBeenCalledTimes(1)
    // Govern is a parallel path: it never registers, and the governed row is done (no checkbox).
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.queryByRole('checkbox', { name: 'Select avg_balance' })).not.toBeInTheDocument()
  })

  it('a whole-round feedback refreshes the intent; kept candidates are not governable, fresh ones are', async () => {
    contractDraft.mockResolvedValue({ draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1' })
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_2', feature_id: 'feat_2', feature_name: 'inactivity_days', version: 1,
    })
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    // Before feedback: the governing intent from generate makes Govern available.
    expect(screen.getByRole('button', { name: 'Govern 1' })).toBeInTheDocument()
    // Feedback routes through considered-set and mints a FRESH intent ('int_1') over the guided set.
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([idea('inactivity_days')])))
    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'more behavioral signals')
    await userEvent.click(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    }))
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    // The kept pin (avg_balance) came from the PRIOR generation, so it is NOT in the new intent's
    // snapshot: with only the kept candidate selected, Govern is absent. Register is unaffected.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('Kept')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Govern/ })).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Approve and register 1 feature' }),
    ).toBeInTheDocument()
    // Selecting a FRESH post-feedback candidate DOES offer Govern over the refreshed intent: only
    // the fresh one is governable (the kept one is not), so the button reads Govern 1.
    await selectCandidate('inactivity_days')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Govern 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    expect(await screen.findByText(/governed/i)).toBeInTheDocument()
    expect(screen.getByText('contract_2')).toBeInTheDocument()
    // The two-gate flow ran with the FRESH intent from the feedback round, for the fresh candidate.
    expect(contractDraft).toHaveBeenCalledWith('int_1', 'alternative', 'inactivity_days')
    expect(contractConfirm).toHaveBeenCalledWith(AVG_DRAFT, 'int_1')
    expect(contractDraft).toHaveBeenCalledTimes(1)
    expect(contractConfirm).toHaveBeenCalledTimes(1)
  })

  it('marks the candidate with the failure and does not govern it when confirm rejects', async () => {
    contractDraft.mockResolvedValue({ draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1' })
    contractConfirm.mockRejectedValue(
      new api.ApiError(422, 'the safety gauntlet rejected the contract'))
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Govern 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    // The failure surfaces on the candidate row; it is never marked governed and stays selectable.
    expect(
      await screen.findByText('the safety gauntlet rejected the contract'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/governed/i)).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeInTheDocument()
    // The failed candidate stays selected, so Govern is offered again for a retry.
    expect(screen.getByRole('button', { name: 'Govern 1' })).toBeInTheDocument()
  })
})
