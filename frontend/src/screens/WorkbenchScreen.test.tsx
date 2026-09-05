import { waitFor, act, fireEvent, render, screen, within } from '@testing-library/react'
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
    contractRecognitions: vi.fn(),
    targetForIntent: vi.fn(),
    attachTargetToIntent: vi.fn(),
    proposeTarget: vi.fn(),
    listTargetEntities: vi.fn(),
    contractIntake: vi.fn(),
    contractIntakeTarget: vi.fn(),
    contractDraft: vi.fn(),
    contractConfirm: vi.fn(),
    refineCandidate: vi.fn(),
    featureRecipe: vi.fn(),
    leakageCheck: vi.fn(),
    registerFeature: vi.fn(),
    featureFreshness: vi.fn(),
    contractUoaProposal: vi.fn(),
    contractOptionDetail: vi.fn(),
  }
})
const recommendFeatures = vi.mocked(api.recommendFeatures)
const contractConsideredSet = vi.mocked(api.contractConsideredSet)
const contractRecognitions = vi.mocked(api.contractRecognitions)
const targetForIntent = vi.mocked(api.targetForIntent)
const listTargetEntities = vi.mocked(api.listTargetEntities)
const attachTargetToIntent = vi.mocked(api.attachTargetToIntent)
const proposeTarget = vi.mocked(api.proposeTarget)
const contractIntake = vi.mocked(api.contractIntake)
const contractIntakeTarget = vi.mocked(api.contractIntakeTarget)
const contractDraft = vi.mocked(api.contractDraft)
const contractConfirm = vi.mocked(api.contractConfirm)
const refineCandidate = vi.mocked(api.refineCandidate)
const featureRecipe = vi.mocked(api.featureRecipe)
const contractUoaProposal = vi.mocked(api.contractUoaProposal)
const contractOptionDetail = vi.mocked(api.contractOptionDetail)
const registerFeature = vi.mocked(api.registerFeature)
const featureFreshness = vi.mocked(api.featureFreshness)

beforeEach(() => {
  targetForIntent.mockResolvedValue(null)
  // The embedded target panel loads the catalog's anchorable entities; with a real catalog now
  // always present on the brief, an unmocked fetch would reject and the panel would honestly
  // report "no keyed spine table" instead of rendering its form.
  listTargetEntities.mockResolvedValue([
    { entity: 'customer', spine_table: 'accounts', spine_ref: 'public.accounts.id' },
  ])
  recommendFeatures.mockReset()
  contractConsideredSet.mockReset()
  contractRecognitions.mockReset()
  contractIntake.mockReset()
  contractIntakeTarget.mockReset()
  // The mandatory read is degrade-never-block: tests that don't script it exercise exactly the
  // degraded path (older backend / no LLM), where the manual target field carries the flow.
  contractIntake.mockRejectedValue(new Error('intake unavailable in this test'))
  contractIntakeTarget.mockRejectedValue(new Error('intake unavailable in this test'))
  contractDraft.mockReset()
  contractConfirm.mockReset()
  refineCandidate.mockReset()
  featureRecipe.mockReset()
  registerFeature.mockReset()
  featureFreshness.mockReset()
  contractUoaProposal.mockReset()
  contractOptionDetail.mockReset()
  // B10 default: no proposal — the UOA block stays absent unless a test scripts one.
  contractUoaProposal.mockResolvedValue({ proposed: null, alternatives: [], contradiction: null })
  // Most pre-Delivery-0 tests pin the emergency compatibility UI. Delivery-0 tests explicitly
  // unstub this value to exercise the release-safe confirmation default.
  vi.unstubAllEnvs()
  vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '0')
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
// Same display name in two sets deliberately represents two independently selectable options.
const SHARED = idea('txn_count_shared')
const SHARED_RECIPE: api.FeatureIdea = {
  ...SHARED,
  generation_source: 'recipe',
  recipe_id: 'txn_count_recipe',
  candidate_status: 'grounded',
}

const CAVEAT =
  'advisory only: a fit/coverage judgment over the metadata, not a performance prediction; '
  + 'confirm the winner with a backtest once features are computed'

function multiSetRound(rejections: api.Rejection[] = []): api.FeatureSetsResult {
  return {
    sets: [
      { lens: 'temporal', features: [TEMPORAL_ONLY, SHARED] },
      { lens: 'ratio', features: [RATIO_ONLY, SHARED_RECIPE] },
    ],
    recommendation: {
      recommended_lens: 'temporal',
      reasoning: 'recency signals move earliest for a churn horizon',
      caveat: CAVEAT,
    },
    rejections,
  }
}

// ONE typed factory for recognition responses. `RecognitionResp` grows required fields over time
// — the repair seam (Task 5) made `recognition_quality` and `ambiguity_note` mandatory — and this
// file had four hand-written copies of the shape, so a single contract change broke three
// unrelated fixtures at once. Overrides layer on top of the minimum classified answer.
//
// The default quality is `null`, which is the HONEST legacy state (an attempt whose quality was
// never recorded), not an invented `clean`: a fixture must not claim the platform observed
// something it did not. Tests about the five dispositions pass their own quality explicitly.
function recognitionResp(over: Partial<api.RecognitionResp> = {}): api.RecognitionResp {
  return {
    intent_id: 'int_1',
    recognition_id: 'rec_1',
    status: 'classified',
    unscoped: false,
    candidates: [{
      use_case_id: 'churn', display_name: 'Customer churn',
      relationship: 'primary', confidence: 'high', evidence_spans: [],
    }],
    modelling_contexts: [],
    target_entity: null,
    warnings: [],
    recognition_quality: null,
    ambiguity_note: null,
    ...over,
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

// E4 cutover: the form collects a catalog source and a target column. There is no entity field —
// the engine plans over ONE frozen catalog context, so an entity-only request is refused typed by
// the route and the screen stopped inviting it.
interface Scope {
  source?: string
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
    intent_id: 'int_1',
    anchor: null,
    alternatives: round.sets.map((set, setIndex) => ({
      ...set,
      features: set.features.map((feature, featureIndex) => ({
        ...feature,
        option_id: feature.option_id ?? `opt_${setIndex}_${featureIndex}_${feature.name}`,
      })),
    })),
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
  // A catalog is REQUIRED now — the copy always said so, and the server always refused without
  // one (SEMANTIC_REQUIRES_CATALOG_SOURCE); only these tests could generate catalog-free.
  await userEvent.type(screen.getByLabelText('Catalog source'), scope.source ?? 'deposits')
  // NOTE: `scope.target` can no longer be typed here. The intake form used to carry a "Target
  // column" field, which asserted the thing being predicted is a COLUMN before anything had read
  // the objective. Naming a column now happens at the scope step, where the read either produced
  // one or reported that it could not — see `nameTargetAtScopeStep`.
  await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
  await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
}

async function renderAndGenerateRaw() {
  // The caller has already queued its own contractConsideredSet mock (e.g. with v2 sections).
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
  await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
  await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
}

async function renderAndGenerateSets(round: api.FeatureSetsResult) {
  contractConsideredSet.mockResolvedValue(considered(round))
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
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
  await userEvent.click(screen.getByRole('button', { name: 'Save ideas' }))
}

// Slice 1: once a brief has been submitted the intake form collapses to the compact submitted
// brief, so every post-submit interaction with a brief field goes through the explicit Revise
// brief control. A no-op before the first round (the form is already the page).
async function reviseBrief() {
  const revise = screen.queryByRole('button', { name: 'Revise brief' })
  if (revise) await userEvent.click(revise)
}

async function openDescribe() {
  // Path 2 (write definitions myself) is a card in the draft shell, so it comes back with the form.
  await reviseBrief()
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
    expect(gateState('Plan over the catalog')).toBe('todo')
    expect(gateState('Compare, mix, give feedback')).toBe('todo')
    expect(gateState('You approve')).toBe('todo')
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    // Goal alone is not the whole brief: the gate stays active until the hypothesis is given too
    // (else it would falsely promise the next step while Generate silently no-ops — bug_004).
    expect(gateState('State the goal')).toBe('active')
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    expect(gateState('State the goal')).toBe('done')
    expect(gateState('Plan over the catalog')).toBe('active')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(gateState('Plan over the catalog')).toBe('done')
    expect(gateState('Compare, mix, give feedback')).toBe('active')
    expect(gateState('You approve')).toBe('todo')
    await selectCandidate('avg_balance')
    expect(gateState('Compare, mix, give feedback')).toBe('done')
    expect(gateState('You approve')).toBe('active')
    await registerSelection(1)
    expect(await screen.findByText('feat_01')).toBeInTheDocument()
    expect(gateState('You approve')).toBe('done')
  })

  // The strip copy is PINNED because it is the screen's promise about who does what. After the
  // E4 cutover the promise changed: cell 1 names the two human steps before generation (the scope
  // confirmation, and the optional unit of analysis), cell 2 names the ONE engine's real output
  // (governed recipes + model intents over the frozen catalog context, not "one set per strategy
  // lens" — that was the deleted free-form generator), and cell 4 speaks the shipped vocabulary
  // (save an idea or govern a contract; nothing "registers" any more).
  it('names the actor on every gate and pins the post-cutover copy', () => {
    render(<WorkbenchScreen />)
    const strip = screen.getByRole('list', { name: 'Where you are in the loop' })
    expect(within(strip).getAllByText('You')).toHaveLength(3)
    expect(within(strip).getByText('Engine')).toBeInTheDocument()
    expect(
      within(strip).getByText(
        'Nothing generates until you confirm the scope, and optionally the unit of analysis.'),
    ).toBeInTheDocument()
    expect(
      within(strip).getByText(
        "Governed recipes and model intents over the catalog's confirmed meaning."),
    ).toBeInTheDocument()
    expect(
      within(strip).getByText('Take a set or pick a la carte across sets.'),
    ).toBeInTheDocument()
    expect(
      within(strip).getByText('Nothing is saved or governed without your click, under your name.'),
    ).toBeInTheDocument()
    // The deleted generator's vocabulary is gone from the strip, not merely reworded around.
    expect(within(strip).queryByText(/strategy lens/i)).toBeNull()
  })

  it('the engine path card describes the engine, not the deleted free-form generator', () => {
    render(<WorkbenchScreen />)
    expect(screen.getByText(
      "Governed recipes and model intents planned over this catalog's confirmed meaning, "
      + 'grouped by operation class — blockers and their next steps named, never hidden.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/one validated set per strategy lens/i)).toBeNull()
  })
})

describe('generation', () => {
  it('passes the hypothesis, goal, and every scope field through to the considered-set call', async () => {
    await renderAndGenerate([], { source: 'deposits' })
    // `targetRef` is undefined because the brief no longer collects one. The target is READ from
    // the objective and confirmed at the scope step; a client-supplied column asserted up front
    // what only the reading can establish.
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn', {
      catalogSource: 'deposits', targetRef: undefined,
    })
  })

  it('a blank catalog is refused AT THE BRIEF, not thirty seconds later by the server', async () => {
    // The copy said "Required." from the start; nothing enforced it, and a catalog-free run
    // sailed through recognition and scope review to die on the server's
    // SEMANTIC_REQUIRES_CATALOG_SOURCE — the owner hit exactly that, three times, as a bare 422.
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(contractConsideredSet).not.toHaveBeenCalled()
    expect(contractRecognitions).not.toHaveBeenCalled()
    expect(screen.getByText(/name a catalog source/i)).toBeInTheDocument()
  })

  // E4 cutover: entity-only generation is refused typed by the route
  // (422 SEMANTIC_REQUIRES_CATALOG_SOURCE) and the cross-catalog lens is unreachable over HTTP, so
  // the form must not offer an Entity field — and must say the catalog is required, not optional.
  it('offers no entity field and frames the catalog source as required', () => {
    render(<WorkbenchScreen />)
    expect(screen.queryByLabelText('Entity')).toBeNull()
    expect(screen.getByText(
      "Required. The catalog the engine plans over: generation reads one catalog's governed "
      + 'meaning. Cross-catalog generation returns in a later release.',
    )).toBeInTheDocument()
  })

  it('shows the empty note only after a generation round returns nothing', async () => {
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([])))
    render(<WorkbenchScreen />)
    expect(screen.queryByText(/no grounded candidates/i)).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
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
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
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

  it('renders the server’s own 503 sentence and never falls back to the plain endpoint', async () => {
    // T9 item 1 — THE MASK THIS TASK EXISTS TO REMOVE. Every 503 used to render one hardcoded
    // sentence ("no LLM provider is enabled"), so the day a 503 carried a GOVERNANCE INTERLOCK
    // the screen told the owner to go look at provider configuration. The status word is not a
    // diagnosis; the response's `detail` is the only thing that knows why.
    const detail = 'cross-catalog planning is interlocked: no activation ceremony for source ftr'
    contractConsideredSet.mockRejectedValue(new api.ApiError(503, detail))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
    expect(alert).not.toHaveTextContent(/no LLM provider/i)
    // The no-silent-fallback rule is unchanged: /features/recommend is never tried behind a 503.
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
    // The meta line counts what the SERVER stamped. This fixture's cards all read DESIGN-CHECKED.
    expect(screen.getAllByText(/2 features · 2 design-checked/)).toHaveLength(2)
    // Advisory panel: the pick, the reasoning, and the backend caveat verbatim.
    expect(screen.getByText(/Engine's pick: Temporal\./)).toBeInTheDocument()
    expect(screen.getByText(/recency signals move earliest for a churn horizon/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(CAVEAT.slice(0, 40)))).toBeInTheDocument()
  })

  // T9 item 5. The set card said "all design-checked" unconditionally. Since T3 the server derives
  // the stamp from the recipe's readiness as well as the gauntlet, and 3 of 317 registry recipes
  // can earn it — so on a real round that clause is false for essentially every card in the set,
  // and it sits directly above the per-card chips that say UNVERIFIED.
  it('the set card counts the stamps the server gave, and claims none it did not', async () => {
    await renderAndGenerateSets({
      // The temporal set holds one stamped card and one UNVERIFIED; the ratio set holds neither.
      sets: [
        { lens: 'temporal', features: [TEMPORAL_ONLY, { ...SHARED, verification: 'UNVERIFIED' }] },
        {
          lens: 'ratio',
          features: [
            { ...RATIO_ONLY, verification: 'UNVERIFIED' },
            { ...SHARED_RECIPE, verification: 'UNVERIFIED' },
          ],
        },
      ],
      recommendation: null,
      rejections: [],
    })
    await screen.findByText('Temporal set')
    const cards = document.querySelector('.sets') as HTMLElement
    // One of the temporal set's two earned the stamp. The card says one, never "all".
    expect(within(cards).getByText(/2 features · 1 design-checked/)).toBeInTheDocument()
    expect(within(cards).queryByText(/all design-checked/)).toBeNull()
    // The ratio set earned none, so it claims nothing at all rather than reporting a zero.
    expect(within(cards).getByText(/^2 features$/)).toBeInTheDocument()
    expect(within(cards).queryAllByText(/design-checked/)).toHaveLength(1)
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
    expect(screen.getByRole('checkbox', {
      name: 'Select txn_count_shared (temporal; Free-form)',
    })).toBeChecked()
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

  it('keeps same-name options in separate lenses as distinct selectable variants', async () => {
    await renderAndGenerateSets(multiSetRound())
    await screen.findByText('txn_count_shared')
    const temporal = screen.getByRole('checkbox', {
      name: 'Select txn_count_shared (temporal; Free-form)',
    })
    await userEvent.click(temporal)
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    // The ratio option has the same display name but a different opaque identity.
    expect(screen.getByRole('checkbox', {
      name: 'Select txn_count_shared (ratio; Recipe · txn_count_recipe)',
    })).not.toBeChecked()
    expect(screen.getByText('grounded')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
  })

  it('registers only the selected same-name variant', async () => {
    registerFeature.mockResolvedValue('feat_20')
    featureFreshness.mockResolvedValue(FRESH)
    await renderAndGenerateSets(multiSetRound())
    await userEvent.click(screen.getByRole('checkbox', {
      name: 'Select txn_count_shared (temporal; Free-form)',
    }))
    await registerSelection(1)
    expect(await screen.findByText('feat_20')).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    expect(screen.queryByText('feat_20')).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', {
      name: 'Select txn_count_shared (ratio; Recipe · txn_count_recipe)',
    })).toBeInTheDocument()
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
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
    expect(screen.getByText('1 rejected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show' }))
    expect(screen.getByText('nps_score_avg')).toBeInTheDocument()
  })
})

// ── T2: the needs-setup lane — a shorter list with a reason given ──────────────────────────────
//
// The live cib arrangement this program came from returns ideas=0, actionable=0, needs_setup=114.
// Before this lane the screen answered that with "No grounded candidates for that goal. Rephrase
// the goal, or change the catalog source" — a wrong REMEDY on top of a hidden fact: 114 candidates
// were planned, and every one of them is waiting on a binding a person can settle.
//
// Every sentence below is copied from `UnboundOperandV1.sentence` in
// overlay/upload/semantic_projection.py. The lane renders those; it composes nothing — because
// "did not bind" is three conditions and only `unresolved` is an absence.
describe('the needs-setup lane', () => {
  const AMBIGUOUS: api.UnboundOperand = {
    role: 'flow', concept: 'monetary_flow', operand_class: 'measure', status: 'ambiguous',
    reason_codes: [], resolution: 'a human adjudicates the tie',
    tied_refs: ['public.txn.amt_local', 'public.txn.amt_usd'],
    sentence: '2 columns carry monetary_flow and the tie is unadjudicated: '
      + 'public.txn.amt_local, public.txn.amt_usd',
  }
  const UNRESOLVED: api.UnboundOperand = {
    role: 'limit', concept: 'credit_limit', operand_class: 'measure', status: 'unresolved',
    reason_codes: [], resolution: '', tied_refs: [],
    sentence: 'no read-scoped column carries credit_limit',
  }
  const NO_VERDICT: api.UnboundOperand = {
    role: 'party', concept: 'counterparty_id', operand_class: 'identifier', status: '',
    reason_codes: [], resolution: '', tied_refs: [],
    sentence: "the binder returned no verdict for the 'party' operand (counterparty_id)",
  }

  function needsSetup(
    name: string, operands: api.UnboundOperand[],
  ): api.NeedsSetupCandidate {
    return {
      name, source_definition_id: `recipe:${name}`, recipe_id: name, catalog_source: 'cib',
      unbound_concepts: [...new Set(operands.map(o => o.concept))],
      unbound_operands: operands,
      sentence: operands.map(o => o.sentence).join('; '),
    }
  }

  async function generateWithSetup(entries: api.NeedsSetupCandidate[]) {
    contractConsideredSet.mockResolvedValue({
      ...considered(singleSetRound([])), contract_version: 2, needs_setup: entries,
    })
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
  }

  it('a round with no cards but setup work says so, and names what did not bind', async () => {
    await generateWithSetup([needsSetup('net_transaction_flow', [AMBIGUOUS, UNRESOLVED])])
    const lane = await screen.findByTestId('needs-setup')
    expect(lane).toHaveTextContent('net_transaction_flow')
    // The AMBIGUOUS operand is a PRESENCE claim, and the tie's own columns are named — that is the
    // difference between "adjudicate this" and "onboard this data".
    expect(lane).toHaveTextContent(
      '2 columns carry monetary_flow and the tie is unadjudicated')
    expect(within(lane).getByText('public.txn.amt_local')).toBeInTheDocument()
    expect(within(lane).getByText('public.txn.amt_usd')).toBeInTheDocument()
    // The UNRESOLVED one is the only absence, and it is the only one worded as one.
    expect(lane).toHaveTextContent('no read-scoped column carries credit_limit')
    // The aggregate is status-NEUTRAL: it never says these concepts are missing.
    expect(lane).toHaveTextContent('monetary_flow')
    expect(lane).not.toHaveTextContent(/missing concepts/i)
  })

  it('is setup work, never a failure, and never the rephrase-the-goal remedy', async () => {
    await generateWithSetup([needsSetup('net_transaction_flow', [UNRESOLVED])])
    await screen.findByTestId('needs-setup')
    // The old answer told the human to rewrite a question that was never the problem.
    expect(screen.queryByText(/Rephrase the goal/i)).toBeNull()
    expect(screen.queryByText(/no grounded candidates for that goal/i)).toBeNull()
  })

  it('a role the binder never ruled on says exactly that — honest absence, not a guess',
    async () => {
      await generateWithSetup([needsSetup('counterparty_concentration', [NO_VERDICT])])
      const lane = await screen.findByTestId('needs-setup')
      expect(lane).toHaveTextContent(
        "the binder returned no verdict for the 'party' operand (counterparty_id)")
    })

  it('an empty needs_setup lane renders nothing at all', async () => {
    await generateWithSetup([])
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
    expect(screen.queryByTestId('needs-setup')).toBeNull()
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

  it('says which USE-gate refusals a person can act on and which are final', async () => {
    // The four Bar-4 codes. Two are things no approval can change ("protected characteristic",
    // "descriptive column"); two name something nobody has set up yet ("needs a ..."). The label
    // has to carry that difference — the fallback would word PERSONAL_DATA_POLICY_REQUIRED as a
    // verdict on the column, sending the reviewer to abandon an idea a policy would allow.
    await renderAndGenerate([IDEA], {}, [
      { name: 'citizenship_propensity', reason: 'cust_ctzn_ctry_cd cannot be a model input', code: 'PROTECTED_CHARACTERISTIC' },
      { name: 'branch_desc_key', reason: 'sol_desc displays and groups; use the code beside it', code: 'DESCRIPTIVE_OPERAND' },
      { name: 'dob_bucket', reason: 'this catalog declares no personal-data use policy', code: 'PERSONAL_DATA_POLICY_REQUIRED' },
      { name: 'total_all_currencies', reason: 'the feature does not bind tran_crncy', code: 'CURRENCY_POLICY_REQUIRED' },
    ])
    await userEvent.click(await screen.findByRole('button', { name: 'Show' }))
    expect(screen.getByText('protected characteristic')).toBeInTheDocument()
    expect(screen.getByText('descriptive column')).toBeInTheDocument()
    expect(screen.getByText('needs a personal-data policy')).toBeInTheDocument()
    expect(screen.getByText('needs a currency decision')).toBeInTheDocument()
    // Never the raw enum token.
    expect(screen.queryByText('PERSONAL_DATA_POLICY_REQUIRED')).not.toBeInTheDocument()
    expect(screen.queryByText('personal data policy required')).not.toBeInTheDocument()
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
      /saves the selected candidates as IDEAS/,
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Save ideas' }))
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
    const confirm = screen.getByRole('button', { name: 'Save ideas' })
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
      /saves the selected candidates as IDEAS/,
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Save ideas' }))
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
    // (Slice 3: submitting from the revise drawer names what it does — a REVISED round.)
    await reviseBrief()
    await userEvent.click(screen.getByRole('button', { name: /generate revised round/i }))
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
    // The brief form is what carries the generate path and the scope fields, so re-open it: the
    // lock under test is on the CONTROLS, not on their visibility.
    await reviseBrief()
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Approve and register 1 feature' }))
    // Confirm step: no new round and no scope edit can pull rows out from under the approval.
    // (Slice 3: the form is in the revise drawer, so its submit names the revised round.)
    expect(screen.getByRole('button', { name: /generate revised round/i })).toBeDisabled()
    expect(screen.getByLabelText('Catalog source')).toBeDisabled()
    // the target field is gone from the brief; the source carries the scope lock
    await userEvent.click(screen.getByRole('button', { name: 'Save ideas' }))
    // Still locked while the batch is in flight.
    expect(screen.getByRole('button', { name: /generate revised round/i })).toBeDisabled()
    expect(screen.getByLabelText('Catalog source')).toBeDisabled()
    await act(async () => {
      pending.resolve('feat_60')
    })
    expect(await screen.findByText('feat_60')).toBeInTheDocument()
    // The lock releases with the batch.
    expect(screen.getByRole('button', { name: /generate revised round/i })).toBeEnabled()
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
    await reviseBrief()
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

  // RETIRED, not re-pointed. Its subject was the one scope edit that invalidated generated
  // candidates WITHOUT touching the source snapshot the drafts were drafted against — the target
  // column. The entity field went at E4 and the target field has now gone too (the brief no longer
  // asserts the target is a column), so no such edit exists to test. Pointing it at Catalog source
  // would assert the OPPOSITE of what it was written to prove, which is worse than deleting it.
  // The surviving guarantee — a source edit clears generated candidates — is covered above.

  it('a scope edit clears generated candidates and the screening note', async () => {
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    // Candidates were planned against this scope: any edit voids them.
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), '2')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
  })

  it('clears the selection when the scope changes', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    await selectCandidate('txn_count')
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'c')
    expect(screen.queryByText('2 selected')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Approve and register 2 features' }),
    ).not.toBeInTheDocument()
  })

  it('a scope edit clears the sets row and the rejections panel too', async () => {
    await renderAndGenerateSets(multiSetRound([
      { name: 'days_to_churn', reason: 'derives from the target column', code: 'LEAKAGE' },
    ]))
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    expect(screen.getByText('1 rejected')).toBeInTheDocument()
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'c')
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

  it('gates the describe path behind the same one notice, in the server’s own words', async () => {
    const detail = 'the authoring provider rejected the request schema (HTTP 400, keyword=type)'
    featureRecipe.mockRejectedValue(new api.ApiError(503, detail))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await openDescribe()
    await draftFeature('total spend per customer')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
    expect(alert).not.toHaveTextContent(/no LLM provider/i)
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

  it('renders an UNVERIFIED stamp quietly and drops the design-checked help line', async () => {
    // T3: the server now derives `verification` from the recipe's READINESS as well as the
    // gauntlet's verdict, so a generated card legitimately arrives UNVERIFIED (on today's
    // registry, 3 of 317 recipes can earn DESIGN-CHECKED — so this is the common case).
    // Two things must follow, and neither did before: the chip must not be the soft-OK tone
    // that reads as a pass, and the page-level sentence explaining DESIGN-CHECKED must not
    // appear over a list where no card wears it.
    const unverified: api.FeatureIdea = { ...IDEA, verification: 'UNVERIFIED' }
    await renderAndGenerate([unverified])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    const chip = screen.getByText('unverified')
    expect(chip).toBeInTheDocument()
    expect(chip.className).toContain('stale')
    expect(chip.className).not.toContain('ok')
    expect(screen.queryByText(/structurally safe against leakage/i)).not.toBeInTheDocument()
  })

  it('keeps the design-checked help line when at least one card still earns the stamp',
    async () => {
      const unverified: api.FeatureIdea = { ...OTHER_IDEA, verification: 'UNVERIFIED' }
      await renderAndGenerate([IDEA, unverified])
      expect(await screen.findByText('avg_balance')).toBeInTheDocument()
      expect(screen.getAllByText('design-checked')).toHaveLength(1)
      expect(screen.getAllByText('unverified')).toHaveLength(1)
      // Scoped, not suppressed: the sentence explains the stamp that IS present.
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

describe('SE-12: the honest tri-state, typed inputs, and outstanding checks', () => {
  const ENGINE_IDEA: api.FeatureIdea = {
    ...idea('activity_recency'),
    generation_source: 'recipe',
    recipe_id: 'customer_activity_recency',
    validation_status: 'NEEDS_EXTERNAL_VALIDATION',
    requirements: [
      { code: 'GRAIN_IS_UNIQUE', operand: ['deposits', 'public.events.customer_id'],
        detail: '[IDENTIFIER_UNIQUENESS] profile this key at the declared grain' },
      { code: 'TEMPORAL_IS_POPULATED', operand: ['deposits', 'public.events.event_ts'],
        detail: '[EVENT_HISTORY_VERIFICATION] verify event history depth' },
    ],
    input_role_bindings: [
      { role: 'who', authority: 'llm/proposed',
        ref: ['deposits', 'public.events.customer_id'], confirmation_required: true },
      { role: 'when', authority: 'human/confirmed',
        ref: ['deposits', 'public.events.event_ts'] },
    ],
  }

  it('never stamps a NEEDS_EXTERNAL_VALIDATION candidate design-checked', async () => {
    await renderAndGenerate([ENGINE_IDEA])
    expect(await screen.findByText('activity_recency')).toBeInTheDocument()
    expect(screen.getByText('needs data checks (2)')).toBeInTheDocument()
    expect(screen.queryByText('design-checked')).not.toBeInTheDocument()
  })

  it('renders one typed input row per role with its measured authority', async () => {
    await renderAndGenerate([ENGINE_IDEA])
    const inputs = await screen.findByRole('list', { name: 'typed inputs' })
    expect(inputs).toHaveTextContent('who')
    expect(inputs).toHaveTextContent('public.events.customer_id')
    expect(inputs).toHaveTextContent('llm/proposed')
    expect(inputs).toHaveTextContent('when')
    expect(inputs).toHaveTextContent('human/confirmed')
    // Only the proposed binding asks for Gate-1 confirmation; the confirmed one is settled.
    // D3 (UI-06): the chip became a deep LINK to the asset field-decision screen.
    expect(screen.getAllByText('needs confirmation →')).toHaveLength(1)
  })

  it('renders outstanding checks as tasks, with the backend prose as fine print', async () => {
    await renderAndGenerate([ENGINE_IDEA])
    const checks = await screen.findByRole('list', { name: 'outstanding checks' })
    expect(checks).toHaveTextContent('Profile uniqueness at the declared grain')
    expect(checks).toHaveTextContent('Verify the time column is populated (event history depth)')
    expect(checks).toHaveTextContent('[IDENTIFIER_UNIQUENESS]')
  })

  it('keeps the design-checked stamp for a DESIGN_CHECKED engine candidate', async () => {
    await renderAndGenerate([{ ...ENGINE_IDEA, validation_status: 'DESIGN_CHECKED',
                               requirements: [], input_role_bindings: [] }])
    expect(await screen.findByText('activity_recency')).toBeInTheDocument()
    expect(screen.getByText('design-checked')).toBeInTheDocument()
    expect(screen.queryByText(/needs data checks/)).not.toBeInTheDocument()
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
      source: 'deposits', target: 'public.labels.churned',
    })
    await selectCandidate('avg_balance')
    // The goal input is edited after the round: feedback still reruns the ROUND's objective.
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Prediction goal'), ' fast')
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([idea('inactivity_days')])))
    await submitSetFeedback('more behavioral signals')
    expect(await screen.findByText('inactivity_days')).toBeInTheDocument()
    // Feedback routes through considered-set with the ROUND's snapshotted hypothesis + objective
    // (the goal input now reads 'predict churn fast') plus the instruction as `feedback`.
    expect(contractConsideredSet).toHaveBeenLastCalledWith(HYPOTHESIS, 'predict churn', {
      // `targetRef` is undefined: the brief no longer collects a column, so feedback carries the
      // round's snapshotted scope and nothing the client asserted about the target.
      catalogSource: 'deposits', targetRef: undefined,
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
    // Previous round held four exact options; the pin stayed and three were replaced.
    expect(screen.getByText(/kept 1 selected, replaced 3/)).toBeInTheDocument()
    // The kept row shows in the temporal view and after switching to the ratio view.
    expect(screen.getAllByText('days_since_last_txn')).toHaveLength(2)
    await userEvent.click(screen.getByRole('button', { name: /ratio set/i }))
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', {
      name: 'Select days_since_last_txn (earlier round)',
    })).toBeChecked()
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
    await reviseBrief()
    await userEvent.click(screen.getByRole('button', { name: /generate revised round/i }))
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
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'c')
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
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'c')
    expect(screen.queryByLabelText('Feedback on the whole round')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    })).toBeInTheDocument()
    expect(screen.queryByText(/Set feedback round \d of 3 · recorded/)).not.toBeInTheDocument()
  })

  it('surfaces the server’s 503 sentence and consumes no round on failure', async () => {
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    const detail = 'the generation lane is drained for migration 1107'
    contractConsideredSet.mockRejectedValueOnce(new api.ApiError(503, detail))
    await submitSetFeedback('one note')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
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

  it('preserves a regenerated same-name option instead of collapsing it into a pin', async () => {
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
    // The pin and regenerated candidate are separate exact choices, so the temporal set remains.
    expect(screen.getByText('Temporal set')).toBeInTheDocument()
    expect(screen.getByText('Ratio set')).toBeInTheDocument()
    expect(screen.getByText('Unary set')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /temporal set/i }))
      .toHaveAttribute('aria-pressed', 'true')
    expect(screen.getAllByText('days_since_last_txn')).toHaveLength(2)
    expect(screen.getByRole('checkbox', {
      name: 'Select days_since_last_txn (earlier round)',
    })).toBeChecked()
    expect(screen.getByRole('checkbox', {
      name: 'Select days_since_last_txn (temporal; Free-form)',
    })).not.toBeChecked()
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
    const kept = screen.getByRole('checkbox', {
      name: 'Select days_since_last_txn (earlier round)',
    })
    await userEvent.click(kept)
    await userEvent.click(kept)
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
    await userEvent.click(screen.getByRole('button', { name: 'Save ideas' }))
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

  it('renders a refusal as a danger line, consuming the round, changing nothing', async () => {
    refineCandidate.mockResolvedValue({
      rejected: { reason: 'leaks target', code: 'LEAKAGE' },
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use the churn label')
    const line = await screen.findByText(
      /refused: leaks target \(leakage\)\. The round is consumed/)
    expect(line).toBeInTheDocument()
    // T9: the line used to open "The safety gauntlet rejected this revision". Most arms of
    // /features/refine-candidate are not the gauntlet at all — an intent-parse rejection, a
    // scope rejection, or (since T2) an operand that did not bind — so the attribution was a
    // cause this screen invented for whatever the server refused with.
    expect(line).not.toHaveTextContent(/safety gauntlet/i)
    expect(screen.queryByRole('button', { name: 'Approve revision' })).not.toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Send feedback for one revision · round 2 of 3',
    })).toBeInTheDocument()
  })

  // T2 reaches the refine seam too: /features/refine-candidate answers a revision whose required
  // operand did not bind with a `needs_setup` envelope and a 200 — "data the reviewer acts on, not
  // an error", in the route's own words. Rendering it in danger styling under a gauntlet heading
  // made setup work look like a safety failure.
  it('a revision that did not BIND is setup work, not a refusal', async () => {
    refineCandidate.mockResolvedValue({
      rejected: {
        reason: 'the revision did not bind: no read-scoped column carries credit_limit',
        code: 'SEMANTIC_NOT_BINDABLE',
        needs_setup: [{
          name: 'drawn_utilisation', source_definition_id: 'intent:1', recipe_id: null,
          catalog_source: 'cib', unbound_concepts: ['credit_limit'],
          unbound_operands: [{
            role: 'limit', concept: 'credit_limit', operand_class: 'measure',
            status: 'unresolved', reason_codes: [], resolution: '', tied_refs: [],
            sentence: 'no read-scoped column carries credit_limit',
          }],
          sentence: 'no read-scoped column carries credit_limit',
        }],
      },
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use the utilisation ratio')
    const line = await screen.findByText(/no read-scoped column carries credit_limit/)
    // The server's sentence, and no cause the screen made up.
    expect(line).not.toHaveTextContent(/safety gauntlet/i)
    expect(line).not.toHaveTextContent(/refused/i)
    // Setup work is not a failure: it does not wear the danger class or announce as an alert.
    expect(line).not.toHaveClass('error')
    expect(line.getAttribute('role')).not.toBe('alert')
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
      expect(await screen.findByText(/This revision was refused/)).toBeInTheDocument()
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

  it('surfaces the server’s 503 sentence on refine, once, and consumes no round', async () => {
    // A 503 goes to the ONE top notice rather than onto the row — and it is the response's own
    // sentence there, not a cause this screen guessed from the status code.
    const detail = 'refine is disabled while the shadow chooser holds the write lock'
    refineCandidate.mockRejectedValue(new api.ApiError(503, detail))
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await openRefineAndSend('use a 30 day window')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(detail)
    expect(alert).not.toHaveTextContent(/no LLM provider/i)
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
    expect(await screen.findByText(/This revision was refused/)).toBeInTheDocument()
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
    expect(screen.getByRole('button', { name: 'Select and draft 1' })).toBeInTheDocument()
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
  // The ROW's governed mark ("Governed <contract> v<n>"), matched precisely rather than by a bare
  // /governed/i: the gates strip also says the word (cell 4 promises nothing is saved or governed
  // without your click), and a matcher that cannot tell the promise from the minted contract would
  // pass whether or not a contract was ever written. The version number is the discriminator —
  // it was the hardcoded "· DESIGN-CHECKED" until T9 removed that claim (see the pin below).
  const GOVERNED_MARK = /Governed .*v\d+/i

  // A ContractDraft for avg_balance, mirroring IDEA. contractDraft returns it wrapped; the
  // server-side intent from the considered-set mock is 'int_1' (see `considered`).
  const AVG_DRAFT: api.ContractDraft = {
    feature_name: 'avg_balance', definition: 'average balance per customer',
    grain_table: 'customers', aggregation: 'avg', as_of_column: null,
    derives_from: ['public.accounts.balance'], target_ref: null,
    derives_pairs: [['cards', 'public.accounts.balance']], join_path: [],
  }

  it('governs a selected generated candidate through draft + confirm into a signed contract', async () => {
    contractDraft.mockResolvedValue({
      draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1', choice_id: 'g1c_1',
    })
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_1', feature_id: 'feat_1', feature_name: 'avg_balance', version: 1,
    })
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    // Govern is offered because a governing intent exists and the pick is generated.
    await userEvent.click(screen.getByRole('button', { name: 'Select and draft 1' }))
    expect(screen.getByText(
      'Governing runs the safety gauntlet and mints a signed contract per feature — a design '
      + 'check, not a proof it predicts well.',
    )).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    // The row shows the minted contract; the two-gate flow ran with the intent from generate.
    expect(await screen.findByText(GOVERNED_MARK)).toBeInTheDocument()
    expect(screen.getByText('contract_1')).toBeInTheDocument()
    expect(contractDraft).toHaveBeenCalledWith(
      'int_1', 'alternative', 'opt_0_0_avg_balance', '', undefined)
    expect(contractConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ feature_name: 'avg_balance' }), 'int_1', 'g1c_1')
    expect(contractDraft).toHaveBeenCalledTimes(1)
    expect(contractConfirm).toHaveBeenCalledTimes(1)
    // Govern is a parallel path: it never registers, and the governed row is done (no checkbox).
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.queryByRole('checkbox', { name: 'Select avg_balance' })).not.toBeInTheDocument()
  })

  // T9 item 5. The governed mark used to append a hardcoded "· DESIGN-CHECKED" to every contract
  // it minted. /contract/confirm's response carries no verification field at all, so that word was
  // the screen's own — and since T3 it is false for essentially every card (3 of 317 registry
  // recipes can earn the stamp). The card's real stamp is already on the row, from the server.
  it('the governed mark states the contract, and claims no design check of its own', async () => {
    const unverified: api.FeatureIdea = { ...IDEA, verification: 'UNVERIFIED' }
    contractDraft.mockResolvedValue({
      draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1', choice_id: 'g1c_1',
    })
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_1', feature_id: 'feat_1', feature_name: 'avg_balance', version: 1,
    })
    await renderAndGenerate([unverified])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Select and draft 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    const mark = await screen.findByText(GOVERNED_MARK)
    expect(mark).toHaveTextContent('contract_1')
    expect(mark).not.toHaveTextContent(/DESIGN-CHECKED/i)
    // …and the stamp the SERVER did put on this card is still the one on screen.
    expect(screen.getByText('unverified')).toBeInTheDocument()
  })

  it('a whole-round feedback refreshes the intent; kept candidates are not governable, fresh ones are', async () => {
    contractDraft.mockResolvedValue({
      draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1', choice_id: 'g1c_1',
    })
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_2', feature_id: 'feat_2', feature_name: 'inactivity_days', version: 1,
    })
    await renderAndGenerate([IDEA, OTHER_IDEA])
    await selectCandidate('avg_balance')
    // Before feedback: the governing intent from generate makes Govern available.
    expect(screen.getByRole('button', { name: 'Select and draft 1' })).toBeInTheDocument()
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
    await userEvent.click(screen.getByRole('button', { name: 'Select and draft 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    expect(await screen.findByText(GOVERNED_MARK)).toBeInTheDocument()
    expect(screen.getByText('contract_2')).toBeInTheDocument()
    // The two-gate flow ran with the FRESH intent from the feedback round, for the fresh candidate.
    expect(contractDraft).toHaveBeenCalledWith(
      'int_1', 'alternative', 'opt_0_0_inactivity_days', '', undefined)
    expect(contractConfirm).toHaveBeenCalledWith(AVG_DRAFT, 'int_1', 'g1c_1')
    expect(contractDraft).toHaveBeenCalledTimes(1)
    expect(contractConfirm).toHaveBeenCalledTimes(1)
  })

  it('marks the candidate with the failure and does not govern it when confirm rejects', async () => {
    contractDraft.mockResolvedValue({
      draft: AVG_DRAFT, unresolved: [], intent_id: 'int_1', choice_id: 'g1c_1',
    })
    contractConfirm.mockRejectedValue(
      new api.ApiError(422, 'the safety gauntlet rejected the contract'))
    await renderAndGenerate([IDEA])
    await screen.findByText('avg_balance')
    await selectCandidate('avg_balance')
    await userEvent.click(screen.getByRole('button', { name: 'Select and draft 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm govern' }))
    // The failure surfaces on the candidate row; it is never marked governed and stays selectable.
    expect(
      await screen.findByText('the safety gauntlet rejected the contract'),
    ).toBeInTheDocument()
    expect(screen.queryByText(GOVERNED_MARK)).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeInTheDocument()
    // The failed candidate stays selected, so Govern is offered again for a retry.
    expect(screen.getByRole('button', { name: 'Select and draft 1' })).toBeInTheDocument()
  })
})

// ------------------------------------------------------ Phase 1B: Gate #1 confirmation + lens ----
describe('Gate #1 scope confirmation', () => {
  // A classified recognition with a primary + one secondary, each carrying an evidence span.
  const RECOGNITION: api.RecognitionResp = recognitionResp({
    candidates: [
      {
        use_case_id: 'churn', display_name: 'Customer churn',
        relationship: 'primary', confidence: 'high', evidence_spans: ['about to leave'],
      },
      {
        use_case_id: 'engagement', display_name: 'Engagement decline',
        relationship: 'secondary', confidence: 'medium', evidence_spans: ['balance is draining'],
      },
    ],
    // Phase-2B SOFT dimensions the recognizer proposed — surfaced for confirm/override at Gate #1.
    modelling_contexts: ['ifrs9'], target_entity: 'customer',
  })

  // A scoped considered-set response: one eligible recipe + one out-of-scope recipe for the lens.
  function scopedConsidered(): api.ConsideredSetResp {
    return {
      intent_id: 'int_1', anchor: null, alternatives: [{ lens: 'temporal', features: [IDEA] }],
      recommendation: null, rejections: [],
      generation_run_id: 'run_1', scope_id: 'scope_1', in_scope_count: 1,
      dispositions: [
        {
          recipe_id: 'recency_since_event', final_disposition: 'eligible', relevance_tier: 'primary',
          applicability: { status: 'COMPLETED', reason_codes: ['in_scope'] },
          grounding: { status: 'COMPLETED', reason_codes: [] },
          safety: { status: 'COMPLETED', reason_codes: [] },
        },
        {
          recipe_id: 'fraud_ring_score', final_disposition: 'out_of_scope', relevance_tier: null,
          applicability: { status: 'COMPLETED', reason_codes: ['not_in_use_case'] },
          grounding: { status: 'NOT_EVALUATED', reason_codes: [] },
          safety: { status: 'NOT_EVALUATED', reason_codes: [] },
        },
      ],
    }
  }

  async function generateFlagOn() {
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
  }

  function dispositionGroup(heading: RegExp): HTMLElement {
    const h3 = screen.getByRole('heading', { level: 3, name: heading })
    return h3.closest('.disposition-group') as HTMLElement
  }

  it('flag OFF: generate calls considered-set once and never recognitions (today’s flow)', async () => {
    // The emergency compatibility flag is pinned to 0 by beforeEach.
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(contractConsideredSet).toHaveBeenCalledTimes(1)
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn', {
      catalogSource: 'deposits', targetRef: undefined,
    })
    expect(contractRecognitions).not.toHaveBeenCalled()
    // No confirm step and no lens render when the flags are off.
    expect(screen.queryByRole('button', { name: /confirm scope and generate/i })).toBeNull()
    expect(screen.queryByText(/how your scope dispositioned/i)).toBeNull()
  })

  it('release default: generate recognises first and never takes the one-shot path', async () => {
    vi.unstubAllEnvs()
    contractRecognitions.mockResolvedValue(RECOGNITION)
    await generateFlagOn()
    expect(contractRecognitions).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn')
    expect(contractConsideredSet).not.toHaveBeenCalled()
    expect(await screen.findByText('Customer churn')).toBeInTheDocument()
  })

  it('flag ON: generate recognises first, renders the proposed scope, and confirm sends the scope', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    // Recognition ran; considered-set has NOT fired yet (scope unconfirmed).
    expect(contractRecognitions).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn')
    expect(contractConsideredSet).not.toHaveBeenCalled()
    // The proposed primary + one evidence span render.
    expect(await screen.findByText('Customer churn')).toBeInTheDocument()
    // the evidence lives behind the settled summary now — expand to inspect it
    await userEvent.click(screen.getByRole('button', { name: /change the scope/i }))
    expect(screen.getByText(/about to leave/)).toBeInTheDocument()
    // Confirm → considered-set with a confirmedScope carrying the recognised primary + secondary.
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        intentId: 'int_1', recognitionId: 'rec_1',
        confirmedScope: expect.objectContaining({
          primary: 'churn', secondary: ['engagement'], expansion: 'exact', unscoped: false,
        }),
      }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
  })

  it('flag ON: removing a secondary drops it from the confirmed scope', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    // secondaries live behind the settled summary — expand to edit them
    await userEvent.click(await screen.findByRole('button', { name: /change the scope/i }))
    await userEvent.click(await screen.findByRole('button', { name: 'Remove Engagement decline' }))
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ primary: 'churn', secondary: [] }),
      }))
  })

  const UOA_PROPOSAL = {
    proposed: { entity: 'Customer', spine_table: 'accounts',
                spine_ref: 'public.accounts.customer_id' },
    alternatives: [
      { entity: 'Customer', spine_table: 'accounts',
        spine_ref: 'public.accounts.customer_id' },
      { entity: 'Account', spine_table: 'positions', spine_ref: 'public.positions.acct_ref' },
    ],
    contradiction: null,
  }

  async function generateFlagOnWithSource() {
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.type(screen.getByLabelText('Catalog source'), 'bank')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
  }

  it('B10: the derived UOA proposal confirms with one click and rides the confirmed scope', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    contractUoaProposal.mockResolvedValue(UOA_PROPOSAL)
    await generateFlagOnWithSource()
    expect(await screen.findByText(/You're predicting per/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Yes' }))
    expect(screen.getByText(/Predicting per/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({
          uoaEntity: 'Customer', spineRef: 'public.accounts.customer_id',
        }),
      }))
  })

  it('B10: a "no" answer offers ONLY the catalog\'s realistic alternatives (closed list)', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    contractUoaProposal.mockResolvedValue(UOA_PROPOSAL)
    await generateFlagOnWithSource()
    await screen.findByText(/You're predicting per/)
    await userEvent.click(screen.getByRole('button', { name: /no — pick another/i }))
    // No free-text input appears — only the spine-backed entities are offered.
    await userEvent.click(screen.getByRole('button', { name: /Account — positions/ }))
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({
          uoaEntity: 'Account', spineRef: 'public.positions.acct_ref',
        }),
      }))
  })

  it('B10: skipping the UOA is free — the scope carries null and generation proceeds', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    contractUoaProposal.mockResolvedValue(UOA_PROPOSAL)
    await generateFlagOnWithSource()
    await screen.findByText(/You're predicting per/)
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ uoaEntity: null, spineRef: null }),
      }))
  })

  it('flag ON: whole-round feedback re-runs recognition before a new scoped generation', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions
      .mockResolvedValueOnce(RECOGNITION)
      .mockResolvedValueOnce({
        ...RECOGNITION,
        recognition_id: 'rec_feedback',
        candidates: [RECOGNITION.candidates[0]],
      })
    contractConsideredSet
      .mockResolvedValueOnce(scopedConsidered())
      .mockResolvedValueOnce({
        ...scopedConsidered(),
        generation_run_id: 'run_2',
        scope_id: 'scope_2',
      })
    await generateFlagOn()
    await userEvent.click(
      await screen.findByRole('button', { name: /confirm scope and generate/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()

    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'more behavioral signals')
    await userEvent.click(screen.getByRole('button', {
      name: 'Regenerate with feedback · round 1 of 3',
    }))

    expect(contractRecognitions).toHaveBeenLastCalledWith(HYPOTHESIS, 'predict churn', {
      feedback: 'more behavioral signals',
      supersedesScopeId: 'scope_1',
    })
    expect(contractConsideredSet).toHaveBeenCalledTimes(1)
    expect(await screen.findByRole(
      'button', { name: /confirm scope and generate/i })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenLastCalledWith(
      HYPOTHESIS,
      'predict churn',
      expect.objectContaining({
        intentId: 'int_1',
        recognitionId: 'rec_feedback',
        feedback: 'more behavioral signals',
        supersedesScopeId: 'scope_1',
      }),
    )
    expect(await screen.findByText(/Set feedback round 1 of 3 · recorded/))
      .toBeInTheDocument()
  })

  it('flag ON: the entity is proposed and confirmable; a proposed CONTEXT is neither', async () => {
    // The entity control is now gated on RANKING — its only consumer — the
    // same argument that removed the modelling-context control.
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    // The entity half is unchanged — proposed, editable, threaded through on confirm.
    //
    // The context half is the honesty proof. This recognition proposes `ifrs9`. The control that
    // let a person accept or reject it is gone (its only consumer, the ranker, is behind a flag
    // this deployment leaves unset), so the proposal must NOT ride through as a confirmed value:
    // `confirmed_scope_dimension` would then record an LLM guess as human-confirmed, which is
    // precisely the claim that record exists to make truthfully. The proposal itself survives
    // where it belongs, on `intent_recognition_attempt`.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    expect(await screen.findByLabelText('Target entity')).toHaveValue('customer')
    expect(screen.queryByText('ifrs9')).toBeNull()
    expect(screen.queryByLabelText('Add modelling context')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({
          modellingContexts: [], targetEntity: 'customer',
        }),
      }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
  })

  it('flag ON: clearing the entity sends a null target entity', async () => {
    // The entity control is now gated on RANKING — its only consumer — the
    // same argument that removed the modelling-context control.
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    await userEvent.click(await screen.findByRole('button', { name: 'Clear entity' }))
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ targetEntity: null }),
      }))
  })

  it('flag ON: a recognizer dimension warning renders a non-fatal hint', async () => {
    // The entity control is now gated on RANKING — its only consumer — the
    // same argument that removed the modelling-context control.
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue({ ...RECOGNITION, warnings: ['UNKNOWN_TARGET_ENTITY'] })
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    expect(await screen.findByText(
      /couldn.t map part of what you described to a known entity/i)).toBeInTheDocument()
  })

  it('flag ON + lens: groups an eligible and an out-of-scope recipe under their headings', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    vi.stubEnv('VITE_INTENT_DISPOSITION_LENS', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    await userEvent.click(await screen.findByRole('button', { name: /confirm scope and generate/i }))
    // The lens renders and each recipe sits under the right disposition heading.
    await screen.findByRole('heading', { name: /how your scope dispositioned/i })
    expect(within(dispositionGroup(/Recommended/)).getByText('recency_since_event'))
      .toBeInTheDocument()
    expect(within(dispositionGroup(/Outside confirmed scope/)).getByText('fraud_ring_score'))
      .toBeInTheDocument()
    // The out-of-scope recipe never appears under the eligible heading.
    expect(within(dispositionGroup(/Recommended/)).queryByText('fraud_ring_score')).toBeNull()
  })

  it('flag ON, lens OFF: a scoped response renders no disposition lens', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    // intent_disposition_lens left off.
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scopedConsidered())
    await generateFlagOn()
    await userEvent.click(await screen.findByRole('button', { name: /confirm scope and generate/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText(/how your scope dispositioned/i)).toBeNull()
  })

  it('flag ON: broaden is chosen at step 1 and generated by the ONE CTA, unscoped', async () => {
    // Show-all used to generate on click, jumping past the target step the flow sequences —
    // "the next part should be target selection". It is a scope CHOICE now; the CTA generates.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([IDEA])))
    await generateFlagOn()
    await userEvent.click(
      await screen.findByRole('button', { name: /show all buildable recipes/i }))
    expect(contractConsideredSet).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /generate over everything/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ unscoped: true }),
      }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
  })

  it('flag ON + lens: broaden FROM the lens reuses the committed intentId (recognition cleared)', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    vi.stubEnv('VITE_INTENT_DISPOSITION_LENS', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    // Confirm → a scoped response (with dispositions) that CLEARS `recognition` and commits intentId
    // 'int_1'; then the lens's broaden re-runs. If broaden read only `rec?.intent_id` it would send
    // undefined here (rec is null) and orphan the run/scope lineage — it must send the committed intentId.
    contractConsideredSet
      .mockResolvedValueOnce(scopedConsidered())
      .mockResolvedValueOnce(considered(singleSetRound([IDEA])))
    await generateFlagOn()
    await userEvent.click(await screen.findByRole('button', { name: /confirm scope and generate/i }))
    // The disposition lens rendered; the proposed-scope panel is gone (recognition cleared).
    await screen.findByRole('heading', { name: /how your scope dispositioned/i })
    await userEvent.click(screen.getByRole('button', { name: /show all buildable recipes/i }))
    expect(contractConsideredSet).toHaveBeenLastCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        intentId: 'int_1',
        confirmedScope: expect.objectContaining({ unscoped: true }),
      }))
  })
})

// -------------------------------------------- Task 5: which of five things happened (repair seam)
//
// The 2026-08-15 incident put four different outcomes behind ONE sentence — "No use-case was
// recognised for this objective" — which was true of exactly one of them. One case per disposition,
// plus the standing honest-absence law asserted on the two that are NOT absence: a partial recovery
// and an ambiguous answer both HAVE proposals, and reporting either as absence is the defect.
describe('Gate #1 recognition quality', () => {
  const ABSENCE = /No use-case was recognised for this objective/

  function candidate(
    use_case_id: string, display_name: string, relationship: 'primary' | 'secondary',
  ): api.RecognitionCandidate {
    return {
      use_case_id, display_name, relationship, confidence: 'high',
      evidence_spans: ['about to leave'],
    }
  }

  // This suite is ABOUT the five dispositions, so unlike the shared factory its default is a
  // recorded `clean` — every test here either asserts that arm or overrides it explicitly.
  function recognition(over: Partial<api.RecognitionResp> = {}): api.RecognitionResp {
    return recognitionResp({
      candidates: [candidate('churn', 'Customer churn', 'primary')],
      recognition_quality: {
        disposition: 'clean', repair_attempts: 0, dropped_candidate_count: 0,
        drop_reason_codes: [],
      },
      ...over,
    })
  }

  function quality(over: Partial<api.RecognitionQuality>): api.RecognitionQuality {
    return {
      disposition: 'clean', repair_attempts: 0, dropped_candidate_count: 0, drop_reason_codes: [],
      ...over,
    }
  }

  async function render_(rec: api.RecognitionResp) {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(rec)
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    await screen.findByRole('heading', { name: /confirm the scope/i })
  }

  it('clean: the scope, and nothing about how it was reached', async () => {
    await render_(recognition())
    expect(screen.getByText('Customer churn')).toBeInTheDocument()
    expect(screen.queryByText(ABSENCE)).toBeNull()
    // Today's behaviour, unchanged: a first answer that validated has nothing to explain.
    expect(document.querySelector('[data-role="recognition-quality"]')).toBeNull()
    expect(screen.queryByText(/discarded/i)).toBeNull()
    expect(screen.queryByText(/did not validate/i)).toBeNull()
  })

  it('repaired: the answer is the CORRECTED one, and says so', async () => {
    await render_(recognition({
      recognition_quality: quality({ disposition: 'repaired', repair_attempts: 1 }),
    }))
    expect(screen.getByText(/The first answer did not validate; the model was asked to correct it/))
      .toBeInTheDocument()
    // It is not a failure and not a loss: the scope is right there to confirm.
    expect(screen.getByText('Customer churn')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirm scope and generate/i })).toBeInTheDocument()
    expect(screen.queryByText(/discarded/i)).toBeNull()
  })

  it('partially recovered: KEEPS its scope and names the loss', async () => {
    await render_(recognition({
      recognition_quality: quality({
        disposition: 'partially_recovered', repair_attempts: 2, dropped_candidate_count: 1,
        drop_reason_codes: ['MALFORMED_EVIDENCE_SPANS'],
      }),
    }))
    expect(screen.getByText('One invalid proposal was discarded; review the remaining scope.'))
      .toBeInTheDocument()
    // THE regression this task exists to stop: a partial recovery is not an offer to broaden.
    expect(screen.getByText('Customer churn')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirm scope and generate/i })).toBeInTheDocument()
    expect(screen.queryByText(ABSENCE)).toBeNull()
    expect(screen.queryByText(/could not be validated/i)).toBeNull()
  })

  it('unscoped: a valid answer that matched nothing', async () => {
    await render_(recognition({
      status: 'unscoped', unscoped: true, candidates: [],
      recognition_quality: quality({ disposition: 'unscoped' }),
    }))
    expect(screen.getByText(/No governed use case clearly matched/)).toBeInTheDocument()
    expect(screen.queryByText(/could not be validated/i)).toBeNull()
    expect(screen.getByRole('button', { name: /show all buildable recipes/i })).toBeInTheDocument()
  })

  it('technical failure: no usable answer at all, said as itself', async () => {
    await render_(recognition({
      status: 'technical_failure', unscoped: true, candidates: [],
      recognition_quality: quality({ disposition: 'technical_failure', repair_attempts: 2 }),
    }))
    expect(screen.getByText('Recognition could not be validated; you may broaden to all recipes.'))
      .toBeInTheDocument()
    // Distinct from "nothing matched" — the platform failed, the taxonomy did not answer.
    expect(screen.queryByText(/No governed use case clearly matched/)).toBeNull()
    expect(screen.getByRole('button', { name: /show all buildable recipes/i })).toBeInTheDocument()
  })

  it('alternatives without a primary are shown as alternatives, never as absence', async () => {
    // THE HONEST-ABSENCE LAW, on the first of the two dispositions that are not absence. The
    // alternatives were in the response all along; the old branch rendered the absence sentence and
    // dropped them on the floor, so the user could neither see nor choose them.
    await render_(recognition({
      status: 'ambiguous',
      candidates: [candidate('engagement', 'Engagement decline', 'secondary'),
                   candidate('deposit', 'Deposit attrition', 'secondary')],
      recognition_quality: quality({ disposition: 'clean' }),
    }))
    expect(screen.getByText(/Several alternatives were found; choose one as the primary/))
      .toBeInTheDocument()
    expect(screen.queryByText(ABSENCE)).toBeNull()
    expect(screen.getByRole('heading', { name: 'Alternatives' })).toBeInTheDocument()
    expect(screen.getByText('Engagement decline')).toBeInTheDocument()
    expect(screen.getByText('Deposit attrition')).toBeInTheDocument()
    // And choosing one is a click, which is what "choose or broaden" has to mean.
    await userEvent.click(screen.getAllByRole('button', { name: 'Make primary' })[0])
    expect(screen.getByRole('button', { name: /confirm scope and generate/i })).toBeInTheDocument()
  })

  it('the TARGET DEFERS while the scope is unsettled, and opens when one is picked', async () => {
    // The one real sequencing dependency: while the person is mid-decision on the scope, a second
    // open decision beside it is what "no idea where to click" was made of. With a settled
    // primary, step 1 is a one-line summary instead and the target is the only open decision —
    // scope-then-target without a forced click on the confident path.
    await render_(recognition({
      status: 'ambiguous',
      candidates: [candidate('engagement', 'Engagement decline', 'secondary'),
                   candidate('deposit', 'Deposit attrition', 'secondary')],
      recognition_quality: quality({ disposition: 'clean' }),
    }))
    expect(screen.getByText(/settle the scope above first/i)).toBeInTheDocument()
    expect(screen.queryByText(/your decision — pick one/i)).toBeNull()
    await userEvent.click(screen.getAllByRole('button', { name: 'Make primary' })[0])
    expect(screen.queryByText(/settle the scope above first/i)).toBeNull()
  })

  it('a partial recovery that lost its primary reports BOTH facts', async () => {
    // The second of the two non-absence dispositions, in its hardest shape: the discarded candidate
    // WAS the primary, so the status downgraded to ambiguous and no scope is designated. Neither
    // fact may hide the other, and neither is absence.
    await render_(recognition({
      status: 'ambiguous',
      candidates: [candidate('deposit', 'Deposit attrition', 'secondary')],
      recognition_quality: quality({
        disposition: 'partially_recovered', repair_attempts: 2, dropped_candidate_count: 1,
        drop_reason_codes: ['MALFORMED_EVIDENCE_SPANS'],
      }),
    }))
    expect(screen.getByText(/Several alternatives were found/)).toBeInTheDocument()
    expect(screen.getByText('One invalid proposal was discarded; review the remaining scope.'))
      .toBeInTheDocument()
    expect(screen.getByText('Deposit attrition')).toBeInTheDocument()
    expect(screen.queryByText(ABSENCE)).toBeNull()
  })

  it('more than one discarded proposal is counted, not pluralised away', async () => {
    await render_(recognition({
      recognition_quality: quality({
        disposition: 'partially_recovered', repair_attempts: 2, dropped_candidate_count: 2,
        drop_reason_codes: ['MALFORMED_EVIDENCE_SPANS'],
      }),
    }))
    expect(screen.getByText('2 invalid proposals were discarded; review the remaining scope.'))
      .toBeInTheDocument()
  })

  it('an attempt with no recorded quality says nothing about how it was reached', async () => {
    // Migration 1071 has no backfill and cannot have one (the table is append-only), so a legacy
    // row carries no quality. The client says what it did before rather than inventing a `clean`.
    await render_(recognition({ recognition_quality: null }))
    expect(screen.getByText('Customer churn')).toBeInTheDocument()
    expect(document.querySelector('[data-role="recognition-quality"]')).toBeNull()
    expect(screen.queryByText(ABSENCE)).toBeNull()
  })

  it('a recognition with no candidates at all still says nothing was recognised', async () => {
    // The one case the old sentence was TRUE of, kept: a classified-shaped response carrying
    // nothing. Absence is still reported as absence.
    await render_(recognition({ candidates: [], recognition_quality: null }))
    expect(screen.getByText(ABSENCE)).toBeInTheDocument()
  })
})

// ------------------------------------------------------ Phase 2A: deterministic ranking (VITE_INTENT_RANKING)
describe('Phase 2A ranking', () => {
  // Three ranked eligible recipes, deliberately supplied OUT of canonical order to prove the UI
  // orders by canonical_rank. Two are in the initial view (ranks 1, 2); rank 3 is held back with a
  // distinct initial_view reason. The two reason streams stay separate: rank_reasons vs
  // initial_view_reasons.
  const RANKING: api.RankedRecipe[] = [
    {
      recipe_id: 'balance_trend_30d', canonical_rank: 3, selected_for_initial_view: false,
      rank_reasons: ['low_binding_quality'],
      initial_view_reasons: ['duplicate_variant_not_in_initial_view'],
    },
    {
      recipe_id: 'recency_since_event', canonical_rank: 1, selected_for_initial_view: true,
      rank_reasons: ['primary_use_case_match', 'exact_binding'],
      initial_view_reasons: ['selected_initial_view'],
    },
    {
      recipe_id: 'balance_trend_90d', canonical_rank: 2, selected_for_initial_view: true,
      rank_reasons: ['supporting_match'],
      initial_view_reasons: ['selected_initial_view'],
    },
  ]

  // A scoped considered-set carrying the deterministic ranking (Task A3) alongside a single-set
  // alternatives list + the LLM recommendation. A single set keeps the multi-set advice panel out,
  // so the only "Recommended starting set" band on the page is the ranking panel's own.
  function rankedConsidered(): api.ConsideredSetResp {
    return {
      intent_id: 'int_1', anchor: null,
      alternatives: [{ lens: 'temporal', features: [IDEA] }],
      recommendation: {
        recommended_lens: 'temporal',
        reasoning: 'recency signals move earliest for a churn horizon',
        caveat: CAVEAT,
      },
      rejections: [],
      generation_run_id: 'run_1', scope_id: 'scope_1', in_scope_count: 3,
      ranking: RANKING, ranking_version: 'applicability@1',
    }
  }

  async function generateRanked() {
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
  }

  function rankingPanel(): HTMLElement {
    const panel = document.getElementById('wb-ranking')
    if (!panel) throw new Error('ranking panel not found')
    return panel
  }

  it('flag OFF: a response carrying ranking renders the pre-2A way (no rank panel or affordances)', async () => {
    // No env stub → VITE_INTENT_RANKING defaults off. The response DOES carry ranking, but the flag
    // gate means none of the 2A affordances render and the candidate list is unchanged.
    contractConsideredSet.mockResolvedValue(rankedConsidered())
    await generateRanked()
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /recipes by priority/i })).toBeNull()
    expect(screen.queryByText(/recommended starting set/i)).toBeNull()
    expect(screen.queryByText('Why here')).toBeNull()
    expect(screen.queryByText('recency_since_event')).toBeNull()
    expect(screen.queryByRole('button', { name: /show all .* recipes/i })).toBeNull()
  })

  it('flag ON: orders eligible recipes by canonical_rank, shows the initial view + a Show all expander', async () => {
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    contractConsideredSet.mockResolvedValue(rankedConsidered())
    await generateRanked()
    // The ranked panel renders (a distinct presentation from the candidate cards).
    expect(await screen.findByRole('heading', { name: /recipes by priority/i })).toBeInTheDocument()
    const panel = rankingPanel()
    // The initial-view subset (ranks 1, 2) shows first, in canonical order — even though the response
    // array was shuffled (rank 3 came first). The held-back rank-3 recipe is hidden until Show all.
    expect(within(panel).getByText('recency_since_event')).toBeInTheDocument()
    expect(within(panel).getByText('balance_trend_90d')).toBeInTheDocument()
    expect(within(panel).queryByText('balance_trend_30d')).toBeNull()
    const text = panel.textContent ?? ''
    expect(text.indexOf('recency_since_event')).toBeLessThan(text.indexOf('balance_trend_90d'))
    // A rank_reasons CODE renders as its frontend-mapped display text (never the raw enum token).
    expect(within(panel).getByText('Matches your primary use case')).toBeInTheDocument()
    expect(within(panel).queryByText('primary_use_case_match')).toBeNull()
    // The LLM "recommended starting set" is present AND visually separate from the ranked list: it
    // lives in its own labelled band, which holds no ranked recipe rows.
    const band = panel.querySelector('[data-band="recommended-starting-set"]') as HTMLElement
    expect(band).not.toBeNull()
    expect(within(band).getByText(/Recommended starting set: Temporal\./)).toBeInTheDocument()
    expect(within(band).queryByText('recency_since_event')).toBeNull()
    // Show all reveals the held-back recipe.
    await userEvent.click(screen.getByRole('button', { name: 'Show all 3 recipes' }))
    expect(within(panel).getByText('balance_trend_30d')).toBeInTheDocument()
  })

  it('flag ON: a non-initial recipe carries a distinct "why not shown initially" reason stream', async () => {
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    contractConsideredSet.mockResolvedValue(rankedConsidered())
    await generateRanked()
    await screen.findByRole('heading', { name: /recipes by priority/i })
    await userEvent.click(screen.getByRole('button', { name: 'Show all 3 recipes' }))
    const panel = rankingPanel()
    const row = within(panel).getByText('balance_trend_30d').closest('li') as HTMLElement
    // The "why not shown initially" stream maps initial_view_reasons → text, kept DISTINCT from the
    // "why here" (rank_reasons) stream — two separately-labelled disclosures on the same row.
    expect(within(row).getByText('Why not shown initially')).toBeInTheDocument()
    expect(within(row).getByText('A similar variant is already shown')).toBeInTheDocument()
    expect(within(row).getByText('Why here')).toBeInTheDocument()
    expect(within(row).getByText('Weaker column binding')).toBeInTheDocument()
    // The two streams never merge: the rank-reason text is not the initial-view reason text.
    expect(within(row).queryByText('duplicate_variant_not_in_initial_view')).toBeNull()
  })

  // A minimal classified recognition so the confirm flow (VITE_INTENT_CONFIRMATION_UI) can mint the
  // scoped considered-set that carries the per-recipe SOFT-dimension signal warnings.
  const REC: api.RecognitionResp = recognitionResp()

  it('flags ON: a per-recipe SOFT-dimension warning renders its mapped text on the ranked row', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    vi.stubEnv('VITE_INTENT_RANKING', '1')
    contractRecognitions.mockResolvedValue(REC)
    contractConsideredSet.mockResolvedValue({
      ...rankedConsidered(),
      signal_warnings: { balance_trend_30d: ['entity_grain_mismatch'] },
    })
    await generateRanked()
    await userEvent.click(await screen.findByRole('button', { name: /confirm scope and generate/i }))
    await screen.findByRole('heading', { name: /recipes by priority/i })
    // balance_trend_30d is held back — reveal it, then its warning code renders as mapped text.
    await userEvent.click(screen.getByRole('button', { name: 'Show all 3 recipes' }))
    const panel = rankingPanel()
    const row = within(panel).getByText('balance_trend_30d').closest('li') as HTMLElement
    expect(within(row).getByText('Built at a different grain — derivable by roll-up'))
      .toBeInTheDocument()
  })
})

// ------------------------------------------------- Intake build: the target confirm block ----
describe('Intake target confirmation', () => {
  const RECOGNITION: api.RecognitionResp = recognitionResp()

  // T7: the fixture is the COMMITTED case — `outcome_label` is the registry's own outcome-family
  // concept, so this target is the label itself: no abstention, no proxy list, no disclosure.
  const TICKET: api.IntakeTicket = {
    target_column: 'public.labels.churned', target_window_days: 90,
    target_type: 'binary_classification', business_domain: ['retail_churn'],
    confidence: 'high', pinned: false, contradiction: null,
    runners_up: ['public.labels.closed'],
    target_concept: 'outcome_label', target_leakage_class: 'outcome',
    target_is_proxy: false, proxy_candidates: [], outcome_candidates: [],
    window_source: 'stated', window_refusal: null,
  }

  const INTAKE: api.IntakeResp = {
    intent_id: 'int_1', reason: 'extracted', ticket: TICKET,
    target_detail: {
      ref: 'public.labels.churned', catalog_source: 'deposits',
      concept: 'outcome_label', ai_summary: 'Whether the customer churned in the window.',
    },
    runner_up_details: [{
      ref: 'public.labels.closed', catalog_source: 'deposits',
      concept: 'outcome_label', ai_summary: 'Whether the account was closed.',
    }],
    proxy_candidate_details: [],
    outcome_candidate_details: [],
  }

  const READING: api.IntakeReading = {
    intent_id: 'int_1', target_ref: 'public.labels.churned', target_window_days: 90,
    target_type: 'binary_classification', business_domain: ['retail_churn'],
    target_provenance: 'human_confirmed', target_confirmed_by: 'user:tester',
    target_concept: 'outcome_label', target_leakage_class: 'outcome', target_is_proxy: false,
  }

  function scoped(): api.ConsideredSetResp {
    return {
      intent_id: 'int_1', anchor: null, alternatives: [{ lens: 'temporal', features: [IDEA] }],
      recommendation: null, rejections: [],
      generation_run_id: 'run_1', scope_id: 'scope_1', in_scope_count: 1, dispositions: [],
    }
  }

  async function generateConfirmOn() {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scoped())
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
  }

  it('renders the draft reading and Yes signs it and threads it into generation', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    // the mandatory read ran alongside recognition, on the SAME hypothesis — AND the prediction
    // goal, which is where the horizon is written and which never used to reach this read.
    expect(contractIntake).toHaveBeenCalledWith(
      HYPOTHESIS, { catalogSource: 'deposits', objective: 'predict churn' })
    // the draft reading renders with the summary one-liner and the window
    expect(await screen.findByText(/I understood your target as/)).toBeInTheDocument()
    expect(screen.getByText('Whether the customer churned in the window.')).toBeInTheDocument()
    // T7 (b): the window now says WHERE it came from as well as what it is. This fixture's
    // window_source is 'stated', so the goal's own horizon is what the 90 is.
    expect(screen.getByText(/Label window: 90 days — the horizon your goal states/))
      .toBeInTheDocument()
    // a DRAFT is not a decision: nothing was recorded yet
    expect(contractIntakeTarget).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    // T7 (c): the acknowledgment is stated on EVERY call, and its default is false. This target's
    // concept is outcome-family, so the gate never fires and the false rides through untouched.
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'confirmed', {
      targetRef: 'public.labels.churned', targetWindowDays: 90,
      targetType: 'binary_classification', businessDomain: ['retail_churn'],
      catalogSource: 'deposits', targetNotOutcomeAcknowledged: false,
    })
    expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
    // the signed target threads into the considered-set request
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({ targetRef: 'public.labels.churned' }))
  })

  it('the label window is EDITABLE and the edited value is what gets signed', async () => {
    // It was the one signed field nobody could correct. That matters more than tidiness: the
    // window is what the near-label leakage critic runs on, so an uncorrectable wrong window (or
    // an uncorrectable ABSENT one) silently switches that check off.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    const box = screen.getByLabelText('Label window (days)')
    expect(box).toHaveValue(90)                       // seeded from the reading
    await userEvent.clear(box)
    await userEvent.type(box, '30')
    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'confirmed',
      expect.objectContaining({ targetWindowDays: 30 }))
  })

  it('a window the reading never produced can be supplied by hand', async () => {
    // The server's own contradiction refusal ends with "state the horizon on the confirm screen".
    // There was no control to state it with, so that instruction was a dead end.
    contractIntake.mockResolvedValue({
      ...INTAKE, ticket: { ...TICKET, target_window_days: null, window_source: 'unstated' },
    })
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    const box = screen.getByLabelText('Label window (days)')
    expect(box).toHaveValue(null)
    await userEvent.type(box, '45')
    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'confirmed',
      expect.objectContaining({ targetWindowDays: 45 }))
  })

  it('no modelling-context control is offered, and none is sent as confirmed', async () => {
    // The control was removed because nothing consumes it in this deployment (the ranking flag is
    // off, and ranking is its only consumer). The SEEDING had to go with it: leaving the
    // recogniser's proposal to ride through an absent control would have recorded an LLM guess as
    // a human-confirmed dimension, which is the one thing the scope record must never say.
    contractIntake.mockResolvedValue(INTAKE)
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    expect(screen.queryByLabelText('Add modelling context')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ modellingContexts: [] }),
      }))
  })

  it('says when a reading came from cache rather than fresh analysis', async () => {
    // "Same hypothesis, answer in seconds" is the cache working — but the screen said nothing, so
    // it was indistinguishable from a backend doing nothing.
    contractIntake.mockResolvedValue({ ...INTAKE, reason: 'replayed' })
    await generateConfirmOn()
    expect(await screen.findByText(/Cached answer from an identical question/))
      .toBeInTheDocument()
  })

  it('says plainly when the MODEL never ran, instead of passing pattern-matching off as a reading',
     async () => {
    // The load-bearing one. `unavailable` means the model could not be reached, so the ticket is
    // the plain-code name match plus honest abstains. On screen that was indistinguishable from a
    // model that HAD read the hypothesis and declined to commit — two completely different facts,
    // and presenting the second as the first is the confidence-it-does-not-have failure this
    // platform exists to refuse.
    contractIntake.mockResolvedValue({ ...INTAKE, reason: 'unavailable' })
    await generateConfirmOn()
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument()
    expect(screen.getByText(/pattern matching/)).toBeInTheDocument()
  })

  it('says nothing extra when the reading is a fresh model call', async () => {
    contractIntake.mockResolvedValue(INTAKE)   // reason: 'extracted'
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    expect(screen.queryByText(/Cached answer/)).toBeNull()
    expect(screen.queryByText(/could not be reached/)).toBeNull()
  })

  it('the signed block keeps showing the label window it was signed with', async () => {
    // The window is part of what the person SIGNED, and it is the input the near-label leakage
    // critic runs on — a critic with no window abstains on every candidate. It was rendered on the
    // draft and then dropped from the signed block, so the one fact that says whether that check
    // can run at all disappeared at the moment it started mattering.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
    expect(screen.getByText(/Label window: 90 days/)).toBeInTheDocument()
  })

  it('the signed block says plainly when no window was recorded', async () => {
    // Absence is stated with its CONSEQUENCE, not left blank: "no window" is why a leakage check
    // the user may be relying on will not run.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue({
      ...READING, target_window_days: null,
    })
    await generateConfirmOn()
    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
    expect(screen.getByText(/No label window recorded/)).toBeInTheDocument()
  })

  it('surfaces a name-vs-prose contradiction as a warning', async () => {
    contractIntake.mockResolvedValue({
      ...INTAKE,
      ticket: {
        ...TICKET,
        contradiction: 'you named cust_status_flg; the description reads as cust_susp_flg',
      },
    })
    await generateConfirmOn()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /you named cust_status_flg; the description reads as cust_susp_flg/)
  })

  it('Change it opens the correction input and signs the typed ref as corrected', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue({
      ...READING, target_ref: 'public.labels.closed',
    })
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    await userEvent.click(screen.getByRole('button', { name: /pick a different column/i }))
    await userEvent.type(screen.getByLabelText('Correct target'), 'public.labels.closed')
    await userEvent.click(screen.getByRole('button', { name: /sign this target/i }))
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'corrected',
      expect.objectContaining({ targetRef: 'public.labels.closed' }))
    expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
  })

  it('a runner-up is a one-click correction, never a restart', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue({
      ...READING, target_ref: 'public.labels.closed',
    })
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    await userEvent.click(screen.getByRole('button', { name: /pick a different column/i }))
    // the ranked runner-up renders with its one-liner and signs in ONE click
    await userEvent.click(screen.getByRole('button',
      { name: /public\.labels\.closed — Whether the account was closed\./ }))
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'corrected',
      expect.objectContaining({ targetRef: 'public.labels.closed' }))
    expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
  })

it('the CLICKED BUTTON shows the working state, not only a banner elsewhere', async () => {
    // The progress callout renders up by the gates strip — which can be scrolled out of view from
    // the button at the bottom of the form. The person watches the control under their cursor,
    // and "Generating" without motion or ellipsis there is what read as stuck.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    let release!: (r: api.RecognitionResp) => void
    contractRecognitions.mockReturnValue(new Promise(res => { release = res }))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    const busy = await screen.findByRole('button', { name: /generating…/i })
    expect(busy).toBeDisabled()
    // and the callout ALSO sits beside the form's buttons, not only at the page top
    expect(screen.getAllByRole('status', { name: /engine progress/i }).length)
      .toBeGreaterThanOrEqual(2)
    release(RECOGNITION)
  })


  it('a REWRITTEN brief clears the previous run\'s target banner', async () => {
    // The builder belongs to the round. Left alone, changing the hypothesis kept a stale
    // "this run predicts tgt_x" — a false claim about the new run.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    targetForIntent.mockResolvedValueOnce(
      { intent_id: 'int_1', definition_id: 'd1', name: 'tgt_npe_90d', reads_as: 'one row per…' })
    await generateConfirmOn()
    expect(await screen.findByText(/this run predicts/i)).toBeInTheDocument()
    // rewrite: same flow, but the server now reports NO attached label for the new intent
    targetForIntent.mockResolvedValue(null)
    await userEvent.click(screen.getByRole('button', { name: /revise brief/i }))
    await userEvent.type(screen.getByLabelText('Hypothesis'), ' now about something else')
    await userEvent.click(screen.getByRole('button', { name: /generate revised round/i }))
    await screen.findByText(/I understood your target as/)
    expect(screen.queryByText(/this run predicts/i)).toBeNull()
  })

  it('an intent that ALREADY carries a label shows it — server truth, not client memory', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    targetForIntent.mockResolvedValue(
      { intent_id: 'int_1', definition_id: 'd1', name: 'tgt_npe_90d', reads_as: 'one row per…' })
    await generateConfirmOn()
    expect(await screen.findByText(/this run predicts/i)).toBeInTheDocument()
    expect(screen.getByText('tgt_npe_90d')).toBeInTheDocument()
  })

  it('registering a label from scope review KEEPS the scope review', async () => {
    // Found by the end-to-end pass, not the unit tests: the attach handler called
    // invalidateGenerated unconditionally, and that helper clears the RECOGNITION — so
    // registering a label made the entire scope review vanish back to the draft shell.
    // Invalidation is for rounds; at scope review there is nothing to invalidate.
    contractIntake.mockResolvedValue({
      ...INTAKE, ticket: { ...TICKET, target_column: null }, target_detail: null,
    })
    contractIntakeTarget.mockResolvedValue({ ...READING, target_ref: null })
    attachTargetToIntent.mockResolvedValue(
      { intent_id: 'int_1', definition_id: 'd9', name: 'tgt_e2e_60d', reads_as: 'one row per…' })
    await generateConfirmOn()
    await userEvent.click(
      await screen.findByRole('button', { name: /build the target instead/i }))
    await screen.findByRole('heading', { name: /build a prediction target/i })
    // shortcut the form: adopt-an-existing exercises the same attach path
    proposeTarget.mockResolvedValue({
      existing: [{ name: 'tgt_e2e_60d', description: 'd', window_days: 60, match_terms: ['x'] }],
      draft: null,
    })
    await userEvent.click(screen.getByRole('button', { name: /propose a target/i }))
    await userEvent.click(await screen.findByRole('button', { name: /use this label/i }))

    expect(await screen.findByText(/this run predicts/i)).toBeInTheDocument()
    // the scope review SURVIVES — step 1 summary still on screen, CTA still offered
    expect(screen.getByText('Customer churn')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirm scope and generate/i }))
      .toBeInTheDocument()
  })

  it('Confirm REFUSES to destroy a target build in progress', async () => {
    contractIntake.mockResolvedValue({
      ...INTAKE, ticket: { ...TICKET, target_column: null }, target_detail: null,
    })
    contractIntakeTarget.mockResolvedValue({ ...READING, target_ref: null })
    await generateConfirmOn()
    await userEvent.click(
      await screen.findByRole('button', { name: /build the target instead/i }))
    await screen.findByRole('heading', { name: /build a prediction target/i })
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    // nothing generated, and the reason is stated — the person's propose outranks our button
    expect(contractConsideredSet).not.toHaveBeenCalled()
    expect(screen.getByText(/finish or cancel the target builder first/i)).toBeInTheDocument()
  })

  it('says leakage checks are OFF while no target decision exists', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    expect(screen.getByText(/no target decision yet/i)).toBeInTheDocument()
    // deciding removes the nudge
    await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
    await waitFor(() => expect(screen.queryByText(/no target decision yet/i)).toBeNull())
  })

  it('Show-all is a SCOPE CHOICE: the target step still gates, ONE CTA generates', async () => {
    // The owner's review, verbatim: "why directly features are generated when I click it, as the
    // next part should be target selection." Show-all now answers step 1 — scope: everything
    // buildable — and generation stays behind the same single CTA every other path uses. The
    // double-click guard rides the same CTA (broadenScope's re-entry check).
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    let release!: (r: api.ConsideredSetResp) => void
    await generateConfirmOn()
    await userEvent.click(await screen.findByRole('button', { name: /change the scope/i }))
    contractConsideredSet.mockReturnValue(new Promise(res => { release = res }))
    const showAll = screen.getAllByRole('button', { name: /show all buildable recipes/i })[0]
    await userEvent.click(showAll)
    // NOTHING generated: the choice collapsed into the scope summary, target step intact.
    expect(contractConsideredSet).not.toHaveBeenCalled()
    expect(screen.getByText(/everything buildable/i)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Prediction target' })).toBeInTheDocument()
    const cta = screen.getByRole('button', { name: /generate over everything/i })
    await userEvent.click(cta)
    await userEvent.click(cta)
    expect(contractConsideredSet).toHaveBeenCalledTimes(1)
    expect(contractConsideredSet).toHaveBeenCalledWith(HYPOTHESIS, 'predict churn',
      expect.objectContaining({
        confirmedScope: expect.objectContaining({ unscoped: true }),
      }))
    release(scoped())
  })

  it('DISAGREEING with the scope has somewhere to go', async () => {
    // "If someone disagrees with the scope, what's the option here?" — the review question this
    // block answers. Expanding the settled scope must name every remedy: swap to an alternative
    // (when one exists), generate over everything, or rewrite the brief.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    await generateConfirmOn()
    await userEvent.click(await screen.findByRole('button', { name: /change the scope/i }))
    const block = screen.getByText(/don't agree with this scope\?/i).closest('div')!
    expect(within(block).getByRole('button', { name: /show all buildable recipes/i }))
      .toBeInTheDocument()
    expect(within(block).getByRole('button', { name: /rewrite the brief/i }))
      .toBeInTheDocument()
    // and the rewrite path actually opens the brief for editing
    await userEvent.click(within(block).getByRole('button', { name: /rewrite the brief/i }))
    expect(await screen.findByLabelText('Hypothesis')).toBeInTheDocument()
  })

  it('intake WAITS for recognitions, so one run cannot mint two intents', async () => {
    // Fired in parallel, both routes get-or-create the intent by hypothesis and both inserted —
    // two intents 59 microseconds apart in the live database, splitting one run's lineage. The
    // recognition creates the intent; the intake, sequenced after it, finds it.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    let releaseRec!: (r: api.RecognitionResp) => void
    contractRecognitions.mockReturnValue(new Promise(res => { releaseRec = res }))
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    contractConsideredSet.mockResolvedValue(scoped())
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    // recognition still pending -> intake must not have fired
    expect(contractIntake).not.toHaveBeenCalled()
    releaseRec(RECOGNITION)
    await screen.findByText(/I understood your target as/)
    expect(contractIntake).toHaveBeenCalledTimes(1)
  })

  it('an IN-FLIGHT target read says it is reading, never that nothing landed', async () => {
    // `intake === null` used to mean both "still running" and "failed", so during the wait the
    // screen asserted "nothing read your objective" — false, and it then swapped to the real
    // reading. In flight and failed are different states with different sentences.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractIntake.mockReturnValue(new Promise(() => {}))   // never lands
    contractConsideredSet.mockResolvedValue(scoped())
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    expect(await screen.findByText(/reading your objective for the prediction target/i))
      .toBeInTheDocument()
    expect(screen.queryByText(/nothing read your objective/i)).toBeNull()
  })

  it('says what the engine is DOING while it runs, not just that it is running', async () => {
    // "Generating" alone reads as stuck: the first step is a model call over the whole catalog and
    // takes tens of seconds, with nothing on screen to say so. A person testing it reloads.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    let release!: (r: api.RecognitionResp) => void
    contractRecognitions.mockReturnValue(new Promise(res => { release = res }))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    const [status] = await screen.findAllByRole('status', { name: /engine progress/i })
    expect(status).toHaveTextContent(/reading your objective/i)
    expect(status).toHaveTextContent(/seconds/i)
    release(RECOGNITION)
  })

  it('names the SECOND wait differently from the first', async () => {
    // Recognition and planning are two model calls with two different subjects. One label for both
    // would say "still going" where it could say which half is running.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    let release!: (r: api.ConsideredSetResp) => void
    contractConsideredSet.mockReturnValue(new Promise(res => { release = res }))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    await userEvent.click(
      await screen.findByRole('button', { name: /confirm scope and generate/i }))

    const [status] = await screen.findAllByRole('status', { name: /engine progress/i })
    expect(status).toHaveTextContent(/planning candidates/i)
    release(considered(singleSetRound([IDEA])))
  })

  it('clears the progress once the round lands', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: /engine progress/i })).toBeNull()
  })

  it('offers to BUILD a target when the objective names no column', async () => {
    // The dead end this closes. A label like "went non-performing within 90 days" is a rule over
    // history, not a column anyone ingested — so an objective whose outcome has to be CONSTRUCTED
    // could previously only be answered with "just exploring", which is a different question.
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scoped())
    contractIntake.mockResolvedValue({
      ...INTAKE, ticket: { ...TICKET, target_column: null }, target_detail: null,
    })
    contractIntakeTarget.mockResolvedValue({ ...READING, target_ref: null })
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    await userEvent.click(
      await screen.findByRole('button', { name: /build the target instead/i }))
    // IN PLACE: the form opens here, on the run being configured. Sending the person to another
    // screen would make them state the same objective twice and leave the run behind them.
    expect(await screen.findByRole('heading', { name: /build a prediction target/i }))
      .toBeInTheDocument()
    expect(screen.getByLabelText(/what are you trying to predict/i)).toHaveValue(HYPOTHESIS)
    // ...and the target decision it opened from is still on screen around it. (The brief itself
    // is collapsed behind "Revise brief" at this phase, which is why it is not asserted here.)
    expect(screen.getByText(/no target detected in your objective/i)).toBeInTheDocument()
  })

  it('offers to build one even when a column WAS read, without displacing it', async () => {
    // A column that merely records the outcome is often not the label you want. The read column
    // stays the primary answer; this is the escape hatch beside it.
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue(READING)
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(RECOGNITION)
    contractConsideredSet.mockResolvedValue(scoped())
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))

    await screen.findByText(/I understood your target as/)
    expect(screen.getByRole('button', { name: /yes, that's my target/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /build the target instead/i })).toBeInTheDocument()
  })

  it('exploring mode states the honest asymmetry on LLM-origin cards', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue({
      ...READING, target_ref: null, target_provenance: 'exploring',
    })
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    await userEvent.click(screen.getByRole('button', { name: /no target — just exploring/i }))
    await screen.findByText(/leakage checks are off/i)
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    // the generated LLM-origin card carries the banner — presentation only, never a removal
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText(/No target declared — leakage unchecked/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeInTheDocument()
  })

  it('exploring is a recorded declaration, not a failure', async () => {
    contractIntake.mockResolvedValue(INTAKE)
    contractIntakeTarget.mockResolvedValue({
      ...READING, target_ref: null, target_provenance: 'exploring',
    })
    await generateConfirmOn()
    await screen.findByText(/I understood your target as/)
    await userEvent.click(screen.getByRole('button', { name: /no target — just exploring/i }))
    expect(contractIntakeTarget).toHaveBeenCalledWith('int_1', 'exploring',
      expect.objectContaining({ targetRef: undefined }))
    expect(await screen.findByText(/leakage checks are off/i)).toBeInTheDocument()
  })

  it('a pinned name is shown as a reading and still asks for a click', async () => {
    // The prose door is closed in every class: a bare word match cannot tell a deliberate
    // reference from an English word that equals a column name, so nothing is recorded server-side
    // and the confirm gate is the one door. The match is still disclosed on the ticket.
    contractIntake.mockResolvedValue({
      ...INTAKE,
      ticket: { ...TICKET, pinned: true },
    })
    await generateConfirmOn()
    expect(await screen.findByText(/I understood your target as/)).toBeInTheDocument()
    expect(screen.queryByText(/you named it/)).toBeNull()
    expect(screen.getByRole('button', { name: /yes, that's my target/i })).toBeInTheDocument()
    expect(contractIntakeTarget).not.toHaveBeenCalled()
    // The pin used to thread into a manual target field on the brief. That field is gone — the
    // brief no longer asserts the target is a column — so the pin is what it always was on its
    // own terms: a READING the ticket reports, which the confirm gate below still asks about.
    await reviseBrief()
    expect(screen.queryByLabelText('Target column')).toBeNull()
  })

  it('an intake failure ASKS for the target instead of rendering nothing', async () => {
    // beforeEach default: contractIntake rejects — the degraded path.
    //
    // This used to assert that nothing rendered, because the intake form carried a "Target column"
    // field that stood in for the reading. That field asserted the thing being predicted is a
    // COLUMN before anything had read the objective, and a great many labels are not — so it was
    // removed. Deleting it outright would have deleted the degrade path with it, so the control
    // moved HERE, to the only case that needs it, where it can say why it is asking.
    await generateConfirmOn()
    expect(await screen.findByText('Customer churn')).toBeInTheDocument()
    expect(screen.queryByText(/I understood your target as/)).toBeNull()
    expect(screen.getByText(/nothing read your objective for a target/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Target column')).toBeInTheDocument()
    // generation is still unimpeded — the block asks, it does not gate
    await userEvent.click(screen.getByRole('button', { name: /confirm scope and generate/i }))
    expect(contractConsideredSet).toHaveBeenCalled()
  })

  it('the intake form no longer asserts the target is a COLUMN up front', async () => {
    render(<WorkbenchScreen />)
    expect(screen.getByLabelText('Hypothesis')).toBeInTheDocument()
    expect(screen.getByLabelText('Catalog source')).toBeInTheDocument()
    expect(screen.queryByLabelText('Target column')).toBeNull()
  })

  // ── T7 (c): THE NON-OUTCOME ACKNOWLEDGMENT ────────────────────────────────────────────────────
  //
  // Every sentence below is COPIED, byte for byte, from `_not_outcome_refusal` in
  // src/featuregen/api/routes/contract.py. That is the point of these three pins: the backend's own
  // pins (mutation V) hold the wording where it is written, and these hold the screen to rendering
  // it VERBATIM. Should the two ever drift, one side or the other goes red — which is exactly the
  // property a single shared banner could not have had.
  //
  // Three tiers, three DIFFERENT claims — and the difference is the defect the backend just
  // removed. `near_label` earns the word PROXY because the registry asserts label-adjacency;
  // `standard` is the OPPOSITE claim (the registry looked and declassified); an unregistered
  // concept asserts nothing in either direction. One banner for all three would have re-created
  // NB-1 in the UI, telling 338 of the registry's 359 concepts they were proxies.
  describe('the non-outcome acknowledgment', () => {
    const NEAR_LABEL_REFUSAL =
      "public.aml.cust_susp_flg carries the concept 'restriction_status', which the registry "
      + 'marks near_label: a funnel-tail signal that BORDERS the label. Confirming it means '
      + 'predicting a PROXY for the outcome, and a model trained on it can read its own answer '
      + 'back. Re-send with target_not_outcome_acknowledged: true to record that you know.'
    const STANDARD_REFUSAL =
      "public.aml.cust_susp_flg carries the concept 'account_status', which the registry "
      + 'classifies as standard: it does not certify this column as an outcome label, and nothing '
      + 'here asserts it correlates with one. Re-send with target_not_outcome_acknowledged: true '
      + 'to record that you know.'
    const UNREGISTERED_REFUSAL =
      'public.aml.cust_susp_flg carries no registered concept, so nothing certifies it as an '
      + 'outcome label — and absence is not an assertion the other way either. Re-send with '
      + 'target_not_outcome_acknowledged: true to record that you know.'

    function banner() {
      return document.querySelector('[data-role="intake-not-outcome"]') as HTMLElement
    }

    async function refuseThenRead(detail: string) {
      contractIntake.mockResolvedValue(INTAKE)
      contractIntakeTarget.mockRejectedValueOnce(new api.ApiError(422, detail))
      await generateConfirmOn()
      await screen.findByText(/I understood your target as/)
      await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
      return await screen.findByText(detail)
    }

    it('renders the near_label tier’s sentence verbatim — the PROXY claim the registry earns',
      async () => {
        await refuseThenRead(NEAR_LABEL_REFUSAL)
        expect(banner()).toHaveTextContent(NEAR_LABEL_REFUSAL)
      })

    it('renders the standard tier’s sentence verbatim — and it claims NO proxy', async () => {
      await refuseThenRead(STANDARD_REFUSAL)
      expect(banner()).toHaveTextContent(STANDARD_REFUSAL)
      // The tier-flattening check: the standard sentence must not pick up the near_label one's
      // word. This is the assertion a single shared banner could not pass.
      expect(banner()).not.toHaveTextContent(/PROXY/)
      expect(banner()).not.toHaveTextContent(/BORDERS the label/)
    })

    it('renders the unregistered tier’s sentence verbatim — silence, in both directions',
      async () => {
        await refuseThenRead(UNREGISTERED_REFUSAL)
        expect(banner()).toHaveTextContent(UNREGISTERED_REFUSAL)
        expect(banner()).not.toHaveTextContent(/PROXY/)
      })

    it('the first attempt never acknowledges; the control re-sends the SAME decision and ref',
      async () => {
        contractIntake.mockResolvedValue(INTAKE)
        contractIntakeTarget.mockRejectedValueOnce(new api.ApiError(422, NEAR_LABEL_REFUSAL))
        contractIntakeTarget.mockResolvedValueOnce({
          ...READING, target_concept: 'restriction_status',
          target_leakage_class: 'near_label', target_is_proxy: true,
        })
        await generateConfirmOn()
        await screen.findByText(/I understood your target as/)
        await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
        // THE ACKNOWLEDGMENT IS THE PERSON'S. The attempt that earns the sentence must not carry
        // it — sending it by default is the undisclosed commit this gate exists to stop.
        expect(contractIntakeTarget).toHaveBeenNthCalledWith(1, 'int_1', 'confirmed',
          expect.objectContaining({ targetNotOutcomeAcknowledged: false }))
        await screen.findByText(NEAR_LABEL_REFUSAL)

        await userEvent.click(
          screen.getByRole('button', { name: /I understand — record this target anyway/i }))
        expect(contractIntakeTarget).toHaveBeenNthCalledWith(2, 'int_1', 'confirmed',
          expect.objectContaining({
            targetRef: 'public.labels.churned', targetNotOutcomeAcknowledged: true,
          }))
        expect(await screen.findByText(/recorded as your decision/)).toBeInTheDocument()
        // The SERVER's echoed class, and the sentence the person actually read — not a
        // reconstruction of it from the class afterwards.
        expect(screen.getByText(/registry class:/)).toHaveTextContent('near_label')
        expect(screen.getByText(/You acknowledged:/)).toHaveTextContent(NEAR_LABEL_REFUSAL)
      })

    it('a 422 this screen cannot identify stays an error, never an offer to acknowledge',
      async () => {
        // The vocabulary refusal from the same route. Turning any 422 into an acknowledge button
        // would make the control an answer to questions it was never asked.
        const other = "business_domain outside the use-case vocabulary: ['not_a_use_case']"
        contractIntake.mockResolvedValue(INTAKE)
        contractIntakeTarget.mockRejectedValueOnce(new api.ApiError(422, other))
        await generateConfirmOn()
        await screen.findByText(/I understood your target as/)
        await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
        expect(await screen.findByText(other)).toBeInTheDocument()
        expect(banner()).toBeNull()
        expect(screen.queryByRole('button', { name: /record this target anyway/i })).toBeNull()
      })

    // THE BOUNDARY. The discriminator matches the refusal's whole closing INSTRUCTION, not the
    // bare field name — because the field name also appears in bodies that are not this refusal.
    // The realizable one is FastAPI's own type failure, caught before the handler runs: its native
    // list-`detail` reaches this component already flattened by the transport (api.ts renders
    // `[{loc, msg}]` as "loc.joined: msg"), and it names the field without ever having asked for
    // an acknowledgment. Offering the control there would re-send a body FastAPI already refused
    // to parse — and would put the word "acknowledge" in front of a person who was never told
    // anything to acknowledge.
    it('a 422 that merely MENTIONS the field is an error, not an acknowledge offer', async () => {
      const typeFailure =
        'body.target_not_outcome_acknowledged: Input should be a valid boolean'
      contractIntake.mockResolvedValue(INTAKE)
      contractIntakeTarget.mockRejectedValueOnce(new api.ApiError(422, typeFailure))
      await generateConfirmOn()
      await screen.findByText(/I understood your target as/)
      await userEvent.click(screen.getByRole('button', { name: /yes, that's my target/i }))
      expect(await screen.findByText(typeFailure)).toBeInTheDocument()
      expect(banner()).toBeNull()
      expect(screen.queryByRole('button', { name: /record this target anyway/i })).toBeNull()
    })
  })

  // ── T7 (a)/(b): the ticket's own facts, which nobody was shown on the AML run ─────────────────
  describe('what the ticket says about the target', () => {
    it('a contradicted window renders the server’s refusal, naming both numbers', async () => {
      // Copied from WindowRefusalV1's `detail` in overlay/upload/contract/intake_ticket.py.
      const detail = 'the objective states a horizon of 90 days; the intake reading returned '
        + 'target_window_days=0. The two disagree, so no label window is accepted — state the '
        + 'horizon on the confirm screen.'
      contractIntake.mockResolvedValue({
        ...INTAKE,
        ticket: {
          ...TICKET, target_window_days: null, window_source: 'contradicted',
          window_refusal: {
            code: 'WINDOW_CONTRADICTS_GOAL', stated_text: '90 days', stated_days: 90,
            ticket_days: 0, detail,
          },
        },
      })
      await generateConfirmOn()
      expect(await screen.findByText(detail)).toBeInTheDocument()
    })

    it('a stated horizon with no day count says so, and invents no number', async () => {
      // The degraded month-horizon shape: `_degraded` reads the goal with pure code, so a month
      // horizon lands as source='stated' with days=null. 28, 29, 30 and 31 are all months —
      // converting one would manufacture the false precision T7 exists to remove.
      contractIntake.mockResolvedValue({
        ...INTAKE,
        ticket: { ...TICKET, target_window_days: null, window_source: 'stated' },
      })
      await generateConfirmOn()
      const line = await screen.findByText(/Label window:/)
      expect(line).toHaveTextContent('your goal states a horizon, but not one that can be counted')
      expect(line.textContent).not.toMatch(/\d/)
    })

    it('an unstated horizon is honest absence, not a default', async () => {
      contractIntake.mockResolvedValue({
        ...INTAKE,
        ticket: { ...TICKET, target_window_days: null, window_source: 'unstated' },
      })
      await generateConfirmOn()
      expect(await screen.findByText(/no horizon stated, and none was read/)).toBeInTheDocument()
    })

    it('an abstention is an ANSWER: the true labels the catalog holds, and the nearest proxies',
      async () => {
        contractIntake.mockResolvedValue({
          ...INTAKE,
          ticket: {
            ...TICKET, target_column: null, confidence: 'abstain',
            target_concept: '', target_leakage_class: null, target_is_proxy: false,
            proxy_candidates: [
              { ref: 'public.aml.cust_susp_flg', concept: 'restriction_status',
                leakage_class: 'near_label' },
              { ref: 'public.aml.acct_status', concept: 'account_status',
                leakage_class: 'standard' },
            ],
            outcome_candidates: [
              { ref: 'public.aml.sar_filed', concept: 'outcome_label', leakage_class: 'outcome' },
            ],
          },
          target_detail: null,
          proxy_candidate_details: [
            { ref: 'public.aml.cust_susp_flg', catalog_source: 'cib',
              concept: 'restriction_status', ai_summary: 'Whether the customer is restricted.' },
            { ref: 'public.aml.acct_status', catalog_source: 'cib',
              concept: 'account_status', ai_summary: 'The account lifecycle state.' },
          ],
          outcome_candidate_details: [
            { ref: 'public.aml.sar_filed', catalog_source: 'cib', concept: 'outcome_label',
              ai_summary: 'Whether a SAR was filed for this customer.' },
          ],
        })
        await generateConfirmOn()
        // THE LABEL THE MODEL DID NOT PICK — named, because the catalog holds it.
        expect(await screen.findByText(/The catalog holds a true label:/)).toBeInTheDocument()
        expect(screen.getByText('public.aml.sar_filed')).toBeInTheDocument()
        // …and each proxy carries the registry's OWN class beside it, not one shared word.
        const proxies = document.querySelector('[data-role="proxy-candidates"]') as HTMLElement
        expect(within(proxies).getByText('near_label')).toBeInTheDocument()
        expect(within(proxies).getByText('standard')).toBeInTheDocument()
      })

    it('a pinned NON-outcome column no longer claims to be recorded', async () => {
      // ▲ NB-2: the server's pin door is outcome-family only now, so "you named it, already
      // recorded" would be false here — the confirm gate is the one door, and it asks out loud.
      contractIntake.mockResolvedValue({
        ...INTAKE,
        ticket: {
          ...TICKET, pinned: true, target_concept: 'restriction_status',
          target_leakage_class: 'near_label', target_is_proxy: true, confidence: 'abstain',
        },
      })
      await generateConfirmOn()
      await screen.findByText(/I understood your target as/)
      expect(screen.queryByText(/you named it/)).toBeNull()
      expect(screen.getByRole('button', { name: /yes, that's my target/i })).toBeInTheDocument()
      // and the registry's own reading of the column is on screen before anyone signs it
      expect(screen.getByText(/borders the outcome label/)).toBeInTheDocument()
    })
  })
})

// ---------------------------------------------- Task 3: the near-label chip (flag-only) ----
describe('near-label verdict chip', () => {
  it('too_close renders the warning chip and its rationale — and nothing is removed', async () => {
    await renderAndGenerate([
      { ...IDEA, near_label_verdict: 'too_close', near_label_rationale: '≈ the 90-day label' },
      OTHER_IDEA,
    ])
    expect(await screen.findByText('⚠ near label')).toBeInTheDocument()
    expect(screen.getByText(/Near-label check: ≈ the 90-day label/)).toBeInTheDocument()
    // flag-only: the flagged candidate is still on the list, selectable like any other
    expect(screen.getByRole('checkbox', { name: 'Select avg_balance' })).toBeInTheDocument()
  })

  it('no_finding is NOT a clearance — no chip renders for it or for abstain', async () => {
    await renderAndGenerate([
      { ...IDEA, near_label_verdict: 'no_finding', near_label_rationale: 'ordinary predictor' },
      { ...OTHER_IDEA, near_label_verdict: 'abstain', near_label_rationale: 'cannot tell' },
    ])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByText('⚠ near label')).toBeNull()
    expect(screen.queryByText(/Near-label check/)).toBeNull()
  })
})

// ------------------------------------------- Task 4b: untaken parameterisations on the card ----
describe('parameter alternatives line', () => {
  it('renders the untaken parameterisations when the server sends them', async () => {
    await renderAndGenerate([{ ...IDEA, param_alternatives: 'window: 30/[90]/180' }, OTHER_IDEA])
    expect(await screen.findByText('Also available — window: 30/[90]/180')).toBeInTheDocument()
    // absent field (flag off / nothing to choose) renders nothing
    expect(screen.getAllByText(/Also available/)).toHaveLength(1)
  })
})


// ── A4: the server decides selectability — the client renders, never re-implements ─────────────

describe('A4: actions-driven selection', () => {
  function withActions(
    cs: api.ConsideredSetResp,
    entries: Partial<api.OptionActionsEntry>[],
  ): api.ConsideredSetResp {
    return {
      ...cs,
      contract_version: 2,
      recommended_options: entries.map(e => ({
        option_id: e.option_id ?? 'opt_x',
        name: e.name ?? null,
        recipe_id: e.recipe_id ?? 'recipe:x',
        binding_state: e.binding_state ?? 'bound',
        allowed_actions: e.allowed_actions ?? ['save_idea'],
        blocked_actions: e.blocked_actions ?? {},
      })),
    }
  }

  it('disables selection when the server blocks create_contract and names the next step', async () => {
    const round = singleSetRound([idea('needs_confirmation')])
    const cs = considered(round)
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce(withActions(cs, [{
      option_id: optionId,
      allowed_actions: ['save_idea'],
      blocked_actions: { create_contract: [{
        code: 'PROPOSED_METADATA_ONLY',
        next_step: 'confirm the AI-proposed concept(s) in the Governance screen',
      }] },
    }]))
    await renderAndGenerateRaw()
    const checkbox = await screen.findByRole('checkbox', { name: /Select needs_confirmation/ })
    expect(checkbox).toBeDisabled()
    expect(checkbox).toHaveAttribute(
      'title', expect.stringContaining('confirm the AI-proposed concept'))
  })

  it('keeps selection enabled when the server allows create_contract', async () => {
    const round = singleSetRound([idea('all_clear')])
    const cs = considered(round)
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce(withActions(cs, [{
      option_id: optionId,
      allowed_actions: ['save_idea', 'create_contract'],
    }]))
    await renderAndGenerateRaw()
    const checkbox = await screen.findByRole('checkbox', { name: /Select all_clear/ })
    expect(checkbox).toBeEnabled()
  })

  it('cards without an actions entry keep today\'s behavior (legacy bridge until B1)', async () => {
    await renderAndGenerate([idea('legacy_card')])
    const checkbox = await screen.findByRole('checkbox', { name: /Select legacy_card/ })
    expect(checkbox).toBeEnabled()
  })
})

describe('D3: the audit drawer', () => {
  function withDecisionSections(cs: api.ConsideredSetResp, optionId: string) {
    return {
      ...cs,
      contract_version: 2,
      considered_revision_id: 'crv_test',
      recommended_options: [{
        option_id: optionId, name: null, recipe_id: 'recipe:x',
        binding_state: 'bound',
        allowed_actions: ['save_idea', 'create_contract'],
        blocked_actions: {},
      }],
    }
  }

  const RECORD: api.OptionDecisionRecord = {
    decision_id: 'sod_1', source_definition_id: 'complaint_count@window=90',
    generation_source: 'recipe', planning_request_hash: 'prh_abcdef1234567890',
    binding_state: 'bound', readiness: 'FORMULA_BLOCKED', review_current: true,
    validation_status: 'NEEDS_EXTERNAL_VALIDATION',
    dataset_story: {
      binding_plan: { source_table: 'accounts', window: 90,
                      population_ref: 'accounts', pit: 'event-anchored trailing window' },
    },
    evidence: {
      verdicts: [
        { role: 'who', status: 'bound', selected_ref: 'public.accounts.customer_id',
          reason_codes: [] },
      ],
      eligibility_audit: [
        { role: 'who', object_ref: 'public.accounts.customer_id', status: 'eligible',
          reason_codes: [] },
        { role: 'who', object_ref: 'public.accounts.alt_id', status: 'provisional',
          reason_codes: ['PROPOSED_METADATA_ONLY'] },
      ],
      validation: { status: 'needs_external_validation',
                    families: [{ family: 'leakage', state: 'evaluated' }] },
    },
    decision_manifest: { authority_matrix_hash: 'amh_1234567890abcdef' },
    observation_id: 'sco_1', context_hash: 'ctx_abcdef1234567890',
    recorded_at: '2026-08-14 00:00:00+00',
  }

  it('fetches the stored record on demand and renders the losing shortlist', async () => {
    const round = singleSetRound([idea('audited')])
    const cs = considered(round)
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce(withDecisionSections(cs, optionId))
    contractOptionDetail.mockResolvedValue({
      considered_revision_id: 'crv_test', considered_content_hash: 'h',
      generation_run_id: 'run_1', option_id: optionId, option: {},
      decision_record: RECORD,
    })
    await renderAndGenerateRaw()
    expect(contractOptionDetail).not.toHaveBeenCalled()      // ON DEMAND, never eager
    await userEvent.click(await screen.findByRole('button', { name: 'Decision record' }))
    expect(contractOptionDetail).toHaveBeenCalledWith('crv_test', optionId)
    expect(await screen.findByText(/Considered and not chosen/)).toBeInTheDocument()
    expect(screen.getByText('public.accounts.alt_id')).toBeInTheDocument()
    expect(screen.getByText(/Reads/)).toBeInTheDocument()    // the frozen plan section
    expect(screen.getByText(/leakage: evaluated/)).toBeInTheDocument()
  })

  it('renders honest absence for an option with no stored record', async () => {
    const round = singleSetRound([idea('legacy_opt')])
    const cs = considered(round)
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce(withDecisionSections(cs, optionId))
    contractOptionDetail.mockResolvedValue({
      considered_revision_id: 'crv_test', considered_content_hash: 'h',
      generation_run_id: 'run_1', option_id: optionId, option: {},
    })
    await renderAndGenerateRaw()
    await userEvent.click(await screen.findByRole('button', { name: 'Decision record' }))
    expect(await screen.findByText(/No stored decision record/)).toBeInTheDocument()
  })
})

describe('D3: operation-class grouping, buildability, and the deep link', () => {
  it('groups the list by typed operation class with fact headings', async () => {
    const flows = { ...idea('net_flow'), operation_class: 'sum' }
    const ratios = { ...idea('util_ratio'), operation_class: 'ratio' }
    const conceptual = idea('pattern_idea')                 // no class — conceptual
    contractConsideredSet.mockResolvedValueOnce(
      considered(singleSetRound([flows, ratios, conceptual])))
    await renderAndGenerateRaw()
    expect(await screen.findByRole('heading', { name: 'Flows & sums' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Ratios & utilization' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Conceptual patterns' })).toBeInTheDocument()
  })

  it('a single-class round keeps the flat list — no heading noise', async () => {
    const a = { ...idea('flow_a'), operation_class: 'sum' }
    const b = { ...idea('flow_b'), operation_class: 'sum' }
    contractConsideredSet.mockResolvedValueOnce(considered(singleSetRound([a, b])))
    await renderAndGenerateRaw()
    await screen.findByText('flow_a')
    expect(screen.queryByRole('heading', { name: 'Flows & sums' })).not.toBeInTheDocument()
  })

  it('renders the server buildability verdict and links confirmation to the exact field', async () => {
    const card = {
      ...idea('gated'),
      input_role_bindings: [{
        role: 'who', ref: ['cib', 'public.accounts.customer_id'] as [string, string],
        authority: 'llm/proposed', confirmation_required: true,
      }],
    }
    const cs = considered(singleSetRound([card]))
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce({
      ...cs, contract_version: 2,
      recommended_options: [{
        option_id: optionId, name: null, recipe_id: 'r', binding_state: 'bound',
        allowed_actions: ['save_idea'],
        blocked_actions: { create_contract: [{
          code: 'PROPOSED_METADATA_ONLY',
          next_step: 'confirm the AI-proposed concept(s) in the Governance screen',
        }] },
      }],
    })
    await renderAndGenerateRaw()
    expect(await screen.findByText(/confirm the AI-proposed concept/)).toBeInTheDocument()
    const link = screen.getByText('needs confirmation →') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe(
      '#asset?source=cib&object_ref=public.accounts.customer_id')
  })
})

// ── Slice 1: the phase-aware post-submit workspace ────────────────────────────────────────────
//
// The screen used to render the full intake form above every result, with the hypothesis input
// still editable beside output generated from earlier text. These pin the corrected hierarchy:
// ONE derived phase drives the shell, the submitted brief is a snapshot and not an input, and the
// results own the first viewport.

describe('post-submit workspace shell', () => {
  function phaseOf(container: HTMLElement): string | null {
    return container.querySelector('section[data-phase]')?.getAttribute('data-phase') ?? null
  }

  // A v2 response whose per-option binding states are the ONLY source of the composition strip.
  function withBindingStates(
    cs: api.ConsideredSetResp,
    states: string[],
  ): api.ConsideredSetResp {
    return {
      ...cs,
      contract_version: 2,
      recommended_options: states.map((binding_state, i) => ({
        option_id: `opt_${i}`, name: null, recipe_id: `recipe_${i}`, binding_state,
        allowed_actions: ['save_idea'], blocked_actions: {},
      })),
    }
  }

  it('draft is the intake form and nothing else: no brief card, no results, no composition', () => {
    const { container } = render(<WorkbenchScreen />)
    expect(phaseOf(container)).toBe('draft')
    expect(screen.getByLabelText('Hypothesis')).toBeInTheDocument()
    expect(screen.getByLabelText('Prediction goal')).toBeInTheDocument()
    expect(screen.queryByText('Your submitted brief')).toBeNull()
    expect(screen.queryByRole('heading', { name: /proposed feature/i })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'What this run returned' })).toBeNull()
    expect(screen.queryByText(/no grounded candidates/i)).toBeNull()
  })

  it('a landed round replaces the intake form with the compact submitted brief', async () => {
    const { container } = render(<WorkbenchScreen />)
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([IDEA])))
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(phaseOf(container)).toBe('compare')
    // The whole intake form is gone — not merely visually shrunk.
    expect(screen.queryByLabelText('Hypothesis')).toBeNull()
    expect(screen.queryByLabelText('Prediction goal')).toBeNull()
    expect(screen.queryByLabelText('Catalog source')).toBeNull()
    expect(screen.queryByRole('button', { name: /generate candidate sets/i })).toBeNull()
    // and the compact brief states what the run was submitted with.
    expect(screen.getByText('Your submitted brief')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    expect(screen.getByText('Goal: predict churn')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revise brief' })).toBeInTheDocument()
  })

  // THE CRITICAL FINDING, pinned. A user must never be able to read results beside request text
  // that did not produce them.
  it('the brief quotes the ROUND snapshot and does not follow the draft field', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Revise brief' }))
    await userEvent.clear(screen.getByLabelText('Hypothesis'))
    await userEvent.type(screen.getByLabelText('Hypothesis'), 'dormant cards precede attrition')
    // Cancel: back to the run exactly as it was.
    await userEvent.click(screen.getByRole('button', { name: 'Keep submitted brief' }))

    // The results are still the old round's, and the brief beside them still says what produced
    // them — the edited draft text is nowhere near the submitted brief.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    expect(screen.queryByText('dormant cards precede attrition')).toBeNull()
    // and nothing silently reverted the human's typing either.
    await userEvent.click(screen.getByRole('button', { name: 'Revise brief' }))
    expect(screen.getByLabelText('Hypothesis')).toHaveValue('dormant cards precede attrition')
  })

  it('a scope edit voids the round and returns the screen to its draft shell', async () => {
    const { container } = render(<WorkbenchScreen />)
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([IDEA])))
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await reviseBrief()
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    // The candidates that brief produced are gone, so the brief that describes them goes too.
    expect(screen.queryByText('Your submitted brief')).toBeNull()
    expect(phaseOf(container)).toBe('draft')
    expect(screen.getByLabelText('Hypothesis')).toHaveValue(HYPOTHESIS)
  })

  it('composition counts the binding states THIS run returned, and invents nothing', async () => {
    contractConsideredSet.mockResolvedValueOnce(withBindingStates(
      considered(singleSetRound([IDEA, OTHER_IDEA])),
      ['bound', 'bound', 'ambiguous', 'missing', 'blocked'],
    ))
    await renderAndGenerateRaw()
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'What this run returned' })).toBeInTheDocument()
    // One assertion over the whole tally: order, counts and labels, from the wire alone.
    expect(screen.getByRole('img', {
      name: '2 bound, 1 ambiguous, 1 missing operands, 1 structurally blocked',
    })).toBeInTheDocument()
    // Each state says what it MEANS — an unconfirmed proposal is work, never a failure.
    expect(screen.getByText(/every operand resolved/)).toBeInTheDocument()
    expect(screen.getByText(/needs confirming before these can bind/)).toBeInTheDocument()
    expect(screen.getByText(/refused by a named rule, not a gap/)).toBeInTheDocument()
    // The concept's illustrative figures are NOT the product.
    expect(screen.queryByText('787')).toBeNull()
    expect(screen.queryByText('917')).toBeNull()
  })

  it('an unknown binding state renders as words and still counts toward the total', async () => {
    contractConsideredSet.mockResolvedValueOnce(withBindingStates(
      considered(singleSetRound([IDEA])), ['bound', 'quarantined_by_policy'],
    ))
    await renderAndGenerateRaw()
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('img', {
      name: '1 bound, 1 quarantined by policy',
    })).toBeInTheDocument()
  })

  it('no composition strip when the response carried no option binding states', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'What this run returned' })).toBeNull()
  })

  it('a landed round moves focus to the stage heading and announces the counts', async () => {
    await renderAndGenerateSets(multiSetRound())
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    const heading = screen.getByRole('heading', { name: 'Compare and refine' })
    expect(heading).toHaveAttribute('tabindex', '-1')
    expect(heading).toHaveFocus()
    expect(screen.getByText('Results ready: 2 sets, 4 candidates.')).toBeInTheDocument()
  })

  it('announces without stealing the caret while the human is typing', async () => {
    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    // The human goes back to the hypothesis field while the round is still in flight.
    const field = screen.getByLabelText('Hypothesis')
    act(() => { field.focus() })
    await act(async () => {
      pending.resolve(considered(singleSetRound([IDEA])))
    })
    // Their half-written revision is not deleted and their caret is not moved…
    expect(screen.getByLabelText('Hypothesis')).toHaveFocus()
    expect(screen.queryByRole('heading', { name: 'Compare and refine' })).toBeNull()
    // …and the round is still announced, and still marked as belonging to the submitted brief.
    expect(screen.getByText('Results ready: 1 set, 1 candidate.')).toBeInTheDocument()
    expect(screen.getByText(/results below were generated for the submitted brief/i))
      .toBeInTheDocument()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
  })

  it('empty: reports the absence and the zero it measured, inventing nothing', async () => {
    contractConsideredSet.mockResolvedValue(considered(singleSetRound([])))
    const { container } = render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByText(/no grounded candidates for that goal/i)).toBeInTheDocument()
    expect(phaseOf(container)).toBe('empty')
    // The submitted brief still says what was asked; unset scope reads "not set", never a guess.
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    expect(screen.getByText('catalog · deposits')).toBeInTheDocument()
    expect(screen.getByText('target · not set')).toBeInTheDocument()
    // The engine-output card states the measured zero, and no composition is drawn over nothing.
    expect(screen.getByText('0 candidates')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'What this run returned' })).toBeNull()
    expect(screen.getByText('No candidates were returned for this brief.')).toBeInTheDocument()
  })

  it('error: the notice is the page, and the form the human filled in stays open to retry', async () => {
    const detail = 'catalog ftr has no activated cross-catalog interlock'
    contractConsideredSet.mockRejectedValue(new api.ApiError(503, detail))
    const { container } = render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(phaseOf(container)).toBe('error')
    // No round was produced, so there is no snapshot to show: the screen does NOT invent a brief
    // card for a run that never landed.
    expect(screen.queryByText('Your submitted brief')).toBeNull()
    // The typed brief is still there, one click from a retry.
    expect(screen.getByLabelText('Hypothesis')).toHaveValue(HYPOTHESIS)
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeEnabled()
  })

  it('scope review owns the shell, and the output card says what is not there yet', async () => {
    vi.stubEnv('VITE_INTENT_CONFIRMATION_UI', '1')
    contractRecognitions.mockResolvedValue(recognitionResp({
      candidates: [{
        use_case_id: 'customer.churn', display_name: 'Customer churn',
        relationship: 'primary', confidence: 'high', evidence_spans: ['churn'],
      }],
    }))
    const { container } = render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('Catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('Hypothesis'), HYPOTHESIS)
    await userEvent.type(screen.getByLabelText('Prediction goal'), 'predict churn')
    await userEvent.click(screen.getByRole('button', { name: /generate candidate sets/i }))
    expect(await screen.findByRole('heading', { name: /confirm the scope/i })).toBeInTheDocument()
    expect(phaseOf(container)).toBe('scope_review')
    expect(screen.getByRole('heading', { name: 'Confirm scope' })).toBeInTheDocument()
    expect(screen.getByText('No candidates yet')).toBeInTheDocument()
    expect(screen.getByText('The run waits on your scope confirmation.')).toBeInTheDocument()
  })
})

// ------------------------------------------------------------ Slice 2: the decision workspace ----
describe('Slice 2: the decision workspace', () => {
  // A round with enough shape to exercise every axis the wire actually carries: two sets, a
  // per-option binding state on each option, one candidate the backend marked as owing data
  // checks, and one recipe whose review the backend says is not current.
  const ACCEL: api.FeatureIdea = {
    option_id: 'opt_accel', name: 'txn_acceleration_30d',
    description: 'compares transaction count in the last 30 days with the prior 30',
    derives_from: ['public.transactions.id'], aggregation: 'ratio', grain_table: 'customers',
    derives_pairs: [['cib', 'public.transactions.id']],
    verification: 'DESIGN-CHECKED', critic_note: '', rationale: '',
    generation_source: 'recipe', recipe_id: 'accel_recipe',
    validation_status: 'DESIGN_CHECKED',
  }
  const RECENCY: api.FeatureIdea = {
    option_id: 'opt_recency', name: 'days_since_last_txn',
    description: 'distance from the cutoff to the most recent eligible transaction',
    derives_from: ['public.transactions.ts'], aggregation: 'recency', grain_table: 'customers',
    derives_pairs: [['cib', 'public.transactions.ts']],
    verification: 'DESIGN-CHECKED', critic_note: '', rationale: '',
    generation_source: 'recipe', recipe_id: 'recency_recipe',
    validation_status: 'NEEDS_EXTERNAL_VALIDATION',
  }
  const DEBIT_RATIO: api.FeatureIdea = {
    option_id: 'opt_ratio', name: 'debit_to_credit_ratio_30d',
    description: 'normalises debit activity by the credit transaction count',
    derives_from: ['public.transactions.amount'], aggregation: 'ratio', grain_table: 'customers',
    derives_pairs: [['cib', 'public.transactions.amount']],
    verification: 'DESIGN-CHECKED', critic_note: '', rationale: '',
    generation_source: 'recipe', recipe_id: 'ratio_recipe',
    validation_status: 'DESIGN_CHECKED',
  }

  const REFUSALS: api.Rejection[] = [
    { name: 'balance_slope_90d', code: 'STALE', reason: 'deposits last loaded 14 days ago' },
    { name: 'salary_band_flag', code: 'PROTECTED_CHARACTERISTIC',
      reason: 'derives from a protected attribute' },
    { name: 'branch_visits_30d', code: 'STALE', reason: 'branch feed has no arrival guarantee' },
  ]

  function workspaceRound(over: Partial<api.ConsideredSetResp> = {}): api.ConsideredSetResp {
    return {
      intent_id: 'int_1', anchor: null,
      alternatives: [
        { lens: 'temporal', features: [ACCEL, RECENCY] },
        { lens: 'ratio', features: [DEBIT_RATIO] },
      ],
      recommendation: {
        recommended_lens: 'temporal',
        reasoning: 'recency signals move earliest for a churn horizon',
        caveat: CAVEAT,
      },
      rejections: REFUSALS,
      contract_version: 2,
      considered_revision_id: 'crv_ws',
      recommended_options: [
        {
          option_id: 'opt_accel', name: null, recipe_id: 'accel_recipe',
          binding_state: 'bound',
          allowed_actions: ['save_idea', 'create_contract'], blocked_actions: {},
        },
        {
          option_id: 'opt_recency', name: null, recipe_id: 'recency_recipe',
          binding_state: 'ambiguous',
          allowed_actions: ['save_idea'],
          blocked_actions: {
            create_contract: [{
              code: 'RECIPE_REVIEW_NOT_CURRENT',
              next_step: 'Ask a steward to re-review this recipe',
            }],
          },
        },
      ],
      actionable_options: [{
        option_id: 'opt_ratio', name: null, recipe_id: 'ratio_recipe',
        binding_state: 'bound',
        allowed_actions: ['save_idea', 'create_contract'], blocked_actions: {},
      }],
      ...over,
    }
  }

  async function renderWorkspace(over: Partial<api.ConsideredSetResp> = {}) {
    contractConsideredSet.mockResolvedValue(workspaceRound(over))
    await renderAndGenerateRaw()
    expect(await screen.findByText('txn_acceleration_30d')).toBeInTheDocument()
  }

  function rail(): HTMLElement {
    return screen.getByRole('complementary', { name: 'Your decision' })
  }

  function rowList(): HTMLElement {
    return document.querySelector('ul.rows') as HTMLElement
  }

  function follows(first: Element, second: Element): boolean {
    return Boolean(
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
  }

  it('puts whole-round feedback ABOVE the candidate list, next to the recommendation', async () => {
    await renderWorkspace()
    const feedback = screen.getByLabelText('Feedback on the whole round')
    const recommendation = screen.getByText(/Engine's pick: Temporal/)
    // The recommendation, then the feedback that disagrees with it, then the rows. On the
    // reviewed run this control sat after hundreds of rows, which is the finding being fixed.
    expect(follows(recommendation, feedback)).toBe(true)
    expect(follows(feedback, rowList())).toBe(true)
  })

  it('keeps the decision rail in normal document order, never over the candidate text', async () => {
    await renderWorkspace()
    const aside = rail()
    const list = rowList()
    // Neither contains the other: the rail is a SIBLING grid cell, so its sticky behaviour is
    // confined to its own column and can never paint across a row.
    expect(aside.contains(list)).toBe(false)
    expect(list.contains(aside)).toBe(false)
    expect(aside.closest('.work-layout')).not.toBeNull()
    expect(follows(list, aside)).toBe(true)
    // No inline positioning: the layout lives in the stylesheet, and nothing here escapes flow.
    expect(aside.getAttribute('style')).toBeNull()
  })

  it('states the selection count, the source sets, and what approving will WRITE', async () => {
    await renderWorkspace()
    // Empty tray: the structure of the decision, and honest absence — never a dead button.
    expect(within(rail()).getByText(/Nothing is selected yet/)).toBeInTheDocument()
    expect(within(rail()).queryByRole('button', { name: /approve and register/i })).toBeNull()

    await selectCandidate('txn_acceleration_30d')
    expect(within(rail()).getByText('1 selected')).toBeInTheDocument()
    expect(within(rail()).getByText(
      /Approve and register writes 1 definition with its lineage, under your name/,
    )).toBeInTheDocument()
    // The consequence is stated in the app's own vocabulary and claims nothing about value.
    expect(within(rail()).getByText(/computes nothing and proves nothing about predictive value/))
      .toBeInTheDocument()
    // Governability is the SERVER's verdict, reported.
    expect(within(rail()).getByText('It can also be governed into a signed contract.'))
      .toBeInTheDocument()
    expect(within(rail()).getByRole('button', { name: 'Select and draft 1' })).toBeInTheDocument()

    // Picking across sets names the mix, so the tray says where the picks came from.
    await userEvent.click(screen.getByRole('button', { name: /Ratio set/ }))
    await selectCandidate('debit_to_credit_ratio_30d')
    expect(within(rail()).getByText('2 selected')).toBeInTheDocument()
    expect(within(rail()).getByText(/mixed from 2 sets/)).toBeInTheDocument()
    expect(within(rail()).getByText(
      /Approve and register writes 2 definitions with their lineage/)).toBeInTheDocument()
  })

  it('names which selections cannot be governed, and that registering still works', async () => {
    await renderWorkspace()
    await selectCandidate('txn_acceleration_30d')
    // A whole-round regeneration PINS the selected candidate: it survives as a kept row from an
    // earlier round, which is outside the new round's governable snapshot by construction.
    contractConsideredSet.mockResolvedValue(workspaceRound())
    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'fewer balance aggregates')
    await userEvent.click(
      screen.getByRole('button', { name: 'Regenerate with feedback · round 1 of 3' }))
    expect(await screen.findByText('Kept')).toBeInTheDocument()

    expect(within(rail()).getByText(/None of these can be governed from this round/))
      .toBeInTheDocument()
    // Never a dead end: the arm that still works is named in the same sentence.
    expect(within(rail()).getByText(/Registering still works/)).toBeInTheDocument()
    expect(within(rail()).queryByRole('button', { name: /^Govern/ })).toBeNull()
    expect(within(rail()).getByRole('button', { name: 'Approve and register 1 feature' }))
      .toBeInTheDocument()

    // Adding a FRESH pick from the new round makes the split explicit — never one flat verdict.
    await userEvent.click(screen.getByRole('checkbox', {
      name: 'Select txn_acceleration_30d (temporal; Recipe · accel_recipe)',
    }))
    expect(within(rail()).getByText(
      /1 of 2 can also be governed into signed contracts; the rest came from an earlier round/,
    )).toBeInTheDocument()
    expect(within(rail()).getByRole('button', { name: 'Select and draft 1' })).toBeInTheDocument()
  })

  it('searches WITHIN the active set only, and leaves the sets and their counts alone', async () => {
    await renderWorkspace()
    // The active set is the engine's advisory pick; the other set's candidate is not on the list.
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.queryByText('debit_to_credit_ratio_30d')).toBeNull()

    await userEvent.type(screen.getByLabelText('Search this set'), 'cutoff')
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.getByText(/Showing 1 of 2 candidates in the Temporal set\./))
      .toBeInTheDocument()
    // The SETS are untouched: a search is not a claim about the round. (The counts are the
    // server's stamps, tallied per set — this workspace fixture's cards all carry one.)
    expect(screen.getByText(/2 features · 2 design-checked/)).toBeInTheDocument()
    expect(screen.getByText(/1 feature · 1 design-checked/)).toBeInTheDocument()
    // and searching cannot reach into the set the human is not looking at.
    expect(screen.queryByText('debit_to_credit_ratio_30d')).toBeNull()
  })

  it('searches the description and the derived columns, not only the name', async () => {
    await renderWorkspace()
    await userEvent.type(screen.getByLabelText('Search this set'), 'transactions.ts')
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
  })

  it('narrows by each wire-stated facet, and every chip count is what it leaves', async () => {
    await renderWorkspace()
    // Binding state: straight off the per-option `binding_state`.
    await userEvent.click(screen.getByRole('button', { name: 'Ambiguous 1' }))
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
    expect(screen.getByText(/Showing 1 of 2 candidates/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Ambiguous 1' }))

    // Design checks: straight off the per-candidate `validation_status`. A candidate that owes
    // data checks is filterable as exactly that — never as a failure.
    await userEvent.click(screen.getByRole('button', { name: 'Needs data checks 1' }))
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Needs data checks 1' }))

    await userEvent.click(screen.getByRole('button', { name: 'Design-checked 1' }))
    expect(screen.getByText('txn_acceleration_30d')).toBeInTheDocument()
    expect(screen.queryByText('days_since_last_txn')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Design-checked 1' }))

    // Review currency: the engine's own RECIPE_REVIEW_NOT_CURRENT blocker, and the same
    // derivation the row badge uses, so a chip and a badge can never disagree.
    await userEvent.click(screen.getByRole('button', { name: 'Review not current 1' }))
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    expect(screen.getByText('review not current')).toBeInTheDocument()
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
  })

  it('ANDs across axes and ORs within one, and says what each axis was counted from', async () => {
    await renderWorkspace()
    // Two values in ONE axis mean "either": both rows survive.
    await userEvent.click(screen.getByRole('button', { name: 'Bound 1' }))
    await userEvent.click(screen.getByRole('button', { name: 'Ambiguous 1' }))
    expect(screen.getByText('txn_acceleration_30d')).toBeInTheDocument()
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    // Adding a DIFFERENT axis narrows further.
    await userEvent.click(screen.getByRole('button', { name: 'Needs data checks 1' }))
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
    expect(screen.getByText('days_since_last_txn')).toBeInTheDocument()
    // Provenance is on the page: a filter whose source is invisible is indistinguishable from
    // one the UI invented.
    expect(screen.getByRole('group', {
      name: 'Operand binding — counted from the binding state the engine returned per option',
    })).toBeInTheDocument()
    expect(screen.getByRole('group', {
      name: 'Design checks — counted from the design-check status the engine returned per candidate',
    })).toBeInTheDocument()
  })

  it('offers no facet for a state the round did not report', async () => {
    // No option-actions sections at all: the binding and review axes have nothing to count, so
    // they render no controls rather than an authored menu of empty states.
    await renderWorkspace({
      recommended_options: undefined, actionable_options: undefined, contract_version: undefined,
    })
    expect(screen.queryByRole('group', { name: /Operand binding/ })).toBeNull()
    expect(screen.queryByRole('group', { name: /Recipe review/ })).toBeNull()
    // The design-check axis still has values, so it still has controls.
    expect(screen.getByRole('group', { name: /Design checks/ })).toBeInTheDocument()
  })

  it('keeps a hidden pick selected and SAYS it is hidden', async () => {
    await renderWorkspace()
    await selectCandidate('txn_acceleration_30d')
    await userEvent.type(screen.getByLabelText('Search this set'), 'cutoff')
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()
    // The consequence never silently shrinks with the list.
    expect(within(rail()).getByText('1 selected')).toBeInTheDocument()
    expect(within(rail()).getByText(
      /1 of them is hidden by the current search and filters\. They stay selected/,
    )).toBeInTheDocument()
    expect(within(rail()).getByRole('button', { name: 'Approve and register 1 feature' }))
      .toBeInTheDocument()
  })

  it('narrowing to nothing is a fact about the filter, not about the round', async () => {
    await renderWorkspace()
    await userEvent.type(screen.getByLabelText('Search this set'), 'zzzz')
    expect(screen.getByText(/No candidate in this set matches the current search and filters/))
      .toBeInTheDocument()
    // The honest counterfactual, and the way back.
    expect(screen.getByText(/2 candidates are here with them cleared/)).toBeInTheDocument()
    expect(screen.getByText(/Showing 0 of 2 candidates/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear search and filters' }))
    expect(screen.getByText('txn_acceleration_30d')).toBeInTheDocument()
    expect(screen.getByLabelText('Search this set')).toHaveValue('')
  })

  it('drops the narrowing when a new round replaces the set it was aimed at', async () => {
    await renderWorkspace()
    await userEvent.type(screen.getByLabelText('Search this set'), 'cutoff')
    await userEvent.click(screen.getByRole('button', { name: 'Needs data checks 1' }))
    expect(screen.queryByText('txn_acceleration_30d')).toBeNull()

    contractConsideredSet.mockResolvedValue(workspaceRound())
    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'more behavioral signals')
    await userEvent.click(
      screen.getByRole('button', { name: 'Regenerate with feedback · round 1 of 3' }))
    expect(await screen.findByText(/1 of 3 rounds recorded/)).toBeInTheDocument()
    // A filter aimed at candidates that no longer exist would hide fresh ones behind a control
    // the human cannot connect to anything on screen.
    expect(screen.getByLabelText('Search this set')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Needs data checks 1' }))
      .toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('txn_acceleration_30d')).toBeInTheDocument()
  })

  it('adds no quality score, and keeps the backend caveat beside the backend recommendation', async () => {
    await renderWorkspace()
    expect(screen.getByText(/Engine's pick: Temporal/)).toBeInTheDocument()
    expect(screen.getByText(`Caveat: ${CAVEAT}`)).toBeInTheDocument()
    // Nothing on this screen ranks or scores: the toolbar only hides rows.
    expect(screen.queryByRole('button', { name: /sort/i })).toBeNull()
    expect(screen.queryByRole('combobox', { name: /sort/i })).toBeNull()
    expect(screen.queryByText(/score/i)).toBeNull()
    expect(screen.queryByText(/quality/i)).toBeNull()
    expect(screen.queryByText(/best match/i)).toBeNull()
  })

  it('refusals keep their named reasons behind a counted summary, never a bare number', async () => {
    await renderWorkspace()
    expect(screen.getByText('3 rejected')).toBeInTheDocument()
    // The summary is count-bearing AND reason-bearing before anything is expanded.
    expect(screen.getByText(/stale source 2 · protected characteristic 1/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show' }))
    // Each refusal names itself, its rule, and the engine's own sentence about it.
    expect(screen.getByText('balance_slope_90d')).toBeInTheDocument()
    expect(screen.getByText('deposits last loaded 14 days ago')).toBeInTheDocument()
    expect(screen.getByText('derives from a protected attribute')).toBeInTheDocument()
    expect(screen.getByText('branch feed has no arrival guarantee')).toBeInTheDocument()
  })

  it('puts the recorded feedback rounds behind a count-bearing summary, losing none', async () => {
    await renderWorkspace()
    expect(screen.queryByText(/rounds recorded/)).toBeNull()   // nothing recorded yet, no disclosure
    contractConsideredSet.mockResolvedValue(workspaceRound())
    await userEvent.type(
      screen.getByLabelText('Feedback on the whole round'), 'fewer balance aggregates')
    await userEvent.click(
      screen.getByRole('button', { name: 'Regenerate with feedback · round 1 of 3' }))
    const summary = await screen.findByText(/1 of 3 rounds recorded/)
    expect(summary).toBeInTheDocument()
    // The strip itself is still there, verbatim, attributed and countable.
    expect(screen.getByText(
      'Set feedback round 1 of 3 · recorded · from user:dev · "fewer balance aggregates" · '
      + 'kept 0 selected, replaced 3',
    )).toBeInTheDocument()
  })

  it('counts what was written beside what failed, and hides neither', async () => {
    registerFeature.mockResolvedValueOnce('feat_01')
    registerFeature.mockRejectedValueOnce(new api.ApiError(409, 'a feature by that name exists'))
    featureFreshness.mockResolvedValue(FRESH)
    await renderWorkspace()
    await selectCandidate('txn_acceleration_30d')
    await userEvent.click(screen.getByRole('button', { name: /Ratio set/ }))
    await selectCandidate('debit_to_credit_ratio_30d')
    await registerSelection(2)
    // The successes are NOT hidden behind the failure, and the failure keeps its own row.
    expect(await within(rail()).findByText(
      /1 of this round's candidates is saved or governed\./)).toBeInTheDocument()
    expect(within(rail()).getByText(
      /1 could not be written — each one says why on its own row\./)).toBeInTheDocument()
    expect(screen.getByText('a feature by that name exists')).toBeInTheDocument()
  })
})

// ------------------------------------------- Slice 2: readiness and the frozen plan, on record ----
describe('Slice 2: the readiness ladder', () => {
  function withRecord(optionId: string): api.ConsideredSetResp {
    const cs = considered(singleSetRound([idea('audited_feature')]))
    return {
      ...cs,
      contract_version: 2,
      considered_revision_id: 'crv_ladder',
      recommended_options: [{
        option_id: optionId, name: null, recipe_id: 'recipe:x', binding_state: 'bound',
        allowed_actions: ['save_idea', 'create_contract'], blocked_actions: {},
      }],
    }
  }

  function record(over: Partial<api.OptionDecisionRecord> = {}): api.OptionDecisionRecord {
    return {
      decision_id: 'sod_1', source_definition_id: 'complaint_count@window=90',
      generation_source: 'recipe', planning_request_hash: 'prh_abcdef1234567890',
      binding_state: 'bound', readiness: 'FORMULA_BLOCKED', review_current: true,
      validation_status: 'NEEDS_EXTERNAL_VALIDATION',
      dataset_story: {
        binding_plan: {
          source_table: 'public.transactions', window: 90,
          population_ref: 'public.customers', pit: 'last event strictly <= as_of',
        },
      },
      evidence: { verdicts: [], eligibility_audit: [] },
      decision_manifest: { authority_matrix_hash: 'amh_1234567890abcdef' },
      observation_id: null, context_hash: 'ctx_abcdef1234567890',
      recorded_at: '2026-08-15 00:00:00+00',
      ...over,
    }
  }

  async function openRecord(over: Partial<api.OptionDecisionRecord> = {}) {
    const cs = considered(singleSetRound([idea('audited_feature')]))
    const optionId = cs.alternatives[0].features[0].option_id!
    contractConsideredSet.mockResolvedValueOnce(withRecord(optionId))
    contractOptionDetail.mockResolvedValue({
      considered_revision_id: 'crv_ladder', considered_content_hash: 'h',
      generation_run_id: 'run_1', option_id: optionId, option: {},
      decision_record: record(over),
    })
    await renderAndGenerateRaw()
    await userEvent.click(await screen.findByRole('button', { name: 'Decision record' }))
  }

  it('locates the definition on the STORED readiness ladder and names the rung', async () => {
    await openRecord()
    expect(await screen.findByRole('img', {
      name: 'Readiness: formula blocked, rung 2 of 6',
    })).toBeInTheDocument()
    expect(screen.getByText(
      /A formula cannot be authored until a named prerequisite is settled/)).toBeInTheDocument()
    // Readiness and the design check are separate claims, and NEITHER is predictive value.
    expect(screen.getByText(/Readiness is separate from the design check/)).toBeInTheDocument()
    expect(screen.getByText(/neither is a claim about predictive value/)).toBeInTheDocument()
  })

  it('reads out exactly what would be computed, and only the terms the record carries', async () => {
    await openRecord()
    expect(await screen.findByText('Exactly what would be computed')).toBeInTheDocument()
    expect(screen.getByText('Reads')).toBeInTheDocument()
    expect(screen.getByText('public.transactions')).toBeInTheDocument()
    expect(screen.getByText('Population')).toBeInTheDocument()
    expect(screen.getByText('90 days')).toBeInTheDocument()
    expect(screen.getByText('last event strictly <= as_of')).toBeInTheDocument()
  })

  it('omits a plan term the record does not carry rather than filling in a default', async () => {
    await openRecord({
      dataset_story: { binding_plan: { source_table: 'public.transactions' } },
    })
    expect(await screen.findByText('Reads')).toBeInTheDocument()
    expect(screen.queryByText('Window')).toBeNull()
    expect(screen.queryByText('Point in time')).toBeNull()
    expect(screen.queryByText('Population')).toBeNull()
  })

  it('draws no ladder for a readiness that is not on it, and states the value instead', async () => {
    await openRecord({ readiness: 'RETIRED' })
    expect(await screen.findByText(/Readiness reported as/)).toBeInTheDocument()
    expect(screen.getByText('RETIRED')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Readiness: / })).toBeNull()
    expect(screen.getByText(/Withdrawn from the library/)).toBeInTheDocument()
  })

  it('a readiness this client has no copy for renders as itself, never as an invented rung', async () => {
    await openRecord({ readiness: 'QUARANTINED_BY_POLICY' })
    expect(await screen.findByText('QUARANTINED_BY_POLICY')).toBeInTheDocument()
    expect(screen.getByText(/not one this screen has copy for/)).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Readiness: / })).toBeNull()
  })

  it('does not draw a readiness ladder on the rows: it is not on the considered-set wire', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Readiness: / })).toBeNull()
    expect(document.querySelectorAll('.ladder')).toHaveLength(0)
  })
})

// ---------------------------------------------------------- Slice 3: the revised-round drawer ----
describe('Slice 3: the revise drawer', () => {
  function drawer(): HTMLElement {
    return screen.getByRole('dialog', { name: 'Revise the brief' })
  }

  async function openRevise() {
    await userEvent.click(screen.getByRole('button', { name: 'Revise brief' }))
  }

  async function retypeHypothesis(text: string) {
    const field = within(drawer()).getByLabelText('Hypothesis')
    await userEvent.clear(field)
    await userEvent.type(field, text)
  }

  const REVISED = 'dormant cards precede attrition'

  it('opens populated from the submitted snapshot, with the run still on screen behind it', async () => {
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()

    const panel = drawer()
    expect(within(panel).getByLabelText('Hypothesis')).toHaveValue(HYPOTHESIS)
    expect(within(panel).getByLabelText('Prediction goal')).toHaveValue('predict churn')
    expect(within(panel).getByLabelText('Catalog source')).toHaveValue('deposits')
    // No target field to populate: the brief does not collect one any more.
    expect(within(panel).queryByLabelText('Target column')).toBeNull()
    expect(within(panel).getByText('Populated from the brief this run was generated from.'))
      .toBeInTheDocument()
    // The run behind it is untouched and still says what produced it: a human editing a draft of
    // the brief can read the brief the results actually came from at the same time.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('Your submitted brief')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    // Focus is in the first field, not left on a control that has just disappeared.
    expect(within(panel).getByLabelText('Hypothesis')).toHaveFocus()
  })

  it('is the ONLY revise affordance while it is open', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()
    // The trigger is gone (it is replaced by what it opened) and Slice 1's inline banner does
    // not double up inside the drawer.
    expect(screen.queryByRole('button', { name: 'Revise brief' })).toBeNull()
    expect(screen.queryByText(/^Revising the brief\./)).toBeNull()
    expect(screen.getAllByLabelText('Hypothesis')).toHaveLength(1)
    // Two explicit outcomes, both inside the dialog, neither of them silent.
    expect(within(drawer()).getByRole('button', { name: 'Keep submitted brief' }))
      .toBeInTheDocument()
    expect(within(drawer()).getByRole('button', { name: /Generate revised round/ }))
      .toBeInTheDocument()
  })

  it('states the replacement policy BEFORE either outcome is taken', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()
    const policy = within(drawer()).getByText(/Generating leaves the results below/)
    // The chosen arm, said in full: keep the result until the new request succeeds.
    expect(policy).toHaveTextContent(
      'Generating leaves the results below on screen and unchanged until the new round lands. '
      + 'If the request fails, nothing here changes.')
    // and exactly what a landed round then replaces — no silent invalidation.
    expect(policy).toHaveTextContent(
      'When it lands it replaces this round: the generated candidates below, and any you have '
      + 'selected but not yet registered, make way for the new ones.')
    expect(policy).toHaveTextContent(
      'Definitions you wrote yourself stay on the list, and features you already registered '
      + 'stay registered.')
    // The tool that KEEPS picks is named, so the difference is not discovered by losing one.
    expect(policy).toHaveTextContent('use "Feedback on the whole round" below')
  })

  it('Cancel returns to the current run unchanged, and sends nothing', async () => {
    await renderAndGenerate([IDEA, OTHER_IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await selectCandidate('avg_balance')
    const callsBefore = contractConsideredSet.mock.calls.length

    await openRevise()
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: 'Keep submitted brief' }))

    expect(screen.queryByRole('dialog')).toBeNull()
    // Candidates, picks and the brief that produced them: all exactly as they were.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('txn_count')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    expect(screen.queryByText(REVISED)).toBeNull()
    // Cancel writes nothing and asks the engine for nothing.
    expect(contractConsideredSet.mock.calls).toHaveLength(callsBefore)
    // Focus goes back to the control it came from, never to <body>.
    expect(screen.getByRole('button', { name: 'Revise brief' })).toHaveFocus()
  })

  it('Escape cancels exactly like the button', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revise brief' })).toHaveFocus()
  })

  it('keeps an in-progress edit through a cancel, names it as an edit, and can undo it', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: 'Keep submitted brief' }))

    await openRevise()
    // Cancelling did not silently revert their typing — that is its own kind of invalidation.
    expect(within(drawer()).getByLabelText('Hypothesis')).toHaveValue(REVISED)
    expect(within(drawer()).getByText(
      /These fields hold your edits, not the brief this run was generated from/))
      .toBeInTheDocument()
    // and the way back to the submitted brief is one click, not retyping from the quote.
    await userEvent.click(
      within(drawer()).getByRole('button', { name: 'Restore the submitted brief' }))
    expect(within(drawer()).getByLabelText('Hypothesis')).toHaveValue(HYPOTHESIS)
    expect(within(drawer()).getByText('Populated from the brief this run was generated from.'))
      .toBeInTheDocument()
  })

  it('a submitted revision keeps the previous results visible until the new round SUCCEEDS', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await selectCandidate('avg_balance')

    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    await openRevise()
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: /Generate revised round/ }))

    // IN FLIGHT — the chosen policy arm: the round is still there, still picked, and the deck
    // still attributes it to the brief that actually produced it.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    expect(screen.queryByText(REVISED)).toBeNull()

    await act(async () => { pending.resolve(considered(singleSetRound([OTHER_IDEA]))) })

    // LANDED — replaced, and only now does the deck quote the brief that produced THESE.
    expect(await screen.findByText('txn_count')).toBeInTheDocument()
    expect(screen.queryByText('avg_balance')).toBeNull()
    expect(screen.getByText(REVISED)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(contractConsideredSet).toHaveBeenLastCalledWith(REVISED, 'predict churn', {
      catalogSource: 'deposits', targetRef: undefined,
    })
  })

  it('a FAILED revision leaves the run, and its picks, exactly where they were', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await selectCandidate('avg_balance')

    // Distinct from every other 503 fixture in this file on purpose: each of the six re-aimed
    // pins must prove verbatim rendering on its OWN sentence, not on one shared between two.
    const detail = 'the planner pool is saturated; no worker took the revised round'
    contractConsideredSet.mockRejectedValueOnce(new api.ApiError(503, detail))
    await openRevise()
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: /Generate revised round/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    // Exactly the promise the drawer made before the click. An outage must not cost the human
    // their candidates and their selections.
    expect(screen.getByText('avg_balance')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByText(HYPOTHESIS)).toBeInTheDocument()
    // and the drawer stays open on the edit, one click from a retry.
    expect(within(drawer()).getByLabelText('Hypothesis')).toHaveValue(REVISED)
  })

  it('a revised round resets the active set, the narrowing and the picks it replaced', async () => {
    await renderAndGenerateSets(multiSetRound())
    expect(await screen.findByText('Temporal set')).toBeInTheDocument()
    // Work the view: switch off the engine's pick, narrow it, and select something.
    await userEvent.click(screen.getByRole('button', { name: /Ratio set/ }))
    await userEvent.type(screen.getByLabelText('Search this set'), 'balance_to')
    await selectCandidate('balance_to_limit_ratio')
    expect(screen.getByText('1 selected')).toBeInTheDocument()

    contractConsideredSet.mockResolvedValue(considered(multiSetRound()))
    await openRevise()
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: /Generate revised round/ }))
    expect(await screen.findByText(REVISED)).toBeInTheDocument()

    // The new round is entered the way any round is: on the engine's advisory pick, unfiltered,
    // with nothing selected. A view aimed at candidates that no longer exist is not carried over.
    expect(screen.getByRole('button', { name: /Temporal set/ })).toHaveAttribute(
      'aria-pressed', 'true')
    expect(screen.getByLabelText('Search this set')).toHaveValue('')
    expect(screen.getByText('0 selected')).toBeInTheDocument()
    expect(screen.getByText(/Nothing is selected yet/)).toBeInTheDocument()
    // "Scroll" on this screen is Slice 1's deliberate transition and nothing else: a revised
    // round moves the caret to the stage heading, exactly as a first round does. There is no
    // saved scroll offset to restore, and pretending to restore one would be a fiction.
    expect(screen.getByRole('heading', { name: 'Compare and refine' })).toHaveFocus()
  })

  it('leaves definitions the human wrote themselves on the list, exactly as the copy said', async () => {
    featureRecipe.mockResolvedValue(recipeWith([]))
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    // openDescribe goes through Revise brief, so the drawer is already open here — and the
    // describe panel it opened is NOT behind a backdrop, which is why the drawer is not modal.
    await openDescribe()
    await draftFeature('total spend per customer')
    expect(await screen.findByText('total_spend_per_customer')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Revise the brief' })).toBeInTheDocument()

    contractConsideredSet.mockResolvedValue(considered(singleSetRound([OTHER_IDEA])))
    await retypeHypothesis(REVISED)
    await userEvent.click(within(drawer()).getByRole('button', { name: /Generate revised round/ }))
    expect(await screen.findByText('txn_count')).toBeInTheDocument()

    // The generated round was replaced; the human's own definition was not.
    expect(screen.queryByText('avg_balance')).toBeNull()
    expect(screen.getByText('total_spend_per_customer')).toBeInTheDocument()
  })

  it('a round landing while the caret is in the drawer keeps the drawer, and the typing', async () => {
    await renderAndGenerate([IDEA])
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    const pending = deferred<api.ConsideredSetResp>()
    contractConsideredSet.mockImplementationOnce(() => pending.promise)
    await openRevise()
    await userEvent.click(within(drawer()).getByRole('button', { name: /Generate revised round/ }))
    // The human goes back to the field and starts the NEXT revision while the round is in flight.
    const field = within(drawer()).getByLabelText('Prediction goal')
    act(() => { field.focus() })
    await act(async () => { pending.resolve(considered(singleSetRound([OTHER_IDEA]))) })

    // Slice 1's caret protection, applied to this surface: the drawer does not close under the
    // caret and take a half-typed revision with it.
    expect(screen.getByRole('dialog', { name: 'Revise the brief' })).toBeInTheDocument()
    expect(within(drawer()).getByLabelText('Prediction goal')).toHaveFocus()
    // and the round still landed behind it.
    expect(screen.getByText('txn_count')).toBeInTheDocument()
  })

  it('a scope edit in the drawer voids the round, closes the drawer, and says so', async () => {
    await renderAndGenerate([IDEA], { source: 'deposits' })
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await openRevise()
    await userEvent.type(within(drawer()).getByLabelText('Catalog source'), '2')

    // The candidates that scope produced are gone, so the drawer's premise — a live run behind
    // it — is gone too. The page hands itself back to the draft shell rather than leaving a
    // dialog floating over nothing.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByText('avg_balance')).toBeNull()
    expect(screen.queryByText('Your submitted brief')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent(/scope changed/i)
    expect(screen.getByLabelText('Hypothesis')).toHaveValue(HYPOTHESIS)
  })

  it('the draft shell has no drawer: there is no run to revise', () => {
    render(<WorkbenchScreen />)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Revise brief' })).toBeNull()
    expect(screen.getByLabelText('Hypothesis')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /generate candidate sets/i })).toBeInTheDocument()
  })
})
