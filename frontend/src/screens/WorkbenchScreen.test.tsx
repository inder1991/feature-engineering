import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { WorkbenchScreen } from './WorkbenchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    recommendFeatures: vi.fn(),
    featureRecipe: vi.fn(),
    leakageCheck: vi.fn(),
    registerFeature: vi.fn(),
    featureFreshness: vi.fn(),
  }
})
const recommendFeatures = vi.mocked(api.recommendFeatures)
const featureRecipe = vi.mocked(api.featureRecipe)
const leakageCheck = vi.mocked(api.leakageCheck)
const registerFeature = vi.mocked(api.registerFeature)
const featureFreshness = vi.mocked(api.featureFreshness)

beforeEach(() => {
  recommendFeatures.mockReset()
  featureRecipe.mockReset()
  leakageCheck.mockReset()
  registerFeature.mockReset()
  featureFreshness.mockReset()
})

// derives_pairs deliberately names a catalog ('cards') that differs from the source the tests
// type into the Context field ('deposits'): registration lineage must come from the backend
// pairs, never from the typed source.
const IDEA: api.FeatureIdea = {
  name: 'avg_balance', description: 'average balance per customer',
  derives_from: ['public.accounts.balance'], aggregation: 'avg', grain_table: 'customers',
  derives_pairs: [['cards', 'public.accounts.balance']],
}

const OTHER_IDEA: api.FeatureIdea = {
  name: 'txn_count', description: 'transactions per customer',
  derives_from: ['public.transactions.id'], aggregation: 'count', grain_table: 'customers',
  derives_pairs: [['cards', 'public.transactions.id']],
}

const FRESH: api.FeatureFreshness = { fresh: true, stale_sources: [] }

function recipeWith(joinPath: api.JoinStep[]): api.Recipe {
  return {
    intent: 'total spend per customer', grain_table: 'customers',
    derives_from: ['public.transactions.amount'], aggregation: 'sum', as_of_column: null,
    join_path: joinPath,
  }
}

async function suggest() {
  await userEvent.type(screen.getByLabelText('catalog source'), 'deposits')
  await userEvent.type(screen.getByLabelText('objective'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
}

async function confirmFirstProposal() {
  await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm register' }))
}

async function buildRecipe(joinPath: api.JoinStep[]) {
  featureRecipe.mockResolvedValue(recipeWith(joinPath))
  render(<WorkbenchScreen />)
  await userEvent.type(screen.getByLabelText('catalog source'), 'deposits')
  await userEvent.type(screen.getByLabelText('feature description'), 'total spend per customer')
  await userEvent.click(screen.getByRole('button', { name: /build recipe/i }))
  expect(await screen.findByText(/join path/i)).toBeInTheDocument()
}

describe('workbench screen', () => {
  it('registers a proposal only after an explicit confirm, with lineage from the backend pairs', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
    expect(registerFeature).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm register' }))
    // Lineage comes from derives_pairs ('cards'), not the typed Context source ('deposits').
    expect(registerFeature).toHaveBeenCalledWith({
      name: 'avg_balance', description: 'average balance per customer',
      grain_table: 'customers', aggregation: 'avg', as_of_column: null,
      derives_from: [{ catalog_source: 'cards', object_ref: 'public.accounts.balance' }],
    })
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/registered as/i)).toBeInTheDocument()
    expect(featureFreshness).toHaveBeenCalledWith('feat_01')
    expect(screen.getByText('fresh')).toBeInTheDocument()
  })

  it('sends exactly one register request when confirm is clicked twice in flight', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    let resolveRegister!: (id: string) => void
    registerFeature.mockImplementation(
      () => new Promise<string>(resolve => { resolveRegister = resolve }))
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
    const confirm = screen.getByRole('button', { name: 'Confirm register' })
    await userEvent.click(confirm)
    await userEvent.click(confirm)
    expect(registerFeature).toHaveBeenCalledTimes(1)
    expect(confirm).toBeDisabled()
    await act(async () => {
      resolveRegister('feat_01')
    })
    expect(await screen.findByText(/registered as/i)).toBeInTheDocument()
    expect(registerFeature).toHaveBeenCalledTimes(1)
  })

  it('cancel backs out of the confirm step without registering', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Register…' })).toBeInTheDocument()
  })

  it('keeps the retry UI and shows the error when registration fails', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockRejectedValue(new api.ApiError(409, 'feature name already registered'))
    render(<WorkbenchScreen />)
    await suggest()
    await confirmFirstProposal()
    expect(await screen.findByText('feature name already registered')).toBeInTheDocument()
    expect(screen.queryByText(/registered as/i)).not.toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Confirm register' })
    expect(confirm).toBeInTheDocument()
    expect(confirm).toBeEnabled()
  })

  it('marks a fresh registration with a fresh chip', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await suggest()
    await confirmFirstProposal()
    expect(await screen.findByText('fresh')).toBeInTheDocument()
    expect(screen.queryByText(/stale:/)).not.toBeInTheDocument()
  })

  it('marks a stale registration with the stale sources', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue({ fresh: false, stale_sources: ['cards'] })
    render(<WorkbenchScreen />)
    await suggest()
    await confirmFirstProposal()
    expect(await screen.findByText('stale: cards')).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
  })

  it('omits the freshness chip silently when the freshness call fails', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockRejectedValue(new api.ApiError(500, 'freshness unavailable'))
    render(<WorkbenchScreen />)
    await suggest()
    await confirmFirstProposal()
    expect(await screen.findByText(/registered as/i)).toBeInTheDocument()
    expect(screen.queryByText('fresh')).not.toBeInTheDocument()
    expect(screen.queryByText(/stale:/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not show a phantom registered state when a re-suggest reuses a name', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    featureFreshness.mockResolvedValue(FRESH)
    render(<WorkbenchScreen />)
    await suggest()
    await confirmFirstProposal()
    expect(await screen.findByText(/registered as/i)).toBeInTheDocument()
    // Second round returns a proposal with the same LLM-chosen name: it was never registered.
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    expect(await screen.findByRole('button', { name: 'Register…' })).toBeInTheDocument()
    expect(screen.queryByText(/registered as/i)).not.toBeInTheDocument()
  })

  it('clears proposals and registration state when the catalog source changes', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    render(<WorkbenchScreen />)
    await suggest()
    expect(await screen.findByText('avg_balance')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('catalog source'), 'x')
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
  })

  it('shows the honest 503 state when assist is unconfigured', async () => {
    recommendFeatures.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('objective'), 'churn')
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    expect(await screen.findByText(/ai assist is not configured/i)).toBeInTheDocument()
  })

  it('shows the empty state only after a suggestion round returns no proposals', async () => {
    recommendFeatures.mockResolvedValue([])
    render(<WorkbenchScreen />)
    expect(screen.queryByText(/no grounded proposals/i)).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('objective'), 'churn')
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    expect(await screen.findByText(/no grounded proposals/i)).toBeInTheDocument()
  })

  it('passes the optional entity scope through to the recommend call', async () => {
    recommendFeatures.mockResolvedValue([])
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('entity'), 'customer')
    await userEvent.type(screen.getByLabelText('objective'), 'churn')
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    expect(recommendFeatures).toHaveBeenCalledWith('churn', null, null, 'customer')
  })

  it('applies only the latest suggestion round when responses arrive out of order', async () => {
    let resolveFirst!: (ideas: api.FeatureIdea[]) => void
    let resolveSecond!: (ideas: api.FeatureIdea[]) => void
    recommendFeatures
      .mockImplementationOnce(
        () => new Promise<api.FeatureIdea[]>(resolve => { resolveFirst = resolve }))
      .mockImplementationOnce(
        () => new Promise<api.FeatureIdea[]>(resolve => { resolveSecond = resolve }))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('objective'), 'churn')
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    await act(async () => {
      resolveSecond([OTHER_IDEA])
    })
    expect(await screen.findByText('txn_count')).toBeInTheDocument()
    // The stale first response resolves late and must not overwrite the newer round.
    await act(async () => {
      resolveFirst([IDEA])
    })
    expect(screen.getByText('txn_count')).toBeInTheDocument()
    expect(screen.queryByText('avg_balance')).not.toBeInTheDocument()
  })

  it('renders the recipe join path with a fan-out warning', async () => {
    await buildRecipe([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:N' },
      { from_ref: 'public.accounts.id', to_ref: 'public.transactions.account_id', cardinality: '1:N' },
    ])
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('flags a lowercase 1:n hop as fan-out', async () => {
    await buildRecipe([
      { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:n' },
    ])
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('names unknown cardinality instead of rendering it as calm', async () => {
    await buildRecipe([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
      { from_ref: 'public.customers.cust_id', to_ref: 'public.segments.cust_id', cardinality: null },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.getByText(/cardinality unknown/)).toBeInTheDocument()
    expect(screen.getByText(/cannot be ruled out/i)).toBeInTheDocument()
  })

  it('stays calm on an all-N:1 join path', async () => {
    await buildRecipe([
      { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
    ])
    expect(screen.queryByText(/aggregate before joining/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/cannot be ruled out/i)).not.toBeInTheDocument()
  })

  it('clears the recipe and shows the error when a rebuild fails', async () => {
    featureRecipe
      .mockResolvedValueOnce(recipeWith([
        { from_ref: 'public.accounts.cust_id', to_ref: 'public.customers.cust_id', cardinality: 'N:1' },
      ]))
      .mockRejectedValueOnce(new api.ApiError(400, 'recipe failed'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('feature description'), 'total spend per customer')
    await userEvent.click(screen.getByRole('button', { name: /build recipe/i }))
    expect(await screen.findByRole('heading', { name: 'Recipe' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /build recipe/i }))
    expect(await screen.findByText('recipe failed')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Recipe' })).not.toBeInTheDocument()
  })

  it('surfaces leakage warnings as a banner', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    leakageCheck.mockResolvedValue([
      { object_ref: 'public.accounts.balance', reason: 'target-adjacent' }])
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.type(screen.getByLabelText('target column'), 'public.labels.churned')
    await userEvent.click(await screen.findByRole('button', { name: /check leakage/i }))
    expect(await screen.findByText(/possible target leakage/i)).toBeInTheDocument()
    expect(leakageCheck).toHaveBeenCalledWith(
      ['public.accounts.balance'], 'public.labels.churned')
    expect(screen.getByText(/target-adjacent/)).toBeInTheDocument()
  })

  it('invalidates leakage results when the target changes', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    leakageCheck.mockResolvedValue([
      { object_ref: 'public.accounts.balance', reason: 'target-adjacent' }])
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.type(screen.getByLabelText('target column'), 'public.labels.churned')
    await userEvent.click(await screen.findByRole('button', { name: /check leakage/i }))
    expect(await screen.findByText(/possible target leakage/i)).toBeInTheDocument()
    // The result was computed for the old target: any edit voids it.
    await userEvent.type(screen.getByLabelText('target column'), '2')
    expect(screen.queryByText(/possible target leakage/i)).not.toBeInTheDocument()
  })

  it('clears stale leakage warnings when a re-check fails', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    leakageCheck
      .mockResolvedValueOnce([
        { object_ref: 'public.accounts.balance', reason: 'target-adjacent' }])
      .mockRejectedValueOnce(new api.ApiError(400, 'unknown target'))
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.type(screen.getByLabelText('target column'), 'public.labels.churned')
    await userEvent.click(await screen.findByRole('button', { name: /check leakage/i }))
    expect(await screen.findByText(/possible target leakage/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /check leakage/i }))
    expect(await screen.findByText('unknown target')).toBeInTheDocument()
    expect(screen.queryByText(/possible target leakage/i)).not.toBeInTheDocument()
  })
})
